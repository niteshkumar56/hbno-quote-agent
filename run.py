"""HBNO email-to-quote agent.

Usage:
    python run.py data/sample_emails.json
    python run.py path/to/new_emails.json --out out_new --db quotes.db
    python run.py data/sample_emails.json --extractor fixture     # offline pipeline test, no API key
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from agent import config, data                      # noqa: E402  (after .env is loaded)
from agent.extract import AnthropicExtractor, FixtureExtractor  # noqa: E402
from agent.pipeline import process_email             # noqa: E402
from agent.store import Store, to_json               # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Turn quote-request emails into draft quotes for rep approval.")
    ap.add_argument("emails", type=Path, help="JSON file: array of {id, sender, to, subject, date, body}")
    ap.add_argument("--out", type=Path, default=ROOT / "out", help="output folder (default: out/)")
    ap.add_argument("--db", type=Path, default=ROOT / "quotes.db", help="SQLite store (default: quotes.db)")
    ap.add_argument("--data-dir", type=Path, default=config.DATA_DIR, help="folder with the pack CSVs")
    ap.add_argument("--extractor", choices=["anthropic", "fixture"], default="anthropic",
                    help="'fixture' uses hand-written test extractions (offline pipeline test only)")
    ap.add_argument("--model", default=None, help=f"override model (default {config.MODEL})")
    ap.add_argument("--only", default=None, help="comma-separated email ids to process")
    ap.add_argument("--as-of", type=dt.date.fromisoformat, default=None, metavar="YYYY-MM-DD",
                    help="draft date for this run (default: today); quotes are valid 5 business days from it")
    args = ap.parse_args()

    try:
        emails = json.loads(args.emails.read_text(encoding="utf-8-sig"))   # tolerate a Windows BOM
    except (OSError, json.JSONDecodeError) as e:
        print(f"Could not read {args.emails}: {e}", file=sys.stderr)
        return 2
    if not isinstance(emails, list):
        print("Input must be a JSON array of email objects (same format as sample_emails.json).", file=sys.stderr)
        return 2
    if args.only:
        wanted = {s.strip() for s in args.only.split(",")}
        emails = [e for e in emails if isinstance(e, dict) and str(e.get("id")) in wanted]

    ref = data.load(args.data_dir)
    if args.extractor == "anthropic":
        if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")):
            print("No API key. Copy .env.example to .env and set ANTHROPIC_API_KEY (see README).", file=sys.stderr)
            return 2
        extractor = AnthropicExtractor(ref, model=args.model)
        print(f"Model: {extractor.model} (effort {extractor.effort}) | prompt {extractor.prompts.version}")
    else:
        extractor = FixtureExtractor(ref)
        print("Extractor: FIXTURE (hand-written test extractions, NOT model output)")

    store = Store(args.db)
    results = []
    try:
        for email in emails:
            r = process_email(email, ref, store, extractor, args.out, today=args.as_of)
            results.append(r)
            rec = r.get("record") or {}
            flags = sorted({f["code"] for f in rec.get("flags", [])} |
                           {f["code"] for l in rec.get("lines", []) for f in l["flags"]})
            total = rec.get("order_total")
            print(f"{r['email_id']:<6} {r['outcome']:<10} {rec.get('quote_number') or rec.get('record_type') or '-':<16} "
                  f"{(rec.get('customer') or {}).get('company') or (rec.get('customer') or {}).get('email_domain') or '':<26.26}"
                  f"{('$' + format(float(total), ',.2f')) if total is not None else '':>12}  "
                  f"{rec.get('review_state') or r.get('error', '')}  {', '.join(flags)}")
    finally:
        store.close()

    by = lambda o: sum(1 for r in results if r["outcome"] == o)               # noqa: E731
    cost = sum(r.get("cost_usd", 0.0) for r in results)
    charged = [r for r in results if r.get("cost_usd")]
    tokens = {k: sum(r.get("usage", {}).get(k, 0) for r in results)
              for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}
    summary = {
        "emails": len(results), "created": by("created"), "duplicates_skipped": by("duplicate"),
        "failed": by("failed"), "invalid_input": by("invalid_input"),
        "quotes": sum(1 for r in results if r["outcome"] == "created" and r["record"]["record_type"] == "quote"),
        "triage": sum(1 for r in results if r["outcome"] == "created" and r["record"]["record_type"] == "triage"),
        "tokens": tokens, "cost_usd": round(cost, 4),
        "cost_per_email_usd": round(cost / len(charged), 4) if charged else 0.0,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "run_summary.json").write_text(to_json(summary), encoding="utf-8")
    print("\n" + json.dumps(summary, indent=2))
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
