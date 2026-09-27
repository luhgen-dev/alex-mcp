from __future__ import annotations

import json
import os
from dataclasses import dataclass

OPTIONS_PATH = os.environ.get("ALEX_OPTIONS_PATH", "/data/options.json")
DATA_DIR = os.environ.get("ALEX_DATA_DIR", "/data")


@dataclass(frozen=True)
class Settings:
    ai_provider: str = "grok"
    grok_model: str = "grok-4.7"
    gemini_model: str = "gemini-3.8-flash"
    openai_model: str = "gpt-6-luna"
    xai_api_key: str = ""
    gemini_api_key: str = ""
    openai_api_key: str = ""
    husband_phone: str = ""
    wife_phone: str = ""
    timezone: str = "Asia/Kuala_Lumpur"
    stt_provider: str = "auto"
    ocr_enabled: bool = True
    context_turns: int = 8
    reasoning_effort: str = "medium"

    @property
    def model(self) -> str:
        return {
            "grok": self.grok_model,
            "gemini": self.gemini_model,
            "openai": self.openai_model,
        }.get(self.ai_provider, self.grok_model)

    @property
    def api_key(self) -> str:
        return {
            "grok": self.xai_api_key,
            "gemini": self.gemini_api_key,
            "openai": self.openai_api_key,
        }.get(self.ai_provider, "")

    @property
    def base_url(self) -> str:
        return {
            "grok": "https://api.x.ai/v1",
            "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
            "openai": "https://api.openai.com/v1",
        }.get(self.ai_provider, "https://api.x.ai/v1")


def _read_options() -> dict:
    try:
        with open(OPTIONS_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
            return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def get_settings() -> Settings:
    raw = _read_options()
    allowed = Settings.__dataclass_fields__.keys()
    clean = {k: raw[k] for k in allowed if k in raw}
    try:
        clean["context_turns"] = max(2, min(20, int(clean.get("context_turns", 8))))
    except (TypeError, ValueError):
        clean["context_turns"] = 8
    return Settings(**clean)


def normalize_phone(value: str) -> str:
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    return f"+{digits}" if digits else ""
