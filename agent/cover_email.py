"""Cover email drafts, built from a code template (not the model), so prices can only come from the
price table. Internal notes (credit, flags) are kept in a separate section that is never sent."""
from __future__ import annotations

import datetime as dt

SEV_ORDER = {"blocker": 0, "warning": 1, "info": 2}


def _usd(x) -> str:
    return f"${float(x):,.2f}"


def _date(s: str | None) -> str:
    return dt.date.fromisoformat(s).strftime("%b %d, %Y") if s else ""


def _question(l: dict) -> str | None:
    codes = {f["code"] for f in l["flags"]}
    what = l.get("product_text") or "this item"
    if "SKU_NO_MATCH" in codes:
        return f"We don't currently list \"{what}\". Would an alternative from our range work? Your rep can suggest options."
    if "SKU_AMBIGUOUS" in codes:
        opts = l.get("candidate_products") or l.get("candidate_skus", [])
        return f"For \"{what}\", which product did you mean: {' or '.join(opts)}?"
    if "SKU_INACTIVE" in codes:
        return f"\"{what}\" is no longer available. Your rep will follow up with an alternative."
    if "UNIT_NOT_SELLABLE" in codes:
        return (f"You asked for {l.get('quantity')} {l.get('quantity_unit')} of {what}. We sell in 4 oz, 16 oz, "
                f"1 gal, 5 gal and 55 gal drum containers. Which container sizes and how many would you like?")
    if "PACK_SIZE_UNAVAILABLE" in codes:
        return f"\"{l.get('pack_size_text') or l.get('pack_size')}\" isn't a size we offer for {what}. Which of our pack sizes would suit you?"
    if "PACK_SIZE_MISSING" in codes:
        return f"Which pack size would you like for {what} (4 oz, 16 oz, 1 gal, 5 gal or 55 gal drum)?"
    if "QTY_MISSING" in codes:
        return f"How many units of {what} would you like?"
    if "LOW_MATCH_CONFIDENCE" in codes:
        return f"Could you confirm the exact product you need for \"{what}\"?"
    return None


def render(record: dict) -> str:
    c = record["customer"]
    rep = ["=" * 72, "REP NOTES - INTERNAL, DO NOT SEND", "=" * 72]
    num = record.get("quote_number") or "(triage, no quote)"
    rep.append(f"Record: {num} | Status: {record['status']} | Review: {record['review_state']}")
    who = c.get("company") or ("UNKNOWN - forwarded internally" if c.get("type") == "unknown_forwarded" else "NEW LEAD")
    rep.append(f"Customer: {who} ({c.get('customer_id') or c.get('email_domain')})")
    if record.get("route_to"):
        rep.append(f"Route to: {record['route_to']}")
    if record.get("summary_for_rep"):
        rep.append(f"Summary: {record['summary_for_rep']}")
    flags = sorted(record["flags"] + [dict(f, detail=f"line {l['line_no']}: {f['detail']}")
                                      for l in record["lines"] for f in l["flags"]],
                   key=lambda f: SEV_ORDER[f["severity"]])
    for f in flags:
        rep.append(f"  [{f['severity'].upper():7}] {f['code']}: {f['detail']}")
    for q, t in (record.get("rep_context", {}).get("other_open_drafts") or []):
        rep.append(f"  [INFO   ] other open draft for this customer: {q} ({_usd(t)})")
    rep.append("Nothing has been sent. Review, edit and send manually.")
    rep.append("")

    to = c.get("contact_email") or "(identify the customer first)"
    if record["record_type"] != "quote":
        rep += ["=" * 72, "SUGGESTED ACKNOWLEDGEMENT (edit before sending)", "=" * 72,
                f"To: {to}",
                f"Subject: Re: {record['source']['subject']}", "",
                "Hello,", "",
                "Thank you for your message. We've passed it to the right person on our team and "
                "will get back to you shortly.", "", "Best regards,", "HBNO Sales"]
        return "\n".join(rep) + "\n"

    priced = [l for l in record["lines"] if l["unit_price"] is not None]
    questions = [q for l in record["lines"] if (q := _question(l))]
    body = ["=" * 72, "DRAFT EMAIL TO CUSTOMER", "=" * 72,
            f"To: {to}",
            f"Subject: HBNO quote {record['quote_number']} - Re: {record['source']['subject']}", "",
            f"Hello{(' ' + c['company'] + ' team') if c.get('company') else ''},", "",
            "Thank you for your request. " + ("Please find our quote below." if priced else
                                              "Before we can price your request we need a few details.")]
    if priced:
        body += ["", f"{'Item':<44}{'Qty':>5}{'Unit price':>13}{'Amount':>13}", "-" * 75]
        for l in priced:
            body.append(f"{(l['sku'] + ' ' + l.get('item_name', ''))[:43]:<44}{l['quantity']:>5}"
                        f"{_usd(l['unit_price']):>13}{_usd(l['amount']):>13}")
        body += ["-" * 75, f"{'Total (USD, excl. freight and tax)':<62}{_usd(record['order_total']):>13}", ""]
        body.append(f"Quote valid until {_date(record['expiry_date'])}.")
        if c.get("terms"):
            body.append(f"Payment terms: {c['terms']}.")
        short = [l for l in priced if any(f["code"] == "STOCK_INSUFFICIENT" for f in l["flags"])]
        for l in short:
            body.append(f"Availability: we currently have {l.get('available_qty', 0)} of {l['sku']} in stock; "
                        f"we'll confirm lead time for the balance.")
        if record.get("needed_by") and record["needed_by"].get("date"):
            body.append(f"We've noted your need-by date of {_date(record['needed_by']['date'])}.")
        if record.get("ship_to"):
            body.append(f"Ship to: {record['ship_to'].get('text')}.")
    if questions:
        body += ["", "A few questions so we can complete your quote:"]
        body += [f"  {i}. {q}" for i, q in enumerate(questions, 1)]
    body += ["", "Best regards,", "HBNO Sales"]
    return "\n".join(rep + body) + "\n"
