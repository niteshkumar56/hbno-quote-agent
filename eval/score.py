"""Score agent output (out/*.json) against eval/expected.json.

Usage: python eval/score.py [--out out] [--expected eval/expected.json]

Metrics
  intent, create_quote, customer        exact match per email
  lines                                  priced lines: sku + qty + unit price + amount exact
                                         unpriced lines: must NOT be priced (pricing one = critical error)
  order_total                            exact to the cent
  flags (blocker+warning)                recall / precision over the union of header and line flags
  flags (info)                           recall only, reported but not penalised
  critical errors                        guessed SKU, forbidden SKU/flag, injected price, wrong customer
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOFT = {"CLARIFICATION_NEEDED", "URGENT_REQUEST", "NON_PRODUCT_REQUEST"}   # extras not penalised


def all_flags(rec_or_exp: dict, actual: bool) -> set[str]:
    if actual:
        return {f["code"] for f in rec_or_exp.get("flags", [])} | \
               {f["code"] for l in rec_or_exp.get("lines", []) for f in l.get("flags", [])}
    return set(rec_or_exp.get("flags", [])) | {f for l in rec_or_exp.get("lines", []) for f in l.get("flags", [])}


def cents(x) -> int | None:
    return None if x is None else round(float(x) * 100)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=ROOT / "out")
    ap.add_argument("--expected", type=Path, default=ROOT / "eval" / "expected.json")
    args = ap.parse_args()
    if not args.out.is_dir():
        print(f"No output folder {args.out}. Run the agent first (python run.py ...).", file=sys.stderr)
        return 2

    expected = json.loads(args.expected.read_text(encoding="utf-8"))["emails"]
    sev = {k: v["severity"] for k, v in json.loads((ROOT / "config" / "flags.json").read_text())["flags"].items()}

    tot = {"emails": 0, "intent": 0, "create_quote": 0, "customer": 0, "total": 0, "total_n": 0,
           "lines_ok": 0, "lines_n": 0, "critical": 0, "missing": 0,
           "tp": 0, "fp": 0, "fn": 0, "info_tp": 0, "info_n": 0}
    rows = []
    for exp in expected:
        eid = exp["id"]
        path = args.out / f"{eid}.json"
        tot["emails"] += 1
        if not path.exists():
            tot["missing"] += 1
            rows.append((eid, "MISSING", ""))
            continue
        rec = json.loads(path.read_text(encoding="utf-8"))
        notes, crit = [], []

        ok_intent = rec.get("intent") == exp["intent"]
        tot["intent"] += ok_intent
        if not ok_intent:
            notes.append(f"intent {rec.get('intent')} != {exp['intent']}")
        created = rec.get("record_type") == "quote"
        tot["create_quote"] += created == exp["create_quote"]
        if created != exp["create_quote"]:
            notes.append(f"create_quote {created} != {exp['create_quote']}")
        cid = (rec.get("customer") or {}).get("customer_id")
        if cid == exp["customer"]["customer_id"]:
            tot["customer"] += 1
        else:
            crit.append(f"customer {cid} != {exp['customer']['customer_id']}")

        # lines
        act_priced = {}
        for l in rec.get("lines", []):
            if l.get("unit_price") is not None:
                act_priced.setdefault(l["sku"], []).append(l)
        for el in exp["lines"]:
            if el.get("unit_price") is None:
                continue
            tot["lines_n"] += 1
            cands = act_priced.get(el["sku"], [])
            hit = next((l for l in cands if l["quantity"] == el["qty"] and cents(l["unit_price"]) == cents(el["unit_price"])
                        and cents(l["amount"]) == cents(el["amount"])), None)
            if hit:
                tot["lines_ok"] += 1
                cands.remove(hit)
            else:
                notes.append(f"line {el['sku']} x{el['qty']} @{el['unit_price']} not matched")
        for sku, leftover in act_priced.items():           # matched lines were removed above
            for l in leftover:
                crit.append(f"priced line not in expected output: {sku} x{l['quantity']} @{l['unit_price']}")
        for sku in exp.get("forbidden_skus", []):
            if any(l.get("sku") == sku for l in rec.get("lines", [])):
                crit.append(f"forbidden SKU {sku} present")

        if exp["create_quote"]:
            tot["total_n"] += 1
            if cents(rec.get("order_total")) == cents(exp["order_total"]):
                tot["total"] += 1
            else:
                notes.append(f"total {rec.get('order_total')} != {exp['order_total']}")

        # flags
        af, ef = all_flags(rec, True), all_flags(exp, False)
        must = {f for f in ef if sev[f] in ("blocker", "warning")}
        got = {f for f in af if sev[f] in ("blocker", "warning")}
        tp, fn, fp = must & got, must - got, (got - must) - SOFT
        tot["tp"] += len(tp); tot["fn"] += len(fn); tot["fp"] += len(fp)
        info_e = {f for f in ef if sev[f] == "info"}
        tot["info_n"] += len(info_e); tot["info_tp"] += len(info_e & af)
        if fn:
            notes.append(f"missing flags {sorted(fn)}")
        if fp:
            notes.append(f"extra flags {sorted(fp)}")
        for f in exp.get("forbidden_flags", []):
            if f in af:
                crit.append(f"forbidden flag {f}")

        tot["critical"] += len(crit)
        status = "CRITICAL" if crit else ("OK" if not notes else "PARTIAL")
        rows.append((eid, status, "; ".join(crit + notes)))

    pct = lambda a, b: f"{100 * a / b:5.1f}%" if b else "  n/a"        # noqa: E731
    print(f"{'ID':<5} {'RESULT':<9} NOTES")
    for r in rows:
        print(f"{r[0]:<5} {r[1]:<9} {r[2]}")
    n = tot["emails"]
    prec = tot["tp"] / (tot["tp"] + tot["fp"]) if tot["tp"] + tot["fp"] else 1.0
    rec_ = tot["tp"] / (tot["tp"] + tot["fn"]) if tot["tp"] + tot["fn"] else 1.0
    summary = {
        "emails": n, "fully_correct": sum(1 for r in rows if r[1] == "OK"),
        "intent_accuracy": round(tot["intent"] / n, 3), "create_quote_accuracy": round(tot["create_quote"] / n, 3),
        "customer_accuracy": round(tot["customer"] / n, 3),
        "priced_line_accuracy": round(tot["lines_ok"] / tot["lines_n"], 3) if tot["lines_n"] else None,
        "order_total_accuracy": round(tot["total"] / tot["total_n"], 3) if tot["total_n"] else None,
        "flag_recall_blocker_warning": round(rec_, 3), "flag_precision_blocker_warning": round(prec, 3),
        "flag_recall_info": round(tot["info_tp"] / tot["info_n"], 3) if tot["info_n"] else None,
        "critical_errors": tot["critical"], "missing_outputs": tot["missing"],
    }
    print("\nSummary")
    print(f"  fully correct emails     {summary['fully_correct']}/{n}")
    print(f"  intent                   {pct(tot['intent'], n)}")
    print(f"  create quote vs triage   {pct(tot['create_quote'], n)}")
    print(f"  customer                 {pct(tot['customer'], n)}")
    print(f"  priced lines exact       {pct(tot['lines_ok'], tot['lines_n'])}  ({tot['lines_ok']}/{tot['lines_n']})")
    print(f"  order totals exact       {pct(tot['total'], tot['total_n'])}")
    print(f"  flags recall (blk+warn)  {pct(tot['tp'], tot['tp'] + tot['fn'])}")
    print(f"  flags precision          {100 * prec:5.1f}%")
    print(f"  info flags recall        {pct(tot['info_tp'], tot['info_n'])}")
    print(f"  CRITICAL errors          {tot['critical']}")
    (args.out / "eval_report.json").write_text(json.dumps({"summary": summary, "per_email": rows}, indent=2),
                                                encoding="utf-8")
    return 0 if tot["critical"] == 0 and tot["missing"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
