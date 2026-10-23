"""Outbound eligibility and opt-out handling.

These checks are deterministic code, not LLM judgement: a lead is only dialled when every check
passes, and every decision (allowed or blocked, with reasons) is written to the dial log.
This is an engineering control, not legal advice or a compliance certification.
"""

import re
from datetime import datetime, time
from zoneinfo import ZoneInfo

from .profiles import DAYS, Profile
from .store import Store, normalize_phone

OPT_OUT = re.compile(
    r"\b(do not call|don'?t (?:ever )?call|stop calling|remove me|take me off|unsubscribe|opt(?:[ -])?out"
    r"|never call|no more calls|do not contact|don'?t contact)\b", re.I)

# Only leads that haven't been reached yet (or whose last attempt didn't connect) are dialled.
# Anyone reached once is handed to people via tasks, never re-dialled automatically.
DIALABLE_STATUSES = {"new", "no_answer", "busy", "failed"}


def detects_opt_out(text: str) -> bool:
    return bool(OPT_OUT.search(text or ""))


def check_eligibility(store: Store, profile: Profile, lead: dict, mode: str = "simulate",
                      test_numbers: list[str] | None = None, now: datetime | None = None) -> list[str]:
    """Return the reasons this lead may NOT be called right now. Empty list = eligible."""
    reasons = []
    out = profile.outbound
    if not out.enabled:
        reasons.append("outbound calling is disabled for this profile")
    dnc = store.is_dnc(lead["phone"], profile.id)
    if dnc:
        reasons.append(f"on do-not-call list ({dnc['reason'] or dnc['source'] or 'opt-out'})")
    if lead.get("consent_status") not in out.allowed_consent:
        reasons.append(f"no qualifying consent (has '{lead.get('consent_status') or 'none'}', "
                       f"needs one of {out.allowed_consent})")
    if (lead.get("status") or "new") not in DIALABLE_STATUSES:
        reasons.append(f"lead status is '{lead['status']}'")
    if (lead.get("attempts") or 0) >= out.max_attempts:
        reasons.append(f"max attempts reached ({out.max_attempts})")

    now = now or datetime.now(ZoneInfo("UTC"))
    if lead.get("next_attempt_at") and datetime.fromisoformat(lead["next_attempt_at"]) > now:
        reasons.append(f"next retry not due until {lead['next_attempt_at']}")
    local = now.astimezone(ZoneInfo(lead.get("timezone") or profile.timezone))
    start, end = (time.fromisoformat(t) for t in out.calling_window)
    if DAYS[local.weekday()] not in out.calling_days or not (start <= local.time() < end):
        reasons.append(f"outside calling window for the lead's local time ({local:%a %H:%M}; "
                       f"allowed {'/'.join(out.calling_days)} {out.calling_window[0]}-{out.calling_window[1]})")

    if mode == "twilio":
        approved = {normalize_phone(n) for n in (test_numbers or [])}
        if normalize_phone(lead["phone"]) not in approved:
            reasons.append("not an approved test number (OUTBOUND_TEST_NUMBERS) - real dial refused")
    return reasons
