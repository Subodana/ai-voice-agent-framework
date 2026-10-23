"""Calendar providers. Swap per profile with `calendar.provider`; add a provider by implementing
`available_slots` and `book` (see docs/SWAPPING_PROVIDERS.md).

- LocalCalendar: built-in calendar driven by the profile's business hours (no account needed).
- CalComCalendar: Cal.com API v2 (slots + bookings) using an event type you create in Cal.com.
"""

import logging
import os
from datetime import date, datetime, timedelta, timezone

import requests

from .faults import Faults, IntegrationError
from .profiles import DAYS, Profile
from .store import Store

log = logging.getLogger(__name__)


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


class LocalCalendar:
    name = "local"

    def __init__(self, store: Store, faults: Faults):
        self.store, self.faults = store, faults

    def available_slots(self, profile: Profile, day: date | None = None, limit: int = 6) -> list[datetime]:
        self.faults.check("calendar")
        cal, tz = profile.calendar, profile.tz
        step = timedelta(minutes=cal.duration_minutes)
        earliest = profile.now() + timedelta(hours=1)  # never offer a slot starting within the hour
        taken = [(_parse(b["start_at"]), _parse(b["end_at"]))
                 for b in self.store.bookings(profile.id, status="confirmed")]
        slots = []
        for offset in range(cal.days_ahead + 1):
            d = profile.now().date() + timedelta(days=offset)
            if day and d != day:
                continue
            span = profile.business_hours.get(DAYS[d.weekday()])
            if not span:
                continue
            t = datetime.combine(d, datetime.strptime(span[0], "%H:%M").time(), tz)
            close = datetime.combine(d, datetime.strptime(span[1], "%H:%M").time(), tz)
            while t + step <= close and len(slots) < limit:
                if t >= earliest and not any(s < t + step and t < e for s, e in taken):
                    slots.append(t)
                t += step
        return slots

    def book(self, profile: Profile, start: datetime, attendee: dict) -> dict:
        self.faults.check("calendar")
        if start not in self.available_slots(profile, start.astimezone(profile.tz).date(), limit=100):
            raise ValueError("That time is no longer available")
        end = start + timedelta(minutes=profile.calendar.duration_minutes)
        return {"external_id": None, "start": start, "end": end}


class CalComCalendar:
    name = "calcom"
    BASE = "https://api.cal.com/v2"

    def __init__(self, faults: Faults):
        self.faults = faults

    def _headers(self, profile: Profile, version: str) -> dict:
        key = os.getenv(profile.calendar.api_key_env, "")
        if not key or not profile.calendar.event_type_id:
            raise IntegrationError("Cal.com is not configured (API key env var or event_type_id missing)")
        return {"Authorization": f"Bearer {key}", "cal-api-version": version}

    def available_slots(self, profile: Profile, day: date | None = None, limit: int = 6) -> list[datetime]:
        self.faults.check("calendar")
        start = day or profile.now().date()
        end = day or start + timedelta(days=profile.calendar.days_ahead)
        try:
            r = requests.get(f"{self.BASE}/slots", headers=self._headers(profile, "2024-09-04"), timeout=8, params={
                "eventTypeId": profile.calendar.event_type_id, "start": start.isoformat(),
                "end": end.isoformat(), "timeZone": profile.timezone})
            r.raise_for_status()
            days = r.json()["data"]
        except (requests.RequestException, KeyError, ValueError) as e:
            raise IntegrationError(f"Cal.com slots failed: {e}") from e
        slots = [_parse(s["start"]).astimezone(profile.tz) for d in sorted(days) for s in days[d]]
        return slots[:limit]

    def book(self, profile: Profile, start: datetime, attendee: dict) -> dict:
        self.faults.check("calendar")
        if not attendee.get("email"):
            raise ValueError("An email address is needed to send the booking confirmation")
        try:
            r = requests.post(f"{self.BASE}/bookings", headers=self._headers(profile, "2024-08-13"), timeout=10, json={
                "start": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                "eventTypeId": profile.calendar.event_type_id,
                "attendee": {"name": attendee["name"], "email": attendee["email"],
                             "timeZone": profile.timezone, "phoneNumber": attendee.get("phone")},
                "metadata": {"source": "ai-voice-agent", "call_id": attendee.get("call_id", "")},
            })
            if r.status_code in (400, 409):
                raise ValueError("That time is no longer available")
            r.raise_for_status()
            data = r.json()["data"]
        except (requests.RequestException, KeyError) as e:
            raise IntegrationError(f"Cal.com booking failed: {e}") from e
        return {"external_id": data.get("uid"), "start": _parse(data["start"]), "end": _parse(data["end"])}


def build_calendars(store: Store, faults: Faults) -> dict:
    return {"local": LocalCalendar(store, faults), "calcom": CalComCalendar(faults)}
