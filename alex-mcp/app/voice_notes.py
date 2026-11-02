"""Opt-in, single-pass recorded-audio input for the existing Alex turn pipeline.

No translation, tool execution, or provider fallback lives here. Reservations
survive restarts: an interrupted upload is never charged/replayed automatically.
"""
from __future__ import annotations

import array
import json
import math
import os
import subprocess
import tempfile
import time
import wave

from openai import OpenAI

from config import get_settings
from db import connect
import runtime_clock

MODEL = "gpt-transcribe"
USD_PER_MINUTE = 0.0045  # Published estimate, not the account's live bill/balance.
MAX_INPUT_BYTES = 25_000_000


class VoiceNoteUnavailable(RuntimeError):
    """A stable reason code, never provider messages, credentials or transcripts."""


def _claim(media_id: str, duration: float, budget: float) -> bool:
    cost = duration / 60 * USD_PER_MINUTE
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        old = conn.execute("SELECT status FROM voice_transcription_jobs WHERE media_id=?", (media_id,)).fetchone()
        if old:
            return False
        month = runtime_clock.now_utc().strftime("%Y-%m")
        spent = conn.execute(
            "SELECT COALESCE(SUM(reserved_usd),0) FROM voice_transcription_jobs WHERE substr(created_at_utc,1,7)=?",
            (month,),
        ).fetchone()[0]
        if not math.isfinite(budget) or budget <= 0 or spent + cost > budget:
            raise VoiceNoteUnavailable("budget")
        conn.execute(
            "INSERT INTO voice_transcription_jobs(media_id,status,duration_seconds,reserved_usd,created_at_utc) VALUES(?,'STARTED',?,?,?)",
            (media_id, duration, cost, runtime_clock.now_utc().isoformat()),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def _finish(media_id: str, *, text: str = "", languages: list | None = None,
            duration: float = 0, elapsed_ms: int = 0, reason: str = "") -> None:
    meta = {
        "mode": "openai_voice_note", "model": MODEL,
        "command_execution": bool(text), "authoritative": bool(text),
        "status": "DONE" if text else "FAILED", "reason": reason,
        "languages": languages or [], "duration_seconds": duration,
        "latency_ms": elapsed_ms, "estimated_cost_usd": duration / 60 * USD_PER_MINUTE,
        # No invented numerical confidence: this endpoint supplies none.
        "confidence": "not_provided",
    }
    conn = connect()
    try:
        conn.execute(
            "UPDATE media_objects SET transcript_text=?,transcript_meta_json=? WHERE media_id=?",
            (text, json.dumps(meta, ensure_ascii=False), media_id),
        )
        conn.execute("UPDATE voice_transcription_jobs SET status=?,latency_ms=?,reason=? WHERE media_id=?",
                     (meta["status"], elapsed_ms, reason, media_id))
        conn.commit()
    finally:
        conn.close()


def transcribe(media_id: str) -> str:
    settings = get_settings()
    if not settings.voice_notes_enabled:
        raise VoiceNoteUnavailable("disabled")
    # A dedicated voice credential does not add OpenAI to the everyday brain.
    key = settings.voice_openai_api_key or settings.openai_api_key
    if not key:
        raise VoiceNoteUnavailable("not_configured")
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM media_objects WHERE media_id=? AND media_type='AUDIO'", (media_id,)).fetchone()
        job = conn.execute("SELECT status FROM voice_transcription_jobs WHERE media_id=?", (media_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise VoiceNoteUnavailable("missing_audio")
    if job:
        if job["status"] == "DONE" and row["transcript_text"]:
            return row["transcript_text"]
        raise VoiceNoteUnavailable("previous_attempt")
    path = row["local_path"]
    if not os.path.isfile(path) or not 0 < os.path.getsize(path) <= MAX_INPUT_BYTES:
        raise VoiceNoteUnavailable("invalid_audio")
    started = time.monotonic()
    duration = 0.0
    claimed = False
    try:
        with tempfile.TemporaryDirectory(prefix="alex-voice-") as tmp:
            wav_path = os.path.join(tmp, "voice.wav")
            # WhatsApp Ogg/Opus is converted to the API's documented WAV format.
            # Limit decode length before loading PCM, without silently clipping.
            subprocess.run([
                "ffmpeg", "-nostdin", "-v", "error", "-i", path,
                "-t", str(settings.voice_max_seconds + 1), "-ac", "1", "-ar", "16000",
                "-c:a", "pcm_s16le", wav_path,
            ], check=True, capture_output=True, timeout=15)
            with wave.open(wav_path, "rb") as audio:
                duration = audio.getnframes() / audio.getframerate()
                if duration <= 0 or duration > settings.voice_max_seconds:
                    raise VoiceNoteUnavailable("too_long")
                samples = array.array("h", audio.readframes(audio.getnframes()))
                if not samples or max(abs(x) for x in samples) < 16:
                    raise VoiceNoteUnavailable("silent")
            if not _claim(media_id, duration, settings.voice_monthly_budget_usd):
                raise VoiceNoteUnavailable("previous_attempt")
            claimed = True
            with OpenAI(api_key=key, base_url="https://api.openai.com/v1",
                        timeout=25.0, max_retries=0) as client:
                with open(wav_path, "rb") as audio:
                    result = client.audio.transcriptions.create(
                        model=MODEL, file=audio,
                        prompt="A household WhatsApp voice note. Preserve the spoken languages and code-switching, names, amounts and negations. Do not translate or answer the speaker.",
                        extra_body={"languages": settings.voice_language_hints,
                                    "keywords": settings.voice_keywords},
                    )
            text = str(getattr(result, "text", "") or "").strip()
            if not text or len(text) > 12000 or any(
                marker in text.casefold() for marker in ("[inaudible]", "[unclear]", "[unintelligible]")
            ):
                raise VoiceNoteUnavailable("unclear")
            raw_languages = getattr(result, "languages", None) or []
            languages = []
            for item in raw_languages:
                code = item.get("code") if isinstance(item, dict) else getattr(item, "code", None)
                if isinstance(code, str) and len(code) <= 12:
                    languages.append(code)
            _finish(media_id, text=text, languages=languages, duration=duration,
                    elapsed_ms=int((time.monotonic() - started) * 1000))
            return text
    except Exception as exc:
        if isinstance(exc, VoiceNoteUnavailable):
            reason = str(exc)
        else:
            reason = {401: "provider_auth", 403: "provider_auth", 404: "model_unavailable",
                      429: "quota_or_rate_limit"}.get(getattr(exc, "status_code", None), "provider_or_audio_error")
        if claimed:
            _finish(media_id, duration=duration, elapsed_ms=int((time.monotonic() - started) * 1000), reason=reason)
        raise VoiceNoteUnavailable(reason) from None


def pending_reply(reason: str) -> str:
    if reason == "budget":
        return "Voice transcription has reached its configured monthly limit. I saved the original; you can still type the request."
    if reason == "too_long":
        return "That voice note is longer than Alex’s configured limit. I saved the original; please send a shorter note or type the request."
    if reason in {"unclear", "silent", "invalid_audio"}:
        return "I couldn’t hear that voice note clearly enough. I saved the original; please resend it or type the request."
    return "I couldn’t process this voice note right now. I saved the original for review; please type the request or send a new note later."


def usage_summary() -> dict:
    """Operational counts/cost only: no transcripts, identities or credentials."""
    settings = get_settings()
    month = runtime_clock.now_utc().strftime("%Y-%m")
    conn = connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS attempts,COALESCE(SUM(reserved_usd),0) AS reserved_usd,"
            "COALESCE(SUM(duration_seconds),0) AS audio_seconds,"
            "COALESCE(AVG(CASE WHEN status='DONE' THEN latency_ms END),0) AS average_latency_ms "
            "FROM voice_transcription_jobs WHERE substr(created_at_utc,1,7)=?", (month,),
        ).fetchone()
        return {"enabled": settings.voice_notes_enabled,
                "credential_configured": bool(settings.voice_openai_api_key or settings.openai_api_key),
                "model": MODEL, "month": month, "budget_usd": settings.voice_monthly_budget_usd,
                "estimate_not_live_balance": True, **dict(row)}
    finally:
        conn.close()
