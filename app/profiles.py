"""Business profiles: everything client-specific lives in profiles/<id>.json, validated here.

Adding a client = copying a profile file and editing it. No core code changes.
"""

import json
import re
from datetime import datetime, time
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
PROFILE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}$")


class TransferTarget(BaseModel):
    name: str
    phone: str
    # Only transfer while the business is open; otherwise take a message.
    business_hours_only: bool = True


class QualificationQuestion(BaseModel):
    id: str
    question: str
    type: Literal["yes_no", "number", "text", "choice"] = "text"
    choices: list[str] = []


class QualificationRule(BaseModel):
    field: str
    op: Literal["eq", "neq", "gte", "lte", "in", "not_in"]
    value: str | float | bool | list[str]
    reason: str  # spoken-safe explanation used in the CRM, never read to the caller verbatim


class Qualification(BaseModel):
    questions: list[QualificationQuestion] = []
    rules: list[QualificationRule] = []
    on_qualified: Literal["book", "transfer"] = "book"


class CalendarConfig(BaseModel):
    provider: Literal["local", "calcom"] = "local"
    appointment_type: str = "Appointment"
    duration_minutes: int = 30
    days_ahead: int = 7
    booking_link: str = ""  # sent in follow-ups if live booking fails
    # Cal.com only: credentials come from the named env var, never from this file.
    event_type_id: int | None = None
    api_key_env: str = "CALCOM_API_KEY"


class CRMConfig(BaseModel):
    provider: Literal["webhook", "none"] = "webhook"
    webhook_url: str = ""
    secret_env: str = ""  # env var holding the HMAC signing secret


class OutboundConfig(BaseModel):
    enabled: bool = True
    opening_line: str
    # Consent types that make a lead eligible for an AI call, e.g. "express_written".
    allowed_consent: list[str] = ["express_written"]
    calling_days: list[str] = ["mon", "tue", "wed", "thu", "fri"]
    calling_window: tuple[str, str] = ("09:00", "20:00")  # in the lead's local time
    max_attempts: int = 3
    retry_after_minutes: list[int] = [60, 240, 1440]
    caller_id: str = ""  # Twilio number for this client; falls back to TWILIO_FROM_NUMBER


class Profile(BaseModel):
    id: str
    company_name: str
    agent_name: str
    industry: str
    timezone: str
    phone_numbers: list[str] = []  # inbound numbers routed to this profile
    greeting: str
    persona: str = ""  # extra tone/style instructions
    inbound_goal: str
    # Holiday closures / staffed overflow: force the business open or closed regardless of hours.
    hours_override: Literal["auto", "open", "closed"] = "auto"
    business_hours: dict[str, list[str]] = Field(
        description='{"mon": ["09:00", "17:00"], ...}; missing day = closed')
    services: list[str]
    faq: dict[str, str]
    lead_fields: list[str] = ["name", "email", "reason"]
    escalation_triggers: list[str] = []
    emergency_instructions: str = ""
    transfer_targets: dict[str, TransferTarget]
    qualification: Qualification = Qualification()
    calendar: CalendarConfig = CalendarConfig()
    crm: CRMConfig = CRMConfig()
    outbound: OutboundConfig

    @field_validator("id")
    @classmethod
    def _id(cls, v):
        if not PROFILE_ID.match(v):
            raise ValueError("id must be lowercase letters, digits and dashes")
        return v

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v):
        try:
            ZoneInfo(v)
        except (KeyError, ValueError):
            raise ValueError(f"unknown timezone {v}")
        return v

    @field_validator("business_hours")
    @classmethod
    def _hours(cls, v):
        for day, span in v.items():
            if day not in DAYS or len(span) != 2:
                raise ValueError(f"bad business_hours entry {day}: {span}")
            for t in span:
                time.fromisoformat(t)
        return v

    # --- helpers ------------------------------------------------------------
    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def is_open(self, at: datetime | None = None) -> bool:
        if self.hours_override != "auto":
            return self.hours_override == "open"
        at = (at or self.now()).astimezone(self.tz)
        span = self.business_hours.get(DAYS[at.weekday()])
        if not span:
            return False
        return time.fromisoformat(span[0]) <= at.time() < time.fromisoformat(span[1])

    def hours_text(self) -> str:
        out = []
        for d in DAYS:
            span = self.business_hours.get(d)
            out.append(f"{d.title()} {span[0]}-{span[1]}" if span else f"{d.title()} closed")
        return ", ".join(out)


class ProfileStore:
    def __init__(self, directory: str):
        self.dir = Path(directory)
        self.profiles: dict[str, Profile] = {}
        self.reload()

    def reload(self) -> None:
        loaded = {}
        for path in sorted(self.dir.glob("*.json")):
            p = Profile.model_validate_json(path.read_text(encoding="utf-8"))
            loaded[p.id] = p
        self.profiles = loaded

    def get(self, profile_id: str) -> Profile:
        if profile_id not in self.profiles:
            raise KeyError(f"Unknown profile {profile_id}")
        return self.profiles[profile_id]

    def for_number(self, dialed: str, default: str) -> Profile:
        digits = re.sub(r"[^\d+]", "", dialed or "")
        for p in self.profiles.values():
            if digits and digits in {re.sub(r"[^\d+]", "", n) for n in p.phone_numbers}:
                return p
        return self.get(default) if default in self.profiles else next(iter(self.profiles.values()))

    def save(self, data: dict) -> Profile:
        p = Profile.model_validate(data)  # raises on invalid config - nothing written
        (self.dir / f"{p.id}.json").write_text(json.dumps(p.model_dump(), indent=2), encoding="utf-8")
        self.profiles[p.id] = p
        return p
