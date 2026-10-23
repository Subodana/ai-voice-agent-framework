"""Load demo leads that exercise every outbound check. Safe to re-run; it only touches demo numbers.

    python -m scripts.seed_demo            # leads use this computer's timezone
    python -m scripts.seed_demo --tz America/Denver

All numbers are fictional 555-01xx numbers. Real dials are refused unless a number is in OUTBOUND_TEST_NUMBERS.
"""

import argparse
from datetime import datetime

from app.config import settings
from app.store import Store, now_iso


def local_zone() -> str:
    hours = int(datetime.now().astimezone().utcoffset().total_seconds() // 3600)
    return "Etc/GMT" if hours == 0 else f"Etc/GMT{'-' if hours > 0 else '+'}{abs(hours)}"  # Etc signs are inverted


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tz", default=local_zone(), help="timezone for the eligible demo leads")
    tz = ap.parse_args().tz
    hours = int(datetime.now().astimezone().utcoffset().total_seconds() // 3600)
    far = (hours + 12 + 11) % 24 - 11  # ~12h from here, wrapped into -11..+12: outside any calling window
    night = "Etc/GMT" if far == 0 else f"Etc/GMT{'-' if far > 0 else '+'}{abs(far)}"

    store = Store(settings.sqlite_path)
    demo = [
        ("summit-solar", "+15550102222", "Sam Carter", "express_written", tz, "web form 2026-10-20, checkbox v3"),
        ("summit-solar", "+15550102230", "Riley Brooks", "none", tz, ""),
        ("summit-solar", "+15550102240", "Jordan Lee", "express_written", tz, "web form 2026-10-18"),
        ("summit-solar", "+15550102250", "Casey Night", "express_written", night, "web form 2026-10-19"),
        ("bright-smile-dental", "+15550101234", "Priya Patel", "existing_patient_reminders", tz, "patient intake form"),
    ]
    for profile, phone, name, consent, zone, source in demo:
        store._exec("DELETE FROM leads WHERE profile_id = ? AND phone = ?", (profile, phone))
        store.remove_dnc(phone, "*")
        store.upsert_lead(profile, phone, name=name, consent_status=consent, consent_source=source or None,
                          consent_at=now_iso() if consent != "none" else None, timezone=zone, status="new", attempts=0)
    store.add_dnc("+15550102240", "*", reason="asked not to be called (national registry)", source="seed")
    print(f"Seeded {len(demo)} demo leads (eligible ones in {tz}; Casey in {night}). Jordan Lee is on the DNC list.")


if __name__ == "__main__":
    main()
