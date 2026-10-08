from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

OPTIONS_PATH = os.environ.get("ALEX_OPTIONS_PATH", "/data/options.json")
DATA_DIR = os.environ.get("ALEX_DATA_DIR", "/data")


@dataclass(frozen=True)
class Settings:
    ai_provider: str = "auto"
    grok_model: str = "grok-4.7"
    gemini_model: str = "gemini-3.8-flash"
    gemini_lite_model: str = "gemini-3.1-flash-lite"
    openai_model: str = "gpt-5.6-luna"
    xai_api_key: str = ""
    gemini_api_key: str = ""
    openai_api_key: str = ""
    husband_phone: str = ""
    wife_phone: str = ""
    husband_name: str = "Luhgen"
    wife_name: str = "Priya"
    timezone: str = "Asia/Kuala_Lumpur"
    stt_provider: str = "auto"
    cloud_stt_rescue_enabled: bool = False
    whisper_model: str = "base"
    ocr_enabled: bool = True
    context_turns: int = 8
    reasoning_effort: str = "low"
    monthly_ai_budget_usd: float = 0.0
    auto_grok_fallback_budget_usd: float = 0.50
    budget_safety_multiplier: float = 2.0
    # v0.5.33: the owner's ChatGPT subscription via "Sign in with ChatGPT".
    # off = unused; shadow_only = only the log-only shadow router uses it;
    # primary = ChatGPT answers text turns first, existing providers fall back.
    chatgpt_plan_mode: str = "off"
    chatgpt_plan_model: str = ""
    ha_notify_devices: list[dict] = field(default_factory=list)

    def model_for(self, provider: str, *, lite: bool = False) -> str:
        if provider == "gemini" and lite:
            return self.gemini_lite_model
        return {
            "grok": self.grok_model,
            "gemini": self.gemini_model,
            "openai": self.openai_model,
        }.get(provider, self.grok_model)

    def api_key_for(self, provider: str) -> str:
        return {
            "grok": self.xai_api_key,
            "gemini": self.gemini_api_key,
            "openai": self.openai_api_key,
        }.get(provider, "")

    def base_url_for(self, provider: str) -> str:
        return {
            "grok": "https://api.x.ai/v1",
            "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
            "openai": "https://api.openai.com/v1",
        }.get(provider, "https://api.x.ai/v1")

    @property
    def model(self) -> str:
        if self.ai_provider == "auto":
            return self.gemini_lite_model if self.gemini_api_key else (
                self.grok_model if self.xai_api_key else self.openai_model
            )
        return self.model_for(self.ai_provider)

    @property
    def api_key(self) -> str:
        if self.ai_provider == "auto":
            return self.gemini_api_key or self.xai_api_key or self.openai_api_key
        return self.api_key_for(self.ai_provider)

    @property
    def base_url(self) -> str:
        if self.ai_provider == "auto":
            provider = "gemini" if self.gemini_api_key else (
                "grok" if self.xai_api_key else "openai"
            )
            return self.base_url_for(provider)
        return self.base_url_for(self.ai_provider)


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
    try:
        clean["monthly_ai_budget_usd"] = max(0.0, float(clean.get("monthly_ai_budget_usd", 0.0)))
    except (TypeError, ValueError):
        clean["monthly_ai_budget_usd"] = 0.0
    try:
        clean["auto_grok_fallback_budget_usd"] = max(
            0.0, float(clean.get("auto_grok_fallback_budget_usd", 0.50))
        )
    except (TypeError, ValueError):
        clean["auto_grok_fallback_budget_usd"] = 0.50
    try:
        clean["budget_safety_multiplier"] = max(1.0, min(10.0, float(clean.get("budget_safety_multiplier", 2.0))))
    except (TypeError, ValueError):
        clean["budget_safety_multiplier"] = 2.0
    plan_mode = str(clean.get("chatgpt_plan_mode", "off") or "off").strip().lower()
    clean["chatgpt_plan_mode"] = (
        plan_mode if plan_mode in {"off", "shadow_only", "primary"} else "off"
    )
    clean["chatgpt_plan_model"] = str(clean.get("chatgpt_plan_model", "") or "").strip()
    devices = clean.get("ha_notify_devices", [])
    if not isinstance(devices, list):
        devices = []
    clean["ha_notify_devices"] = [
        item for item in devices if isinstance(item, dict)
    ][:10]
    return Settings(**clean)


def normalize_phone(value: str) -> str:
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    return f"+{digits}" if digits else ""
