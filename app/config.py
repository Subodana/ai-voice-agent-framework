"""Environment settings. Secrets live only in .env / the host's secret store, never in profiles."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _list(name: str) -> list[str]:
    return [v.strip() for v in os.getenv(name, "").split(",") if v.strip()]


@dataclass(frozen=True)
class Settings:
    claude_model: str = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
    # low = fastest replies on a phone line; raise to medium for harder conversations
    claude_effort: str = os.getenv("CLAUDE_EFFORT", "low")
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    twilio_account_sid: str = os.getenv("TWILIO_ACCOUNT_SID", "")
    twilio_auth_token: str = os.getenv("TWILIO_AUTH_TOKEN", "")
    twilio_from_number: str = os.getenv("TWILIO_FROM_NUMBER", "")
    # Real outbound dials are refused unless the number is on this list (approved test numbers only).
    outbound_test_numbers: list = field(default_factory=lambda: _list("OUTBOUND_TEST_NUMBERS"))
    tts_provider: str = os.getenv("TTS_PROVIDER", "ElevenLabs")
    tts_voice: str = os.getenv("TTS_VOICE", "")
    admin_token: str = os.getenv("ADMIN_TOKEN", "")
    # Where this server reaches itself; webhook URLs starting with "/" (the built-in mock CRM) resolve against it.
    self_url: str = os.getenv("SELF_URL", "http://127.0.0.1:8000").rstrip("/")
    sqlite_path: str = os.getenv("SQLITE_PATH", str(ROOT / "data" / "voice.db"))
    profiles_dir: str = os.getenv("PROFILES_DIR", str(ROOT / "profiles"))
    default_profile: str = os.getenv("DEFAULT_PROFILE", "bright-smile-dental")
    webhook_max_attempts: int = int(os.getenv("WEBHOOK_MAX_ATTEMPTS", "6"))


settings = Settings()
