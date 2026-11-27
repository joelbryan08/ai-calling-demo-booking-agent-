import os
from dataclasses import dataclass
import sys
from pathlib import Path
from dotenv import dotenv_values, load_dotenv

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"   # the .env next to the app folder
_TESTING = "pytest" in sys.modules
if not _TESTING:
    print(f"[config] reading {ENV_PATH}" + ("" if ENV_PATH.exists() else "  (FILE NOT FOUND)"))
    for _k, _v in (dotenv_values(ENV_PATH) if ENV_PATH.exists() else {}).items():
        if _k in os.environ and os.environ[_k] != _v:
            print(f"[config] NOTE: {_k} is also set in your terminal or Windows environment; the value in .env is used.")
load_dotenv(ENV_PATH, override=not _TESTING)       # .env is the single source of truth
if not _TESTING:
    print(f"[config] public address in use: {os.getenv('PUBLIC_BASE_URL') or '(not set)'}")


def _csv(name: str, default: str) -> tuple:
    return tuple(x.strip() for x in os.getenv(name, default).split(",") if x.strip())


@dataclass(frozen=True)
class Settings:
    provider: str = os.getenv("TELEPHONY_PROVIDER", "fake").lower()
    plivo_auth_id: str = os.getenv("PLIVO_AUTH_ID", "")
    plivo_auth_token: str = os.getenv("PLIVO_AUTH_TOKEN", "")
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    webhook_secret: str = os.getenv("WEBHOOK_SECRET", "dev-secret")
    default_caller_number: str = os.getenv("DEFAULT_CALLER_NUMBER", "")
    dialer_enabled: bool = os.getenv("DIALER_ENABLED", "true").lower() == "true"
    dialer_interval: float = float(os.getenv("DIALER_INTERVAL_SECONDS", "5"))
    admin_password: str = os.getenv("ADMIN_PASSWORD", "")
    stale_call_minutes: int = int(os.getenv("STALE_CALL_MINUTES", "30"))

    # --- agent tools: calendar scheduling + sheet logging
    calendar_backend: str = os.getenv("CALENDAR_BACKEND", "fake").lower()  # fake | google
    tools_api_key: str = os.getenv("TOOLS_API_KEY", "dev-tools-key")
    company_name: str = os.getenv("COMPANY_NAME", "Our company")
    presenters: tuple = tuple(e.lower() for e in _csv(
        "PRESENTERS", "presenter1@example.com,presenter2@example.com,presenter3@example.com"))
    presenter_timezone: str = os.getenv("PRESENTER_TIMEZONE", "Asia/Kolkata")
    work_start: str = os.getenv("WORK_START", "10:00")
    work_end: str = os.getenv("WORK_END", "18:00")
    work_days: tuple = tuple(int(x) for x in _csv("WORK_DAYS", "0,1,2,3,4"))  # 0 = Monday
    demo_minutes: int = int(os.getenv("DEMO_MINUTES", "20"))
    slot_step_minutes: int = int(os.getenv("SLOT_STEP_MINUTES", "30"))
    buffer_minutes: int = int(os.getenv("BUFFER_MINUTES", "15"))
    max_demos_per_day: int = int(os.getenv("MAX_DEMOS_PER_DAY", "3"))
    min_notice_hours: int = int(os.getenv("MIN_NOTICE_HOURS", "3"))
    max_lookahead_days: int = int(os.getenv("MAX_LOOKAHEAD_DAYS", "14"))
    google_mode: str = os.getenv("GOOGLE_MODE", "auto").lower()          # auto | delegated | shared
    meeting_links: tuple = tuple(p.split("=", 1) for p in os.getenv("MEETING_LINKS", "").split(";") if "=" in p)  # email=url;email=url
    default_meeting_link: str = os.getenv("DEFAULT_MEETING_LINK", "")
    google_sa_file: str = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "")
    google_sa_json: str = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    sheet_id: str = os.getenv("SHEET_ID", "")
    sheet_tab: str = os.getenv("SHEET_TAB", "Calls")

    # --- live voice agent (Plivo audio stream <-> Sarvam STT / LLM / TTS)
    sarvam_api_key: str = os.getenv("SARVAM_API_KEY", "")
    stt_model: str = os.getenv("SARVAM_STT_MODEL", "saaras:v4")
    llm_model: str = os.getenv("SARVAM_LLM_MODEL", "sarvam-105b")
    tts_model: str = os.getenv("SARVAM_TTS_MODEL", "bulbul:v4-flash")
    tts_speaker: str = os.getenv("TTS_SPEAKER", "simran_en_customer")  # must exist for the chosen TTS model
    agent_language: str = os.getenv("AGENT_LANGUAGE", "en-IN")         # STT language, or "auto"
    tts_language: str = os.getenv("TTS_LANGUAGE", "en-IN")
    tts_speaker_hi: str = os.getenv("TTS_SPEAKER_HI", "")               # optional Hindi voice
    record_calls: bool = os.getenv("RECORD_CALLS", "false").lower() in ("1", "true", "yes")
    recordings_dir: str = os.getenv("RECORDINGS_DIR", "recordings")
    recording_notice: str = os.getenv("RECORDING_NOTICE", "This call may be recorded.")
    tts_codec: str = os.getenv("TTS_CODEC", "mulaw")                    # falls back to linear16 automatically
    llm_reasoning_effort: str = os.getenv("LLM_REASONING_EFFORT", "")   # "" = off (fastest); low/medium/high
    llm_max_tokens: int = int(os.getenv("LLM_MAX_TOKENS", "1500"))
    llm_temperature: float = float(os.getenv("LLM_TEMPERATURE", "0.4"))
    stt_silence_ms: int = int(os.getenv("STT_SILENCE_MS", "700"))
    max_call_seconds: int = int(os.getenv("MAX_CALL_SECONDS", "420"))
    agent_name: str = os.getenv("AGENT_NAME", "Diya")
    product_name: str = os.getenv("PRODUCT_NAME", "our product")
    problem_area: str = os.getenv("PROBLEM_AREA", "handling sales leads and follow-ups")
    product_pitch: str = os.getenv("PRODUCT_PITCH", "Dexkor automates your customer follow-ups, so fewer customers slip away and support gets easier.")


settings = Settings()
