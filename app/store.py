"""SQLite system of record: leads, calls, bookings, tasks, do-not-call list, webhook outbox.

External CRMs receive copies through the webhook outbox (app/crm.py); this database is
what the admin console reads and what makes retries and audits possible.
"""

import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id TEXT NOT NULL, phone TEXT NOT NULL,
    name TEXT, email TEXT, details TEXT DEFAULT '{}',
    consent_status TEXT DEFAULT 'none', consent_source TEXT, consent_at TEXT,
    timezone TEXT, status TEXT DEFAULT 'new',
    qualified INTEGER, qualification TEXT,
    attempts INTEGER DEFAULT 0, last_attempt_at TEXT, next_attempt_at TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE (profile_id, phone)
);
CREATE TABLE IF NOT EXISTS dnc (
    phone TEXT NOT NULL, profile_id TEXT NOT NULL DEFAULT '*',
    reason TEXT, source TEXT, created_at TEXT NOT NULL,
    PRIMARY KEY (phone, profile_id)
);
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT UNIQUE NOT NULL, profile_id TEXT, direction TEXT,
    lead_id INTEGER, phone TEXT, started_at TEXT, ended_at TEXT,
    transcript TEXT, events TEXT, summary TEXT, sentiment TEXT,
    outcome TEXT, transferred_to TEXT, booking_id INTEGER
);
CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id TEXT, lead_id INTEGER, call_id TEXT,
    start_at TEXT NOT NULL, end_at TEXT NOT NULL, appointment_type TEXT,
    provider TEXT, external_id TEXT, status TEXT DEFAULT 'confirmed',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id TEXT, lead_id INTEGER, call_id TEXT,
    title TEXT NOT NULL, details TEXT, priority TEXT DEFAULT 'medium',
    status TEXT DEFAULT 'open', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id TEXT, event_type TEXT NOT NULL, payload TEXT NOT NULL,
    status TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0,
    next_attempt_at TEXT NOT NULL, last_error TEXT,
    created_at TEXT NOT NULL, sent_at TEXT
);
CREATE TABLE IF NOT EXISTS dial_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id TEXT, lead_id INTEGER, phone TEXT,
    allowed INTEGER, reasons TEXT, mode TEXT, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS crm_inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT NOT NULL, event_type TEXT, payload TEXT, signature_valid INTEGER
);
"""

LEAD_COLUMNS = ("name", "email", "consent_status", "consent_source", "consent_at", "timezone",
                "status", "qualified", "qualification", "attempts", "last_attempt_at", "next_attempt_at")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"[^\d+]", "", phone or "")
    return digits or "unknown"


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._lock = threading.RLock()

    def _exec(self, sql: str, params=()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, params)
            self._db.commit()
            return cur

    def _one(self, sql: str, params=()) -> dict | None:
        with self._lock:
            row = self._db.execute(sql, params).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, params=()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, params).fetchall()]

    # --- leads ----------------------------------------------------------------
    def get_lead(self, lead_id: int) -> dict | None:
        return self._decode_lead(self._one("SELECT * FROM leads WHERE id = ?", (lead_id,)))

    def find_lead(self, profile_id: str, phone: str) -> dict | None:
        return self._decode_lead(self._one("SELECT * FROM leads WHERE profile_id = ? AND phone = ?",
                                           (profile_id, normalize_phone(phone))))

    def upsert_lead(self, profile_id: str, phone: str, **fields) -> dict:
        """Known columns are set directly; anything else is merged into the `details` JSON."""
        phone = normalize_phone(phone)
        cols = {k: v for k, v in fields.items() if k in LEAD_COLUMNS and v is not None}
        extra = {k: v for k, v in fields.items() if k not in LEAD_COLUMNS and v not in (None, "")}
        if isinstance(cols.get("qualification"), dict):
            cols["qualification"] = json.dumps(cols["qualification"])
        with self._lock:
            existing = self.find_lead(profile_id, phone)
            if existing:
                details = {**existing["details"], **extra}
                sets = ", ".join(f"{k} = ?" for k in cols)
                self._exec(f"UPDATE leads SET {sets + ', ' if sets else ''}details = ?, updated_at = ? WHERE id = ?",
                           (*cols.values(), json.dumps(details), now_iso(), existing["id"]))
            else:
                names = ["profile_id", "phone", *cols, "details", "created_at", "updated_at"]
                self._exec(f"INSERT INTO leads ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
                           (profile_id, phone, *cols.values(), json.dumps(extra), now_iso(), now_iso()))
        return self.find_lead(profile_id, phone)

    @staticmethod
    def _decode_lead(row: dict | None) -> dict | None:
        if row:
            row["details"] = json.loads(row.get("details") or "{}")
            row["qualification"] = json.loads(row["qualification"]) if row.get("qualification") else None
        return row

    def leads(self, profile_id: str | None = None) -> list[dict]:
        sql, params = "SELECT * FROM leads", ()
        if profile_id:
            sql, params = sql + " WHERE profile_id = ?", (profile_id,)
        return [self._decode_lead(r) for r in self._all(sql + " ORDER BY updated_at DESC LIMIT 200", params)]

    # --- do-not-call --------------------------------------------------------------
    def add_dnc(self, phone: str, profile_id: str = "*", reason: str = "", source: str = "") -> None:
        self._exec("INSERT OR IGNORE INTO dnc (phone, profile_id, reason, source, created_at) VALUES (?, ?, ?, ?, ?)",
                   (normalize_phone(phone), profile_id, reason, source, now_iso()))

    def remove_dnc(self, phone: str, profile_id: str = "*") -> None:
        self._exec("DELETE FROM dnc WHERE phone = ? AND profile_id = ?", (normalize_phone(phone), profile_id))

    def is_dnc(self, phone: str, profile_id: str) -> dict | None:
        return self._one("SELECT * FROM dnc WHERE phone = ? AND profile_id IN ('*', ?)",
                         (normalize_phone(phone), profile_id))

    def dnc_list(self) -> list[dict]:
        return self._all("SELECT * FROM dnc ORDER BY created_at DESC")

    # --- calls, bookings, tasks ---------------------------------------------------
    def log_call(self, call: dict) -> None:
        cols = ["call_id", "profile_id", "direction", "lead_id", "phone", "started_at", "ended_at",
                "transcript", "events", "summary", "sentiment", "outcome", "transferred_to", "booking_id"]
        values = [json.dumps(call.get(c, [])) if c in ("transcript", "events") else call.get(c) for c in cols]
        self._exec(f"INSERT OR REPLACE INTO calls ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", values)

    def calls(self, profile_id: str | None = None) -> list[dict]:
        sql = ("SELECT c.*, l.name AS lead_name FROM calls c LEFT JOIN leads l ON l.id = c.lead_id"
               + (" WHERE c.profile_id = ?" if profile_id else "") + " ORDER BY c.id DESC LIMIT 100")
        rows = self._all(sql, (profile_id,) if profile_id else ())
        for r in rows:
            r["transcript"] = json.loads(r["transcript"] or "[]")
            r["events"] = json.loads(r["events"] or "[]")
        return rows

    def add_booking(self, **b) -> dict:
        cur = self._exec(
            "INSERT INTO bookings (profile_id, lead_id, call_id, start_at, end_at, appointment_type, provider, external_id,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (b["profile_id"], b.get("lead_id"), b.get("call_id"), b["start"], b["end"], b.get("appointment_type"),
             b.get("provider"), b.get("external_id"), now_iso()))
        return self._one("SELECT * FROM bookings WHERE id = ?", (cur.lastrowid,))

    def bookings(self, profile_id: str | None = None, status: str | None = None) -> list[dict]:
        where, params = [], []
        if profile_id:
            where.append("b.profile_id = ?"); params.append(profile_id)
        if status:
            where.append("b.status = ?"); params.append(status)
        sql = ("SELECT b.*, l.name AS lead_name, l.phone AS lead_phone FROM bookings b "
               "LEFT JOIN leads l ON l.id = b.lead_id" + (" WHERE " + " AND ".join(where) if where else "")
               + " ORDER BY b.start_at")
        return self._all(sql, params)

    def add_task(self, profile_id: str, lead_id: int | None, call_id: str | None, title: str,
                 details: str = "", priority: str = "medium") -> dict:
        cur = self._exec("INSERT INTO tasks (profile_id, lead_id, call_id, title, details, priority, created_at)"
                         " VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (profile_id, lead_id, call_id, title, details, priority, now_iso()))
        return self._one("SELECT * FROM tasks WHERE id = ?", (cur.lastrowid,))

    def tasks(self, profile_id: str | None = None) -> list[dict]:
        sql = ("SELECT t.*, l.name AS lead_name, l.phone AS lead_phone FROM tasks t "
               "LEFT JOIN leads l ON l.id = t.lead_id" + (" WHERE t.profile_id = ?" if profile_id else "")
               + " ORDER BY t.id DESC LIMIT 200")
        return self._all(sql, (profile_id,) if profile_id else ())

    # --- outbox (webhook delivery with retries) -------------------------------------
    def enqueue(self, profile_id: str, event_type: str, payload: dict) -> int:
        cur = self._exec("INSERT INTO outbox (profile_id, event_type, payload, next_attempt_at, created_at)"
                         " VALUES (?, ?, ?, ?, ?)",
                         (profile_id, event_type, json.dumps(payload, default=str), now_iso(), now_iso()))
        return cur.lastrowid

    def due_outbox(self, limit: int = 20) -> list[dict]:
        return self._all("SELECT * FROM outbox WHERE status = 'pending' AND next_attempt_at <= ? ORDER BY id LIMIT ?",
                         (now_iso(), limit))

    def mark_outbox(self, event_id: int, **fields) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE outbox SET {sets} WHERE id = ?", (*fields.values(), event_id))

    def outbox(self, profile_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM outbox" + (" WHERE profile_id = ?" if profile_id else "") + " ORDER BY id DESC LIMIT 200"
        rows = self._all(sql, (profile_id,) if profile_id else ())
        for r in rows:
            r["payload"] = json.loads(r["payload"])
        return rows

    # --- audit ----------------------------------------------------------------------
    def log_dial(self, profile_id: str, lead_id: int, phone: str, allowed: bool, reasons: list[str], mode: str) -> None:
        self._exec("INSERT INTO dial_log (profile_id, lead_id, phone, allowed, reasons, mode, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (profile_id, lead_id, phone, int(allowed), json.dumps(reasons), mode, now_iso()))

    def dial_log(self, profile_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM dial_log" + (" WHERE profile_id = ?" if profile_id else "") + " ORDER BY id DESC LIMIT 200"
        rows = self._all(sql, (profile_id,) if profile_id else ())
        for r in rows:
            r["reasons"] = json.loads(r["reasons"] or "[]")
        return rows

    def record_inbox(self, event_type: str, payload: str, signature_valid: bool) -> None:
        self._exec("INSERT INTO crm_inbox (received_at, event_type, payload, signature_valid) VALUES (?, ?, ?, ?)",
                   (now_iso(), event_type, payload, int(signature_valid)))

    def inbox(self) -> list[dict]:
        rows = self._all("SELECT * FROM crm_inbox ORDER BY id DESC LIMIT 100")
        for r in rows:
            r["payload"] = json.loads(r["payload"])
        return rows
