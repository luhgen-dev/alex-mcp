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


def _update_transcript(media_id: str, text: str, meta: dict | None = None) -> None:
    conn = connect()
    try:
        conn.execute(
            """UPDATE media_objects
               SET transcript_text=?,transcript_meta_json=?
               WHERE media_id=?""",
            (
                (text or "")[:50000],
                json.dumps(meta or {}, ensure_ascii=False, sort_keys=True)[:12000],
                media_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _transcript_meta(candidates: list[tuple[str, str]], chosen: str,
                     *, mode: str, cloud_rescue: bool = False) -> dict:
    return {
        "mode": mode,
        "chosen": chosen,
        "cloud_rescue": bool(cloud_rescue),
        "candidates": [
            {
                "label": label,
                "score": _voice_intent_score(text),
                "chars": len(text or ""),
            }
            for label, text in candidates
            if (text or "").strip()
        ],
    }


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


def _local_whisper(path: str, model_name: str, language: str = "auto") -> str:
    """Run one local Whisper pass.

    Short household commands are unusually vulnerable to auto-language drift
    (for example an English command being decoded as Malay/Indonesian).  The
    caller may therefore request a second English/Tamil pass only when the
    first transcript is not trustworthy.  Normal voice notes still pay for one
    local pass only.
    """
    model_path = _ensure_whisper_model(model_name)
    lang = (language or "auto").strip().lower()
    if lang not in {"auto", "en", "ta"}:
        raise ValueError("Unsupported Whisper language hint")
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


_VOICE_ACTION_PATTERNS = (
    r"\b(?:add|put|remove|delete|mark|bought|buy|shopping|grocery)\b",
    r"\b(?:remind|reminder|notify|tomorrow|today|tonight)\b",
    r"\b(?:spent|paid|expense|receipt|rm|myr|sgd|payment)\b",
    r"\b(?:save|remember|saved|find|show|open)\b",
    r"\b(?:diary|appointment|meeting|agenda|plan|task)\b",
    r"\b(?:goal|cash|salary|bonus|overtime|\bot\b|reserve|bill)\b",
    r"\b(?:turn|switch|light|fan|air conditioner|\bac\b|home)\b",
    r"\b(?:what|when|where|how|list|check|tell)\b",
    # Malay household-action vocabulary. Malay is valid user input; only
    # assistant-like apology boilerplate is penalized below.
    r"\b(?:tambah|letak|buang|padam|beli|dibeli|senarai|barang)\b",
    r"\b(?:ingatkan|esok|hari ini|malam ini|bayar|bil|belanja|resit)\b",
    r"\b(?:simpan|ingat|cari|tunjuk|buka|lampu|kipas|tutup|hidupkan)\b",
)


def _voice_intent_score(text: str) -> int:
    """Score whether a transcript resembles a useful household utterance.

    This is not a language classifier and never decides the action.  It is only
    used to choose between ASR candidates for the same audio.
    """
    value = (text or "").strip()
    if not value:
        return -100
    low = value.casefold()
    score = 0
    score += min(4, sum(1 for pattern in _VOICE_ACTION_PATTERNS if re.search(pattern, low)))
    if re.search(r"[\u0B80-\u0BFF]", value):
        score += 3
    if re.search(r"\b(?:நினைவூட்டு|சேமி|காட்டு|வாங்க|பட்டியல்)\b", value):
        score += 2
    # Strong signals of the exact wrong-language drift observed in live smoke
    # tests. Malay itself remains supported input; these phrases are penalized
    # only when competing ASR candidates exist for the same audio.
    if re.search(r"\b(?:mohon maaf|silakan|apakah anda|bermaksud|sebelumnya)\b", low):
        score -= 4
    words = re.findall(r"[\w'-]+", value, re.UNICODE)
    if 2 <= len(words) <= 40:
        score += 1
    return score


def _choose_voice_transcript(candidates: list[tuple[str, str]]) -> tuple[str, str]:
    """Choose the most actionable non-empty transcript deterministically."""
    usable = [(label, (text or "").strip()) for label, text in candidates if (text or "").strip()]
    if not usable:
        return "", ""
    ranked = sorted(
        usable,
        key=lambda row: (_voice_intent_score(row[1]), min(len(row[1]), 240)),
        reverse=True,
    )
    return ranked[0]


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
    last_error = None

    # Explicit STT modes remain deterministic and do exactly what the owner
    # selected. Auto is more defensive: use one local multilingual pass first,
    # then locally verify a suspicious short command before spending any cloud
    # transcription call.
    if requested != "auto":
        try:
            if requested == "local_whisper":
                text = _local_whisper(
                    row["local_path"], settings.whisper_model, "auto"
                )
            elif requested == "xai" and settings.xai_api_key:
                text = _xai_stt(
                    row["local_path"], row["mime_type"], settings.xai_api_key
                )
            elif requested == "openai" and settings.openai_api_key:
                text = _openai_stt(
                    row["local_path"], settings.openai_api_key
                )
            elif requested == "gemini" and settings.gemini_api_key:
                text = _gemini_stt(
                    row["local_path"], row["mime_type"],
                    settings.gemini_api_key, settings.gemini_model,
                )
            else:
                text = ""
            if text:
                _update_transcript(
                    media_id, text,
                    _transcript_meta(
                        [(requested, text)], requested,
                        mode="explicit", cloud_rescue=requested != "local_whisper",
                    ),
                )
                return text
        except Exception as exc:
            last_error = exc
        if last_error:
            raise RuntimeError(f"Voice transcription failed: {last_error}")
        raise RuntimeError("No speech-to-text provider is configured")

    candidates: list[tuple[str, str]] = []
    try:
        auto_text = _local_whisper(
            row["local_path"], settings.whisper_model, "auto"
        )
        if auto_text:
            candidates.append(("local_auto", auto_text))
            # A clearly actionable transcript is accepted immediately: this is
            # the zero-token fast path for the overwhelming majority of notes.
            if _voice_intent_score(auto_text) >= 2:
                _update_transcript(
                    media_id, auto_text,
                    _transcript_meta(
                        candidates, "local_auto", mode="auto_fast"
                    ),
                )
                return auto_text
    except Exception as exc:
        last_error = exc

    # Verification passes are local and only happen when auto-language decoding
    # did not look like a coherent household turn. English and Tamil cover the
    # owner's actual voice use, including many Tanglish utterances.
    for lang in ("en", "ta"):
        try:
            candidate = _local_whisper(
                row["local_path"], settings.whisper_model, lang
            )
            if candidate:
                candidates.append((f"local_{lang}", candidate))
        except Exception as exc:
            last_error = exc

    label, best = _choose_voice_transcript(candidates)
    if best and _voice_intent_score(best) >= 2:
        _update_transcript(
            media_id, best,
            _transcript_meta(
                candidates, label, mode="auto_local_verify"
            ),
        )
        return best

    # Only now use a configured cloud STT as a rescue. This is not a required
    # paid path: if no cloud key exists, the best local transcript is returned.
    for provider in ("gemini", "openai", "xai"):
        try:
            if provider == "gemini" and settings.gemini_api_key:
                text = _gemini_stt(
                    row["local_path"], row["mime_type"],
                    settings.gemini_api_key, settings.gemini_model,
                )
            elif provider == "openai" and settings.openai_api_key:
                text = _openai_stt(
                    row["local_path"], settings.openai_api_key
                )
            elif provider == "xai" and settings.xai_api_key:
                text = _xai_stt(
                    row["local_path"], row["mime_type"], settings.xai_api_key
                )
            else:
                continue
            if text:
                candidates.append((provider, text))
                chosen_label, chosen = _choose_voice_transcript(candidates)
                if chosen:
                    _update_transcript(
                        media_id, chosen,
                        _transcript_meta(
                            candidates, chosen_label,
                            mode="auto_cloud_rescue", cloud_rescue=True,
                        ),
                    )
                    return chosen
        except Exception as exc:
            last_error = exc

    chosen_label, best = _choose_voice_transcript(candidates)
    if best:
        _update_transcript(
            media_id, best,
            _transcript_meta(
                candidates, chosen_label, mode="auto_local_fallback"
            ),
        )
        return best
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
