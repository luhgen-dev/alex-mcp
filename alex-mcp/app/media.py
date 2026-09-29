from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import subprocess
import tempfile
import uuid

import requests
from openai import OpenAI

from config import DATA_DIR, get_settings
from db import connect

MEDIA_DIR = os.path.join(DATA_DIR, "media")
MODEL_DIR = os.path.join(DATA_DIR, "models")
WHISPER_MODELS = {"tiny", "base", "small"}
WHISPER_MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{model}.bin"


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
                ["tesseract", path, "stdout", "-l", "eng+msa+tam", "--psm", "6"],
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

        # Image-only/scanned PDFs have no text layer. Fall back to local OCR
        # rather than paying the conversational model to inspect the whole PDF.
        if not text and settings.ocr_enabled:
            pages = []
            try:
                with tempfile.TemporaryDirectory(prefix="alex-pdf-") as tmp:
                    prefix = os.path.join(tmp, "page")
                    subprocess.run(
                        ["pdftoppm", "-f", "1", "-l", "5", "-jpeg", "-r", "180", path, prefix],
                        capture_output=True, text=True, timeout=45, check=True,
                    )
                    for name in sorted(os.listdir(tmp)):
                        if not name.lower().endswith((".jpg", ".jpeg")):
                            continue
                        page_path = os.path.join(tmp, name)
                        ocr = subprocess.run(
                            ["tesseract", page_path, "stdout", "-l", "eng", "--psm", "6"],
                            capture_output=True, text=True, timeout=30,
                        )
                        if (ocr.stdout or "").strip():
                            pages.append((ocr.stdout or "").strip())
                text = "\n\n".join(pages)
            except Exception:
                text = ""

        _update_text(media_id, "ocr_text", text)
        return text
    return ""


def _ensure_whisper_model(model_name: str) -> str:
    model = (model_name or "base").strip().lower()
    if model not in WHISPER_MODELS:
        raise ValueError(f"Unsupported local Whisper model: {model}")
    os.makedirs(MODEL_DIR, exist_ok=True)
    path = os.path.join(MODEL_DIR, f"ggml-{model}.bin")
    if os.path.isfile(path) and os.path.getsize(path) > 1024 * 1024:
        return path

    part = path + ".part"
    try:
        with requests.get(
            WHISPER_MODEL_URL.format(model=model),
            stream=True,
            timeout=(15, 180),
            allow_redirects=True,
        ) as response:
            response.raise_for_status()
            with open(part, "wb") as out:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        out.write(chunk)
        if os.path.getsize(part) < 1024 * 1024:
            raise RuntimeError("Downloaded Whisper model is unexpectedly small")
        os.replace(part, path)
        return path
    except Exception:
        try:
            if os.path.exists(part):
                os.remove(part)
        except OSError:
            pass
        raise


_VOICE_INTENT_WORDS = {
    "add", "buy", "bought", "shopping", "list", "mark", "remove", "delete",
    "remind", "reminder", "save", "remember", "show", "find", "receipt",
    "expense", "spent", "paid", "payment", "task", "plan", "diary",
    "appointment", "meeting", "goal", "cash", "money", "turn", "switch",
    "light", "fan", "what", "when", "where", "how", "check", "tell",
}
_WHISPER_HALLUCINATION_MARKERS = (
    "thank you for watching", "thanks for watching", "subscribe to",
    "subtitles by", "amara.org", "www.", "captioned by",
)


def _transcript_command_score(text: str) -> int:
    """Cheap signal used only to choose between local Whisper hypotheses."""
    value = str(text or "").strip()
    if not value:
        return -100
    low = value.casefold()
    score = 0
    if any(marker in low for marker in _WHISPER_HALLUCINATION_MARKERS):
        score -= 20
    words = re.findall(r"[a-z]+", low)
    score += sum(3 for word in words if word in _VOICE_INTENT_WORDS)
    if re.search(r"[\u0B80-\u0BFF]", value):
        # Tamil script is a valid household-language hypothesis, not noise.
        score += 6
    if len(set(words)) <= 2 and len(words) >= 8:
        score -= 8
    if len(value) >= 3:
        score += 1
    return score


def _local_transcript_suspicious(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return True
    low = value.casefold()
    if any(marker in low for marker in _WHISPER_HALLUCINATION_MARKERS):
        return True
    words = re.findall(r"[a-z]+", low)
    if len(words) >= 8 and len(set(words)) <= 2:
        return True
    # A longer Latin-script turn with no household/query signal gets one
    # second local pass forced to English.  We still keep the original unless
    # the retry is materially stronger, so Malay/Tanglish small talk is not
    # silently rewritten.
    return (
        not re.search(r"[\u0B80-\u0BFF]", value)
        and len(words) >= 4
        and _transcript_command_score(value) <= 1
    )


def _local_whisper(path: str, model_name: str, language: str = "auto") -> str:
    model_path = _ensure_whisper_model(model_name)
    lang = str(language or "auto").strip().lower()
    if lang not in {"auto", "en", "ta", "ms"}:
        lang = "auto"
    with tempfile.TemporaryDirectory(prefix="alex-whisper-") as tmp:
        wav_path = os.path.join(tmp, "voice.wav")
        prefix = os.path.join(tmp, "transcript")
        subprocess.run(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", path,
             "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", wav_path],
            capture_output=True, text=True, timeout=90, check=True,
        )
        subprocess.run(
            ["whisper-cli", "-m", model_path, "-f", wav_path, "-l", lang,
             "-nt", "-otxt", "-of", prefix, "-np"],
            capture_output=True, text=True, timeout=180, check=True,
        )
        output_path = prefix + ".txt"
        if not os.path.isfile(output_path):
            raise RuntimeError("Local Whisper did not produce a transcript")
        with open(output_path, "r", encoding="utf-8", errors="replace") as transcript:
            return transcript.read().strip()


def _best_local_whisper(path: str, model_name: str) -> tuple[str, bool]:
    """Return best local hypothesis and whether it remains obviously unsafe.

    Real smoke tests showed that a non-empty auto-language transcript can still
    be a confident wrong-language hallucination.  Non-empty is therefore no
    longer treated as sufficient evidence.
    """
    primary = _local_whisper(path, model_name, "auto")
    best = primary
    if _local_transcript_suspicious(primary):
        try:
            english = _local_whisper(path, model_name, "en")
            if _transcript_command_score(english) >= _transcript_command_score(primary) + 2:
                best = english
        except Exception:
            pass
    obvious_junk = (
        not best.strip()
        or any(m in best.casefold() for m in _WHISPER_HALLUCINATION_MARKERS)
        or (
            len(re.findall(r"[a-z]+", best.casefold())) >= 8
            and len(set(re.findall(r"[a-z]+", best.casefold()))) <= 2
        )
    )
    return best.strip(), obvious_junk


def _xai_stt(path: str, mime: str, key: str) -> str:
    with open(path, "rb") as f:
        response = requests.post(
            "https://api.x.ai/v1/stt",
            headers={"Authorization": f"Bearer {key}"},
            data=[("model", "grok-voice-transcribe-2.0")],
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
    # Local multilingual Whisper is first by default: it keeps voice-note
    # transcription off the conversational AI bill and supports Tamil.
    order = [requested] if requested != "auto" else ["local_whisper", "gemini", "openai", "xai"]
    last_error = None
    best_local = ""
    for provider in order:
        try:
            if provider == "local_whisper":
                text, obvious_junk = _best_local_whisper(
                    row["local_path"], settings.whisper_model
                )
                best_local = text
                # Explicit local_whisper means local-only by owner choice.
                # Auto mode may continue to a configured cloud STT only when
                # the local output is obvious hallucinated/repetitive junk.
                if text and (requested != "auto" or not obvious_junk):
                    _update_text(media_id, "transcript_text", text)
                    return text
                if requested == "auto":
                    continue
            elif provider == "xai" and settings.xai_api_key:
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
    # If cloud fallbacks were unavailable but local Whisper produced something,
    # return that evidence rather than pretending transcription did not happen.
    # The conversation model receives a strong voice/source hint and may ask one
    # clarification instead of a false successful mutation.
    if best_local:
        _update_text(media_id, "transcript_text", best_local)
        return best_local
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


def _ocr_is_sufficient_financial(text: str) -> bool:
    """Skip model vision only when local OCR contains enough financial evidence to reason safely."""
    if not _looks_like_financial_document(text):
        return False
    lowered = (text or "").lower()
    has_amount = bool(re.search(r"(?:rm|myr|sgd|s\\$|\\$)?\\s*\\d+[.,]\\d{2}", lowered))
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    anchors = sum(1 for marker in (
        "total", "amount", "payment", "reference", "transaction", "bank", "merchant", "invoice"
    ) if marker in lowered)
    return has_amount and len((text or "").strip()) >= 40 and (len(lines) >= 3 or anchors >= 3)


VOICE_TRANSCRIPT_PREFIX = "Voice-note transcript:\n"


def split_voice_transcript(context_lines: list[str]) -> tuple[str, list[str]]:
    """Separate the user's own voice transcript from untrusted document text.

    v0.4.4: a voice note is the user speaking, so its transcript becomes the
    turn's trusted text (same routing/tools as typed text). OCR/PDF lines stay
    in the document context and never count as user intent.
    """
    transcript_parts: list[str] = []
    documents: list[str] = []
    for line in context_lines or []:
        if isinstance(line, str) and line.startswith(VOICE_TRANSCRIPT_PREFIX):
            part = line[len(VOICE_TRANSCRIPT_PREFIX):].strip()
            if part:
                transcript_parts.append(part)
        else:
            documents.append(line)
    return "\n".join(transcript_parts).strip(), documents


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
        needs_vision = not _ocr_is_sufficient_financial(text)
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
            context_lines.append(VOICE_TRANSCRIPT_PREFIX + text[:12000])

    return media_ids, context_lines, vision_parts
