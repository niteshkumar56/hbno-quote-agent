"""Deterministic business rules: extraction -> draft quote (or triage record) with flags.

The model's output is treated as untrusted. It contributes: intent, product words, quantity,
pack size, ship-to, needed-by, customer-stated prices and attention notes. It never contributes
a SKU decision, a price, a credit decision or a stock decision.
"""
from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal

from . import config
from .dates import add_business_days, business_days_between, is_business_day
from .data import RefData
from .matching import _has_phrase, norm, products_mentioned, resolve
from .pricing import money, price_line, target_per_container

QUOTE_INTENTS = {"quote_request", "quote_change"}
WEIGHT_OR_LOOSE_UNITS = {"lb", "kg", "g", "gal", "l", "ml", "fl_oz"}
UNIT_TEXT = {"case": "by the case (units per case not confirmed)", "unknown": "in an unclear unit"}

# Number words a customer may use for a quantity (English, Spanish, French).
_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "single": 1, "two": 2, "pair": 2, "couple": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "dozen": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "hundred": 100,
    "un": 1, "una": 1, "uno": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7,
    "ocho": 8, "nueve": 9, "diez": 10, "docena": 12, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5,
    "sept": 7, "huit": 8, "neuf": 9, "dix": 10, "douzaine": 12,
}


def numbers_in(text: str) -> set[float]:
    """Every quantity a customer could mean in this text: digits, number words, 'N dozen'."""
    t = (text or "").lower()
    t = re.sub(r"(?<=\d),(?=\d{3}\b)", "", t)                       # 1,000 -> 1000
    out = {float(n) for n in re.findall(r"\d+(?:\.\d+)?", t)}
    words = re.findall(r"[a-záéíóúñç]+|\d+(?:\.\d+)?", t)
    for i, w in enumerate(words):
        if w in _NUMBER_WORDS:
            out.add(float(_NUMBER_WORDS[w]))
        if w in ("dozen", "docena", "douzaine") and i:
            prev = words[i - 1]
            mult = 0.5 if prev == "half" else (float(prev) if prev[0].isdigit() else _NUMBER_WORDS.get(prev))
            if prev == "a" and i >= 2 and words[i - 2] == "half":
                mult = 0.5
            if mult:
                out.add(mult * 12)
    return out


class Flags:
    def __init__(self, ref: RefData):
        self.ref = ref
        self.items: list[dict] = []

    def add(self, code: str, detail: str = "", source: str | None = None):
        existing = next((f for f in self.items if f["code"] == code), None)
        if existing:
            if detail and detail not in existing["detail"]:
                existing["detail"] = f"{existing['detail']} | {detail}" if existing["detail"] else detail
            return
        meta = self.ref.flags["flags"][code]
        self.items.append({"code": code, "severity": meta["severity"], "detail": detail,
                           "source": source or meta["source"]})

    def codes(self) -> set[str]:
        return {f["code"] for f in self.items}


# --------------------------------------------------------------------------- output guard
def _loose(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def verify_source_text(line: dict, prepared: dict) -> bool:
    """A line must quote the customer's NEW message or subject. Blocks invented lines and lines
    lifted from quoted history."""
    hay = _loose(prepared["body"]) + " \n " + _loose(prepared["subject"])
    st = _loose(line.get("source_text", ""))
    if st and st in hay:
        return True
    # tolerate the model collapsing a table row's spacing: compare on word sequence
    words = re.findall(r"[a-z0-9/]+", st)
    hay_words = " ".join(re.findall(r"[a-z0-9/]+", hay))
    return bool(words) and " ".join(words) in hay_words


# --------------------------------------------------------------------------- customer
def find_customer(ref: RefData, sender: str) -> tuple[dict | None, str]:
    domain = sender.rsplit("@", 1)[-1].strip().lower() if "@" in sender else ""
    if domain in ref.customers:
        return ref.customers[domain], domain
    for d, c in ref.customers.items():                 # mail.bloombath.example -> bloombath.example
        if domain.endswith("." + d):
            return c, domain
    return None, domain


_COMPANY_SUFFIXES = {"co", "company", "inc", "llc", "ltd", "corp", "corporation", "the"}


def named_customers(ref: RefData, text: str) -> list[dict]:
    """Known customers whose company name appears in the email text (checked locally, never sent
    to the model). Used only to warn the rep; never to link the email to an account."""
    t = norm(text)
    out = []
    for c in ref.customers.values():
        words = [w for w in norm(c["company"]).split() if w not in _COMPANY_SUFFIXES]
        name = " ".join(words)
        if len(name) >= 5 and _has_phrase(t, name):
            out.append(c)
    return out


_FWD_SUBJECT = re.compile(r"^\s*(?:fwd?|fw)\s*:", re.I)
_FWD_BODY = re.compile(r"^\s*(?:-{2,}\s*forwarded message|begin forwarded message:)", re.I | re.M)


def is_forwarded(email: dict) -> bool:
    return bool(_FWD_SUBJECT.match(email.get("subject") or "") or _FWD_BODY.search(email.get("body") or ""))


def is_internal(email: dict, domain: str) -> bool:
    to = str(email.get("to") or "")
    to_domain = to.rsplit("@", 1)[-1].strip().lower() if "@" in to else ""
    return bool(domain) and (domain == to_domain or domain in config.INTERNAL_DOMAINS)


# --------------------------------------------------------------------------- lines
def build_line(ref: RefData, n: int, x: dict, message_text: str = "") -> dict:
    """message_text: the customer's new message + subject (redacted), used to verify the quantity."""
    lf = Flags(ref)
    m = resolve(ref, x.get("product_text", ""), x.get("catalog_product"),
                float(x.get("product_confidence", 0) or 0), x.get("catalog_candidates") or [])
    # product_text is written by the model. The product must also be named in the customer's own
    # words (source_text, verified verbatim against the email), or the line is not auto-quoted.
    if m["product"] and m["status"] == "matched" and m["product"] not in products_mentioned(ref, x.get("source_text", "")):
        m = {**m, "status": "unconfirmed", "confidence": 0.0,
             "reason": f"'{x.get('product_text')}' is not supported by the customer's words '{x.get('source_text')}'"}
    product = m["product"]
    pack = x.get("pack_size")
    qty = x.get("quantity")
    unit = x.get("quantity_unit")
    line = {
        "line_no": n, "line_ref": x.get("line_ref"),
        "request_text": x.get("source_text"), "product_text": x.get("product_text"),
        "product": product, "pack_size": pack, "pack_size_text": x.get("pack_size_text"),
        "quantity": qty, "quantity_unit": unit, "ship_to_text": x.get("ship_to_text"),
        "sku": None, "unit_price": None, "amount": None, "match_confidence": m["confidence"],
        "match_status": m["status"], "match_reason": m["reason"],
        "candidate_skus": [], "flags": lf.items,
    }

    def candidate_skus(products: list[str]) -> list[str]:
        out = []
        for p in products:
            packs = ref.products[p].packs
            out += [packs[pack]] if pack in packs else list(packs.values())
        return out

    # 1. product
    if m["status"] == "no_match":
        lf.add("SKU_NO_MATCH", f"'{x.get('product_text')}' is not in the catalog. {m['reason']}")
    elif m["status"] == "ambiguous":
        lf.add("SKU_AMBIGUOUS", f"'{x.get('product_text')}' could be: {', '.join(m['candidates'])}")
    elif m["status"] == "unconfirmed" or m["confidence"] < config.MATCH_THRESHOLD:
        lf.add("LOW_MATCH_CONFIDENCE", f"confidence {m['confidence']:.2f} < {config.MATCH_THRESHOLD}; {m['reason']}")
    line["candidate_skus"] = candidate_skus(m["candidates"]) if m["candidates"] else []
    line["candidate_products"] = m["candidates"]

    # 2. unit / pack size
    blocked = bool(lf.items)
    # A quantity is only a container count when the unit says so. 'N gal' in 1 gal packs is the
    # same count. Anything else (lb, kg, liters, cases...) is never converted, even if the model
    # also filled in a pack size.
    counts_containers = unit == "container" or (unit == "gal" and pack == "1 gal")
    if pack is None:
        # 'unknown' with no pack just means the size is missing: let the pack-size checks speak.
        if unit in WEIGHT_OR_LOOSE_UNITS or unit == "case":
            lf.add("UNIT_NOT_SELLABLE", f"requested {qty} {UNIT_TEXT.get(unit, unit)}; sold only as a count of "
                   "4 oz / 16 oz / 1 gal / 5 gal / drum packs")
        elif x.get("pack_size_text"):
            lf.add("PACK_SIZE_UNAVAILABLE", f"'{x['pack_size_text']}' is not a pack size we sell")
        else:
            lf.add("PACK_SIZE_MISSING", "customer did not state a pack size")
    else:
        if not counts_containers and qty is not None:
            lf.add("UNIT_NOT_SELLABLE", f"requested {qty} {UNIT_TEXT.get(unit, unit)}, not a count of {pack} "
                   "containers; not converted")
        if product and pack not in ref.products[product].packs:
            lf.add("PACK_SIZE_UNAVAILABLE", f"{product} is not sold in {pack}; available: "
                   f"{', '.join(ref.products[product].packs)}")

    # 3. quantity
    if qty is None:
        if not x.get("is_sample"):                      # samples are routed, not priced
            lf.add("QTY_MISSING", "no quantity stated")
    elif float(qty) != int(qty) or qty <= 0 or qty > config.MAX_QTY:
        lf.add("QTY_MISSING", f"quantity {qty} is not a whole number of containers in range")
    elif float(qty) not in numbers_in(x.get("source_text", "")) and float(qty) not in numbers_in(message_text):
        lf.add("QTY_NOT_IN_SOURCE", f"quantity {qty} does not appear in the customer's message")
    if x.get("quantity_is_approximate"):
        lf.add("QTY_UNCERTAIN", f"customer hedged the quantity ('{x.get('source_text')}')")

    # 4. SKU, active, price, stock
    sku = ref.products[product].packs.get(pack) if product and pack else None
    if sku and m["status"] == "matched" and m["confidence"] >= config.MATCH_THRESHOLD:
        line["sku"] = sku
        line["item_name"] = f"{product} {pack}"
        if not ref.skus[sku]["active"]:
            lf.add("SKU_INACTIVE", f"{sku} is inactive; not quoted. Rep may offer an alternative manually")
    blocking = any(f["severity"] == "blocker" for f in lf.items)
    if line["sku"] and not blocking:
        q = int(qty)
        p = price_line(ref, line["sku"], q)
        line.update(unit_price=p["unit_price"], amount=p["amount"], price_tier_min_qty=p["tier_min_qty"],
                    quantity=q)
        if "next_tier" in p:
            line["next_tier"] = p["next_tier"]
        for code, detail in p["flags"]:
            lf.add(code, detail)
        if p["valid_business_days"] < config.QUOTE_VALID_BUSINESS_DAYS:
            lf.add("LINE_VALIDITY_SHORTER", f"price table allows {p['valid_business_days']} business days for "
                   f"{line['sku']}; quote says {config.QUOTE_VALID_BUSINESS_DAYS}")
        avail = ref.stock.get(line["sku"], 0)
        line["available_qty"] = avail
        if q > avail:
            lf.add("STOCK_INSUFFICIENT", f"requested {q}, available {avail}")
    line["_blocked"] = blocking or blocked
    return line


# --------------------------------------------------------------------------- record
def build_record(ref: RefData, email: dict, prepared: dict, extraction: dict, received_date: dt.date,
                 draft_date: dt.date | None = None) -> dict:
    """received_date: when the customer sent the email. draft_date: when this draft is created
    (defaults to received_date, which keeps tests reproducible). The quote is dated, and its
    validity counted, from the draft date."""
    quote_date = max(draft_date or received_date, received_date)
    hf = Flags(ref)
    intent = extraction["intent"]
    customer, domain = find_customer(ref, email["sender"])

    # Lines (with output guard)
    lines, dropped = [], []
    message_text = prepared["body"] + "\n" + prepared["subject"]
    for x in extraction.get("lines", []):
        if not verify_source_text(x, prepared):
            dropped.append(x.get("source_text"))
            continue
        lines.append(build_line(ref, len(lines) + 1, x, message_text))
    if dropped:
        hf.add("CLARIFICATION_NEEDED", f"{len(dropped)} extracted line(s) not found in the customer's message "
               f"were discarded: {dropped}", source="code")
    if prepared.get("text_below_quote"):
        hf.add("CLARIFICATION_NEEDED", "customer wrote text below the quoted history; it was not processed. "
               "Read the original email", source="code")

    # Intent / attention flags from the model
    if intent in ref.flags["intent_to_flag"]:
        hf.add(ref.flags["intent_to_flag"][intent], f"intent: {intent}", source="llm")
    # A second purpose must never be lost. Only an order reference changes what the quote needs.
    for s in extraction.get("secondary_intents", []):
        if s in QUOTE_INTENTS or s == intent:
            continue
        if s == "reorder_by_reference":
            hf.add("NEEDS_ORDER_HISTORY", "secondary intent: refers to a previous order", source="llm")
        else:
            hf.add("SECONDARY_REQUEST", f"also: {s} (route: {config.ROUTING.get(s, 'Inbox review')})", source="llm")
    for a in extraction.get("rep_attention", []):
        code = ref.flags["llm_attention_to_flag"].get(a["code"])
        if code:
            hf.add(code, a.get("detail", ""), source="llm")
    if extraction.get("language", "en") != "en":
        hf.add("NON_ENGLISH", f"language: {extraction['language']}")
    for d in extraction.get("referenced_documents", []):
        if d["type"] == "attachment":
            hf.add("ATTACHMENT_REFERENCED", d["ref"])

    # Customer-stated prices (data only; never applied)
    # A request names the model's line_ref (lines are renumbered after dropped lines, so line_no
    # can't be used). A request for a dropped line attaches to nothing. A percentage with no line
    # applies to every priced line; any other request with no line only if there is exactly one.
    priced = [l for l in lines if l["unit_price"] is not None]
    for req in extraction.get("customer_price_requests", []):
        hf.add("DISCOUNT_REQUEST", req.get("text", ""), source="code")
        ref_no = req.get("line_ref")
        if ref_no is not None:
            targets = [l for l in priced if l.get("line_ref") == ref_no]
        elif req.get("kind") == "discount_percent" or req.get("per") == "percent":
            targets = priced
        else:
            targets = priced if len(priced) == 1 else []
        for l in targets:
            t = target_per_container(req, l["pack_size"], l["unit_price"])
            if t is None:
                continue
            l["customer_target_price"] = t
            fl = Flags(ref); fl.items = l["flags"]
            floor = price_line(ref, l["sku"], l["quantity"])["price_floor"]
            if t < floor:
                fl.add("CUSTOMER_PRICE_BELOW_FLOOR", f"customer asked {t}/unit ({req.get('text', '')}); floor is "
                       f"{floor}; table price {l['unit_price']} kept")
            elif t < l["unit_price"]:
                fl.add("CUSTOMER_PRICE_REQUEST", f"customer asked {t}/unit ({req.get('text', '')}); "
                       f"table price {l['unit_price']} kept")

    # Ship-to / needed-by
    ship_to = extraction.get("ship_to")
    if any(l.get("ship_to_text") for l in lines):
        hf.add("MULTIPLE_SHIP_TO", "; ".join(f"line {l['line_no']}: {l['ship_to_text']}" for l in lines if l.get("ship_to_text")))
    nb = extraction.get("needed_by")
    needed_by_date = None
    if nb:
        if nb.get("is_vague"):
            hf.add("NEEDED_BY_VAGUE", f"'{nb.get('text')}'")
        if nb.get("date"):
            try:
                needed_by_date = dt.date.fromisoformat(nb["date"])
            except ValueError:
                needed_by_date = None
        if needed_by_date:
            if not is_business_day(needed_by_date, ref.holidays):
                hf.add("NEEDED_BY_NON_BUSINESS_DAY", f"{needed_by_date.isoformat()} is a {needed_by_date:%A}")
            # Short notice is a property of the request (counted from receipt). A date that has
            # already passed by the time the draft is made is at risk too.
            if business_days_between(received_date, needed_by_date, ref.holidays) <= 5:
                hf.add("NEEDED_BY_AT_RISK", f"needed by {needed_by_date.isoformat()}, requested {received_date.isoformat()}")
            elif needed_by_date < quote_date:
                hf.add("NEEDED_BY_AT_RISK", f"needed by {needed_by_date.isoformat()} has passed (draft dated {quote_date.isoformat()})")

    is_quote = intent in QUOTE_INTENTS
    if is_quote and not lines:
        hf.add("NO_LINES_FOUND", "quote-like email but no product lines could be extracted")

    # Same SKU on several lines
    by_sku: dict[str, list[dict]] = {}
    for l in lines:
        if l["unit_price"] is not None:
            by_sku.setdefault(l["sku"], []).append(l)
    for sku, ls in by_sku.items():
        if len(ls) > 1:
            total_q = sum(l["quantity"] for l in ls)
            combined = price_line(ref, sku, total_q)
            current = sum(l["amount"] for l in ls)
            if combined["amount"] < current:
                hf.add("TIER_CONSOLIDATION_POSSIBLE", f"{sku}: {total_q} combined at {combined['unit_price']} = "
                       f"{combined['amount']} vs {current} priced per line (saves {money(current - combined['amount'])})")

    # Stock is per SKU: two lines of the same SKU draw on the same stock.
    for sku, ls in by_sku.items():
        total_q, avail = sum(l["quantity"] for l in ls), ref.stock.get(sku, 0)
        if len(ls) > 1 and total_q > avail:
            for l in ls:
                fl = Flags(ref); fl.items = l["flags"]
                fl.add("STOCK_INSUFFICIENT", f"lines {', '.join(str(x['line_no']) for x in ls)} together request "
                       f"{total_q} of {sku}, available {avail}")

    order_total = money(sum((l["amount"] for l in lines if l["amount"] is not None), Decimal(0)))
    unpriced = sum(1 for l in lines if l["amount"] is None)

    # Customer / credit
    forwarded, internal = is_forwarded(email), is_internal(email, domain)
    if forwarded or internal:
        hf.add("FORWARDED_EMAIL", "sent from an internal address: the original customer is not the sender"
               if internal else "the customer forwarded an earlier message; check who the request is for")
    if internal:
        cust_block = {"type": "unknown_forwarded", "customer_id": None, "company": None, "email_domain": domain,
                      "contact_email": None}
    elif customer is None:
        hf.add("NEW_LEAD", f"unknown domain {domain}")
        hf.add("CREDIT_NOT_ESTABLISHED", "no terms on file: credit application or prepayment needed")
        if domain in config.FREEMAIL_DOMAINS:
            hf.add("FREEMAIL_DOMAIN", domain)
        if not ship_to and is_quote:
            hf.add("SHIP_TO_MISSING", "no ship-to stated and no address on file")
        for c in named_customers(ref, (email.get("subject") or "") + "\n" + (email.get("body") or "")):
            hf.add("POSSIBLE_EXISTING_CUSTOMER", f"email names {c['company']} ({c['customer_id']}, "
                   f"{c['email_domain']}); not linked, confirm identity first")
        cust_block = {"type": "new_lead", "customer_id": None, "company": None, "email_domain": domain,
                      "contact_email": email["sender"]}
    else:
        cust_block = {"type": "existing", "customer_id": customer["customer_id"], "company": customer["company"],
                      "email_domain": domain, "terms": customer["terms"], "contact_email": email["sender"]}
        if is_quote:
            if customer["credit_hold"].upper() == "Y":
                hf.add("CREDIT_HOLD", f"{customer['company']} is on credit hold")
            if customer["terms"].strip().lower() == "prepaid":
                hf.add("PREPAID_TERMS", "prepaid account: no credit check; payment required before release")
            else:
                limit, bal = Decimal(customer["credit_limit_usd"]), Decimal(customer["open_balance_usd"])
                after = bal + order_total
                if after > limit:
                    note = (f"; total excludes {unpriced} unpriced line{'s' if unpriced != 1 else ''}, "
                            "so the overage grows once they are priced") if unpriced else ""
                    hf.add("CREDIT_LIMIT_EXCEEDED", f"open balance {money(bal)} + this quote {order_total} = "
                           f"{money(after)} > limit {money(limit)} (over by {money(after - limit)}){note}")

    all_flags = hf.items + [f for l in lines for f in l["flags"]]
    review_state = "needs_rep_action" if any(f["severity"] == "blocker" for f in all_flags) else "ready_for_review"

    for l in lines:
        l.pop("_blocked", None)

    record = {
        "record_type": "quote" if is_quote else "triage",
        "quote_number": None,
        "status": config.QUOTE_STATUS if is_quote else "Triage - not a quote",
        "review_state": review_state,
        "route_to": None if is_quote else config.ROUTING.get(intent, "Inbox review"),
        "intent": intent,
        "source": {"email_id": email["id"], "sender": email["sender"], "subject": email.get("subject"),
                   "received_at": email.get("date")},
        "customer": cust_block,
        "received_date": received_date.isoformat(),
        "quote_date": quote_date.isoformat(),
        "expiry_date": add_business_days(quote_date, config.QUOTE_VALID_BUSINESS_DAYS, ref.holidays).isoformat()
                       if is_quote else None,
        "currency": "USD",
        "ship_to": ship_to,
        "needed_by": {"text": nb.get("text"), "date": needed_by_date.isoformat() if needed_by_date else None} if nb else None,
        "lines": lines,
        "order_total": order_total if is_quote else None,
        "flags": hf.items,
        "summary_for_rep": extraction.get("summary_for_rep"),
        "referenced_documents": extraction.get("referenced_documents", []),
    }
    return record


def netsuite_estimate(record: dict) -> dict | None:
    """The record reshaped as a NetSuite REST Estimate payload (not posted; for review).

    - An Estimate needs an existing customer/prospect as `entity`. For a new lead, `entity` is null
      and `prerequisites` says to find or create the prospect first; the poster fills in its id.
    - Every requested line is kept. Unpriced (blocked) lines become Description-item lines with no
      rate, so the Estimate never silently drops a product the customer asked for.
    - Subsidiary and location are account-specific: placeholders from config.NETSUITE."""
    if record["record_type"] != "quote":
        return None
    c, ns = record["customer"], config.NETSUITE
    items = []
    for l in record["lines"]:
        line_flags = [f["code"] for f in l["flags"]]
        if l["unit_price"] is not None:
            items.append({"item": {"externalId": l["sku"]}, "quantity": l["quantity"],
                          "rate": float(l["unit_price"]), "amount": float(l["amount"]),
                          "price": {"id": "-1"},          # custom price level: rate fixed from the table
                          "custcol_ai_flags": line_flags})
        else:
            items.append({"item": {"externalId": ns["description_item"]},
                          "description": f"UNPRICED - needs rep: {l.get('request_text') or l.get('product_text')}",
                          "custcol_ai_flags": line_flags})
    prerequisites = [] if c.get("customer_id") else [
        {"action": "find_or_create_prospect", "email": c.get("contact_email"), "company": c.get("company"),
         "then": "set entity to the prospect's internal id"}]
    return {
        "externalId": record["idempotency_key"],
        "entity": {"externalId": c["customer_id"]} if c.get("customer_id") else None,
        "prerequisites": prerequisites,
        "subsidiary": ns["subsidiary"], "location": ns["location"], "currency": {"refName": record["currency"]},
        "entityStatus": ns["entity_status"],
        "tranDate": record["quote_date"],
        "dueDate": record["expiry_date"],
        "memo": f"AI draft from email {record['source']['email_id']}: {record['source']['subject']}",
        "custbody_ai_review_state": record["review_state"],
        "custbody_ai_flags": [f["code"] for f in record["flags"]],
        "custbody_needed_by": (record.get("needed_by") or {}).get("date"),
        "shipAddress": (record.get("ship_to") or {}).get("text"),
        "item": {"items": items},
    }
