"""Per-email orchestration: input guard -> extraction -> output guard -> rules -> atomic write -> files."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path

from . import config, cover_email, redact
from .data import RefData
from .dates import parse_email_date
from .extract import ExtractionError, FixtureExtractor
from .rules import build_record, netsuite_estimate
from .store import Store, idempotency_key, to_json

REQUIRED_FIELDS = ("id", "sender", "subject", "date", "body")


def validate_email(email) -> str | None:
    if not isinstance(email, dict):
        return "record is not an object"
    missing = [k for k in REQUIRED_FIELDS if k not in email]
    if missing:
        return f"missing fields: {missing}"
    if "@" not in str(email["sender"]):
        return "sender is not an email address"
    try:
        parse_email_date(str(email["date"]))
    except ValueError as e:
        return str(e)
    return None


def write_outputs(record: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    eid = re.sub(r"[^\w.-]", "_", str(record["source"]["email_id"]))[:100]   # safe as a filename
    (out_dir / f"{eid}.json").write_text(to_json(record), encoding="utf-8")
    (out_dir / f"{eid}_cover_email.txt").write_text(cover_email.render(record), encoding="utf-8")


def manual_record(email: dict, eid: str, status: str, flag: str, detail: str, quote_date: str | None) -> dict:
    """A triage record for an email the agent could not process. Nothing is guessed."""
    sender = str(email.get("sender") or "")
    return {
        "record_type": "triage", "quote_number": None, "status": status,
        "review_state": "needs_rep_action", "route_to": "Sales rep: process manually", "intent": None,
        "source": {"email_id": eid, "sender": sender or None, "subject": email.get("subject"),
                   "received_at": email.get("date")},
        "customer": {"type": "unknown", "customer_id": None, "company": None,
                     "email_domain": sender.rsplit("@", 1)[-1] if "@" in sender else None,
                     "contact_email": sender or None},
        "quote_date": quote_date, "expiry_date": None, "lines": [], "order_total": None,
        "flags": [{"code": flag, "severity": "blocker", "detail": detail, "source": "code"}],
    }


def _safe_id(email) -> str:
    raw = email.get("id") if isinstance(email, dict) else None
    if raw is not None and str(raw).strip():
        return str(raw)
    digest = hashlib.sha256(json.dumps(email, sort_keys=True, default=str).encode()).hexdigest()[:10]
    return f"invalid-{digest}"


def process_email(email: dict, ref: RefData, store: Store, extractor, out_dir: Path,
                  today: dt.date | None = None) -> dict:
    """Returns a result summary dict. Never raises for a single bad email. `today` is the draft
    date (defaults to the local date); a draft is never dated before the email it answers."""
    eid = _safe_id(email)
    err = validate_email(email)
    if err:
        # Not stored as processed (there may be no reliable key). Re-running is free: no model call.
        rec = manual_record(email if isinstance(email, dict) else {}, eid, "Invalid input - manual processing",
                            "INVALID_INPUT", err, None)
        write_outputs(rec, out_dir)
        return {"email_id": eid, "outcome": "invalid_input", "error": err, "record": rec, "cost_usd": 0.0}

    key = idempotency_key(email)
    existing = store.get(key)
    if existing:                                   # checked BEFORE any model call: reruns cost nothing
        write_outputs(existing, out_dir)
        return {"email_id": eid, "outcome": "duplicate", "record": existing, "cost_usd": 0.0}

    prepared = redact.prepare(email, config.MAX_BODY_CHARS, protected_words=ref.vocabulary)
    received = parse_email_date(email["date"])
    try:
        if isinstance(extractor, FixtureExtractor):
            result = extractor.extract_for(eid)
        else:
            result = extractor.extract(received.isoformat(), prepared)
    except ExtractionError as e:
        # Not stored as processed: a rerun will retry. A triage file is written for the rep.
        failed = manual_record(email, eid, "Extraction failed - manual processing", "EXTRACTION_FAILED",
                               str(e), max(today or dt.date.today(), received).isoformat())
        write_outputs(failed, out_dir)
        return {"email_id": eid, "outcome": "failed", "error": str(e), "record": failed,
                "cost_usd": getattr(e, "cost_usd", 0.0), "usage": getattr(e, "usage", {})}

    record = build_record(ref, email, prepared, result.extraction, received, today or dt.date.today())
    record["idempotency_key"] = key
    if prepared["truncated"]:
        record["flags"].append({"code": "CLARIFICATION_NEEDED", "severity": "warning", "source": "code",
                                "detail": f"body longer than {config.MAX_BODY_CHARS} chars was truncated"})
    drafts = store.open_drafts(record["customer"]["email_domain"], key) if record["record_type"] == "quote" else []
    record["rep_context"] = {"other_open_drafts": drafts}
    record["netsuite_estimate"] = netsuite_estimate(record)
    record["audit"] = {
        "processed_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "extractor": extractor.name, "model": result.model, "prompt_version": result.prompt_version,
        "attempts": result.attempts, "usage": result.usage, "cost_usd": result.cost_usd,
        "redactions_before_model": prepared["redactions"],
        "sent_to_model": {"subject": prepared["subject"], "body": prepared["body"], "quoted": prepared["quoted"]},
        "extraction": result.extraction,
    }
    stored, created = store.save(record)
    write_outputs(stored, out_dir)
    return {"email_id": eid, "outcome": "created" if created else "duplicate", "record": stored,
            "cost_usd": result.cost_usd if created else 0.0, "usage": result.usage}
