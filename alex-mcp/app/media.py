from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import subprocess
import uuid

import requests
from openai import OpenAI

from config import DATA_DIR, get_settings
from db import connect

MEDIA_DIR = os.path.join(DATA_DIR, "media")


def _safe_ext(mime_type: str, media_type: str) -> str:
    known = {
        "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
        "audio/ogg": ".ogg", "audio/opus": ".opus", "audio/mpeg": ".mp3",
        "audio/mp4": ".m4a", "application/pdf": ".pdf",
    }
    return known.get((mime_type or "").split(";")[0].lower()) or mimetypes.guess_extension(mime_type or "") or {
        "IMAGE": ".jpg", "AUDIO": ".ogg", "PDF": ".pdf"
    }.get(media_type, ".bin")


def save_media(source_message_id: str, media_type: str, mime_type: str,
               data_b64: str, original_name: str | None = None) -> str:
    conn = connect()
    try:
        existing = conn.execute(
            "SELECT media_id FROM media_objects WHERE source_message_id=? AND media_type=?",
            (source_message_id, media_type),
        ).fetchone()
        if existing:
            return existing["media_id"]

        raw = base64.b64decode(data_b64)
        digest = hashlib.sha256(raw).hexdigest()
        media_id = str(uuid.uuid4())
        os.makedirs(MEDIA_DIR, exist_ok=True)
        ext = _safe_ext(mime_type, media_type)
        path = os.path.join(MEDIA_DIR, f"{media_id}{ext}")
        with open(path, "wb") as f:
            f.write(raw)

        conn.execute(
            """INSERT INTO media_objects(
                media_id,source_message_id,media_type,mime_type,sha256,original_name,local_path
               ) VALUES(?,?,?,?,?,?,?)""",
            (media_id, source_message_id, media_type, mime_type or "application/octet-stream",
             digest, original_name, path),
        )
        conn.commit()
        return media_id
    finally:
        conn.close()


def _update_text(media_id: str, field: str, text: str) -> None:
    if field not in ("ocr_text", "transcript_text"):
        raise ValueError("unsupported media text field")
    conn = connect()
    try:
        conn.execute(f"UPDATE media_objects SET {field}=? WHERE media_id=?", (text[:50000], media_id))
        conn.commit()
    finally:
        conn.close()


def get_media(media_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM media_objects WHERE media_id=?", (media_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def extract_text(media_id: str) -> str:
    row = get_media(media_id)
    if not row:
        return ""
    path = row["local_path"]
    kind = row["media_type"]
    settings = get_settings()
    if kind == "IMAGE" and settings.ocr_enabled:
        try:
            p = subprocess.run(
                ["tesseract", path, "stdout", "-l", "eng", "--psm", "6"],
                capture_output=True, text=True, timeout=30,
            )
            text = (p.stdout or "").strip()
        except Exception:
            text = ""
        _update_text(media_id, "ocr_text", text)
        return text
    if kind == "PDF":
        try:
            p = subprocess.run(["pdftotext", "-layout", path, "-"], capture_output=True, text=True, timeout=30)
            text = (p.stdout or "").strip()
        except Exception:
            text = ""
        _update_text(media_id, "ocr_text", text)
        return text
    return ""


def _xai_stt(path: str, mime: str, key: str) -> str:
    with open(path, "rb") as f:
        response = requests.post(
            "https://api.x.ai/v1/stt",
            headers={"Authorization": f"Bearer {key}"},
            data=[("model", "grok-voice-transcribe-2.0"), ("format", "true")],
            files={"file": (os.path.basename(path), f, mime or "audio/ogg")},
            timeout=90,
        )
    response.raise_for_status()
    return (response.json().get("text") or "").strip()


def _openai_stt(path: str, key: str) -> str:
    client = OpenAI(api_key=key, base_url="https://api.openai.com/v1")
    with open(path, "rb") as f:
        result = client.audio.transcriptions.create(model="gpt-4o-mini-transcribe", file=f)
    return (getattr(result, "text", "") or "").strip()


def _gemini_stt(path: str, mime: str, key: str, model: str) -> str:
    with open(path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("ascii")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "contents": [{"parts": [
            {"text": "Transcribe this voice note faithfully. Preserve Tamil, English, Malay, or mixed-language wording. Return transcript only."},
            {"inlineData": {"mimeType": mime or "audio/ogg", "data": encoded}},
        ]}],
        "generationConfig": {"temperature": 0},
    }
    response = requests.post(url, headers={"x-goog-api-key": key}, json=payload, timeout=90)
    response.raise_for_status()
    data = response.json()
    return "".join(
        p.get("text", "") for p in data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    ).strip()


def transcribe_audio(media_id: str) -> str:
    row = get_media(media_id)
    if not row or row["media_type"] != "AUDIO":
        return ""
    settings = get_settings()
    requested = settings.stt_provider
    order = [requested] if requested != "auto" else ["xai", "openai", "gemini"]
    last_error = None
    for provider in order:
        try:
            if provider == "xai" and settings.xai_api_key:
                text = _xai_stt(row["local_path"], row["mime_type"], settings.xai_api_key)
            elif provider == "openai" and settings.openai_api_key:
                text = _openai_stt(row["local_path"], settings.openai_api_key)
            elif provider == "gemini" and settings.gemini_api_key:
                text = _gemini_stt(row["local_path"], row["mime_type"], settings.gemini_api_key, settings.gemini_model)
            else:
                continue
            if text:
                _update_text(media_id, "transcript_text", text)
                return text
        except Exception as exc:
            last_error = exc
    if last_error:
        raise RuntimeError(f"Voice transcription failed: {last_error}")
    raise RuntimeError("No speech-to-text provider is configured")


def _looks_like_financial_document(text: str) -> bool:
    lowered = (text or "").lower()
    markers = (
        "receipt", "payment", "paid", "amount", "total", "subtotal", "balance",
        "transfer", "duitnow", "bank", "reference", "ref no", "transaction",
        "myr", "sgd", "rm ", "invoice",
    )
    score = sum(1 for marker in markers if marker in lowered)
    has_amount = bool(re.search(r"(?:rm|myr|sgd|s\\$|\\$)?\\s*\\d+[.,]\\d{2}", lowered))
    return score >= 2 or (score >= 1 and has_amount)


def process_payload_media(payload: dict) -> tuple[list[str], list[str], list[dict]]:
    """Persist media first, cheaply extract text, and use model vision only when OCR is insufficient/non-financial."""
    media_ids: list[str] = []
    context_lines: list[str] = []
    vision_parts: list[dict] = []

    if payload.get("image_data"):
        mime = payload.get("image_mime_type") or "image/jpeg"
        mid = save_media(payload["message_id"], "IMAGE", mime, payload["image_data"])
        media_ids.append(mid)
        text = extract_text(mid)
        if text:
            context_lines.append("Local OCR from attached image:\n" + text[:12000])

        caption = (payload.get("text") or "").strip()
        # Receipts/payment slips with useful OCR stay local to save multimodal tokens.
        # General photos, unreadable receipts, or non-financial images are shown to the model.
        needs_vision = not _looks_like_financial_document(text)
        if needs_vision:
            vision_parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{payload['image_data']}"},
            })

    if payload.get("pdf_data"):
        mid = save_media(payload["message_id"], "PDF", "application/pdf", payload["pdf_data"])
        media_ids.append(mid)
        text = extract_text(mid)
        if text:
            context_lines.append("Text extracted from attached PDF:\n" + text[:12000])

    if payload.get("audio_data"):
        mid = save_media(payload["message_id"], "AUDIO", payload.get("audio_mime_type") or "audio/ogg", payload["audio_data"])
        media_ids.append(mid)
        text = transcribe_audio(mid)
        if text:
            context_lines.append("Voice-note transcript:\n" + text[:12000])

    return media_ids, context_lines, vision_parts
