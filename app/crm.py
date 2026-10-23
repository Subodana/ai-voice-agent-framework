"""CRM delivery through a signed webhook with a durable outbox.

Every lead/call/booking/opt-out change becomes an event in the SQLite outbox and is POSTed to the
profile's `crm.webhook_url` (n8n, Make, Zapier, GoHighLevel, HubSpot workflows, or your own API).
Failed deliveries are retried with exponential backoff and never block a live call.
"""

import hashlib
import hmac
import json
import logging
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone

import requests

from .faults import Faults, IntegrationError
from .profiles import Profile, ProfileStore
from .store import Store, now_iso

log = logging.getLogger(__name__)


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class WebhookCRM:
    def __init__(self, store: Store, profiles: ProfileStore, faults: Faults, max_attempts: int = 6, post=None,
                 self_url: str = "http://127.0.0.1:8000"):
        self.store, self.profiles, self.faults = store, profiles, faults
        self.self_url = self_url
        self.max_attempts = max_attempts
        self.post = post or requests.post
        self._wake = threading.Event()
        self._flush_lock = threading.Lock()

    def emit(self, profile: Profile, event_type: str, data: dict) -> int:
        payload = {"id": f"evt_{uuid.uuid4().hex[:16]}", "type": event_type, "profile_id": profile.id,
                   "company": profile.company_name, "occurred_at": now_iso(), "data": data}
        event_id = self.store.enqueue(profile.id, event_type, payload)
        self._wake.set()  # deliver now in the background; the caller never waits on the CRM
        return event_id

    def flush(self) -> None:
        with self._flush_lock:
            for row in self.store.due_outbox():
                self._deliver(row)

    def retry(self, event_id: int) -> None:
        self.store.mark_outbox(event_id, status="pending", next_attempt_at=now_iso())
        self._wake.set()

    def _deliver(self, row: dict) -> None:
        attempts = row["attempts"] + 1
        try:
            profile = self.profiles.get(row["profile_id"])
            if profile.crm.provider == "none" or not profile.crm.webhook_url:
                self.store.mark_outbox(row["id"], status="skipped", attempts=attempts, sent_at=now_iso())
                return
            self.faults.check("crm")
            body = row["payload"].encode()
            headers = {"Content-Type": "application/json", "X-Voice-Event": row["event_type"]}
            secret = os.getenv(profile.crm.secret_env, "") if profile.crm.secret_env else ""
            if secret:
                headers["X-Signature"] = sign(secret, body)
            url = profile.crm.webhook_url
            if url.startswith("/"):
                url = self.self_url + url
            r = self.post(url, data=body, headers=headers, timeout=8)
            if r.status_code >= 300:
                raise IntegrationError(f"HTTP {r.status_code}")
            self.store.mark_outbox(row["id"], status="sent", attempts=attempts, sent_at=now_iso(), last_error=None)
        except (requests.RequestException, IntegrationError, KeyError) as e:
            delay = min(10 * 2 ** (attempts - 1), 3600)  # 10s, 20s, 40s ... capped at 1h
            status = "dead" if attempts >= self.max_attempts else "pending"
            nxt = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec="seconds")
            self.store.mark_outbox(row["id"], status=status, attempts=attempts, last_error=str(e)[:300],
                                   next_attempt_at=nxt)
            log.warning("Webhook %s attempt %d failed: %s", row["id"], attempts, e)

    def start(self) -> None:
        def loop():
            while True:
                self._wake.wait(timeout=5)
                self._wake.clear()
                try:
                    self.flush()
                except Exception:
                    log.exception("Outbox flush failed")
        threading.Thread(target=loop, name="crm-outbox", daemon=True).start()


def verify(profiles: ProfileStore, body: bytes, signature: str) -> bool:
    """Used by the built-in mock CRM receiver; real receivers do the same check."""
    try:
        profile = profiles.get(json.loads(body)["profile_id"])
    except (KeyError, ValueError):
        return False
    secret = os.getenv(profile.crm.secret_env, "") if profile.crm.secret_env else ""
    return bool(secret) and hmac.compare_digest(sign(secret, body), signature or "")
