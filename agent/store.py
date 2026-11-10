"""SQLite store. One transaction per email; a unique idempotency key prevents duplicates."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from decimal import Decimal
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_emails (
    idempotency_key TEXT PRIMARY KEY,
    email_id        TEXT NOT NULL,
    record_type     TEXT NOT NULL,             -- quote | triage
    quote_number    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    record_json     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quotes (
    quote_number    TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE REFERENCES processed_emails(idempotency_key),
    customer_id     TEXT,
    customer_domain TEXT NOT NULL,
    status          TEXT NOT NULL,
    review_state    TEXT NOT NULL,
    quote_date      TEXT NOT NULL,
    expiry_date     TEXT NOT NULL,
    order_total     TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS quote_lines (
    quote_number    TEXT NOT NULL REFERENCES quotes(quote_number),
    line_no         INTEGER NOT NULL,
    sku             TEXT,
    quantity        REAL,
    unit_price      TEXT,
    amount          TEXT,
    flags_json      TEXT NOT NULL,
    PRIMARY KEY (quote_number, line_no)
);
CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
"""


def idempotency_key(email: dict) -> str:
    body = re.sub(r"\s+", " ", email.get("body") or "").strip()
    raw = "\x1f".join([(email.get("sender") or "").strip().lower(), (email.get("date") or "").strip(),
                       re.sub(r"\s+", " ", email.get("subject") or "").strip(), body])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def to_json(obj) -> str:
    def default(o):
        if isinstance(o, Decimal):
            return float(o)
        raise TypeError(type(o))
    return json.dumps(obj, default=default, ensure_ascii=False, indent=2)


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None)   # explicit transactions
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)

    def get(self, key: str) -> dict | None:
        row = self.conn.execute("SELECT record_json FROM processed_emails WHERE idempotency_key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, record: dict, _fail_after_header: bool = False) -> tuple[dict, bool]:
        """Atomically persist a record. Returns (stored_record, created). If the key already exists
        (e.g. a concurrent or repeated run), the existing record is returned and nothing is written."""
        key = record["idempotency_key"]
        cur = self.conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            existing = cur.execute("SELECT record_json FROM processed_emails WHERE idempotency_key=?", (key,)).fetchone()
            if existing:
                cur.execute("ROLLBACK")
                return json.loads(existing[0]), False

            if record["record_type"] == "quote":
                year = record["quote_date"][:4]
                cname = f"{config.QUOTE_PREFIX}-{year}"
                cur.execute("INSERT INTO counters(name, value) VALUES (?, 1) "
                            "ON CONFLICT(name) DO UPDATE SET value = value + 1", (cname,))
                n = cur.execute("SELECT value FROM counters WHERE name=?", (cname,)).fetchone()[0]
                record["quote_number"] = f"{cname}-{n:05d}"
                if record.get("netsuite_estimate"):
                    record["netsuite_estimate"]["tranId"] = record["quote_number"]

            payload = to_json(record)
            cur.execute("INSERT INTO processed_emails(idempotency_key, email_id, record_type, quote_number, record_json) "
                        "VALUES (?,?,?,?,?)", (key, record["source"]["email_id"], record["record_type"],
                                               record["quote_number"], payload))
            if record["record_type"] == "quote":
                cur.execute("INSERT INTO quotes(quote_number, idempotency_key, customer_id, customer_domain, status, "
                            "review_state, quote_date, expiry_date, order_total) VALUES (?,?,?,?,?,?,?,?,?)",
                            (record["quote_number"], key, record["customer"]["customer_id"],
                             record["customer"]["email_domain"], record["status"], record["review_state"],
                             record["quote_date"], record["expiry_date"], str(record["order_total"])))
                if _fail_after_header:
                    raise RuntimeError("simulated crash after header insert")
                for l in record["lines"]:
                    cur.execute("INSERT INTO quote_lines VALUES (?,?,?,?,?,?,?)",
                                (record["quote_number"], l["line_no"], l["sku"], l["quantity"],
                                 None if l["unit_price"] is None else str(l["unit_price"]),
                                 None if l["amount"] is None else str(l["amount"]),
                                 json.dumps([f["code"] for f in l["flags"]])))
            cur.execute("COMMIT")
            return json.loads(payload), True
        except BaseException:
            cur.execute("ROLLBACK")
            if record["record_type"] == "quote":
                record["quote_number"] = None
            raise

    def open_drafts(self, customer_domain: str, exclude_key: str) -> list[tuple[str, str]]:
        return self.conn.execute(
            "SELECT quote_number, order_total FROM quotes WHERE customer_domain=? AND idempotency_key<>? "
            "AND status=? ORDER BY quote_number", (customer_domain, exclude_key, config.QUOTE_STATUS)).fetchall()

    def close(self):
        self.conn.close()
