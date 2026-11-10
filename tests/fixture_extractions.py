"""HAND-WRITTEN test fixtures: what a correct extraction of each sample email looks like.

Purpose: test the deterministic layers (matching, pricing, credit, stock, dates, flags, store)
offline and reproducibly. These are NOT model output and are never used for submitted results.
"""

LAV = "Lavender Essential Oil 40/42"
LAVBG = "Lavender Essential Oil Bulgarian"
PEP = "Peppermint Essential Oil"
EUCG = "Eucalyptus Globulus Essential Oil"
EUCR = "Eucalyptus Radiata Essential Oil"
TEA = "Tea Tree Essential Oil"
LEM = "Lemon Essential Oil"
ORA = "Sweet Orange Essential Oil"
ROS = "Rosemary Essential Oil"
FRS = "Frankincense Serrata Essential Oil"
FRC = "Frankincense Carterii Essential Oil"
CLO = "Clove Bud Essential Oil"
JOJ = "Jojoba Oil Golden"
ALM = "Sweet Almond Oil"
FCO = "Fractionated Coconut Oil"
ARG = "Argan Oil"
GRP = "Grapeseed Oil"
CAS = "Castor Oil"


def L(src, text, product, conf, qty, pack, unit="container", cands=None, approx=False,
      pack_text=None, sample=False, ship=None):
    return {"line_ref": 0, "source_text": src, "product_text": text, "catalog_product": product,
            "catalog_candidates": cands or ([product] if product else []), "product_confidence": conf,
            "quantity": qty, "quantity_is_approximate": approx, "quantity_unit": unit, "pack_size": pack,
            "pack_size_text": pack_text, "is_sample": sample, "ship_to_text": ship}


def E(intent, lines=(), lang="en", ship=None, nb=None, prices=(), docs=(), attn=(), secondary=(),
      summary="Customer request."):
    lines = list(lines)
    for i, l in enumerate(lines, 1):
        l["line_ref"] = i
    return {"intent": intent, "intent_confidence": 0.95, "secondary_intents": list(secondary), "language": lang,
            "lines": lines, "ship_to": ship, "needed_by": nb, "customer_price_requests": list(prices),
            "referenced_documents": [{"type": t, "ref": r} for t, r in docs],
            "rep_attention": [{"code": c, "detail": d} for c, d in attn], "summary_for_rep": summary}


def SHIP(text, city, region):
    return {"text": text, "city": city, "region": region, "country": "US"}


def NB(text, date=None, vague=False):
    return {"text": text, "date": date, "is_vague": vague}


Q = "quote_request"
FIXTURES = {
    "S01": E(Q, [L("10 x 1 gallon Lavender 40/42", "Lavender 40/42", LAV, 0.98, 10, "1 gal")]),
    "S02": E(Q, [L("Peppermint, 5 gal x 2", "Peppermint", PEP, 0.97, 2, "5 gal"),
                 L("Eucalyptus globulus, 1 gal x 4", "Eucalyptus globulus", EUCG, 0.98, 4, "1 gal"),
                 L("Sweet orange, 5 gal x 3", "Sweet orange", ORA, 0.97, 3, "5 gal")],
             ship=SHIP("Portland, OR warehouse", "Portland", "OR"), nb=NB("Need by Dec 1.", "2026-12-01")),
    "S03": E(Q, [L("lav 40/42", "lav 40/42", LAV, 0.95, 6, None, approx=True),
                 L("some peppermint", "peppermint", PEP, 0.95, 6, None, approx=True)]),
    "S04": E(Q, [L("20 lbs of tea tree oil", "tea tree oil", TEA, 0.97, 20, None, unit="lb")]),
    "S05": E(Q, [L("8 x 16 oz eucalyptus radiata", "eucalyptus radiata", EUCR, 0.98, 8, "16 oz"),
                 L("4 x 1 gal sweet almond", "sweet almond", ALM, 0.97, 4, "1 gal")],
             nb=NB("Order for December", vague=True)),
    "S06": E(Q, [L("2 x 5 gallon jojoba", "jojoba", JOJ, 0.95, 2, "5 gal")]),
    "S07": E(Q, [L("Fractionated Coconut Oil | 55 gal | 2", "Fractionated Coconut Oil", FCO, 0.98, 2, "55 gal drum"),
                 L("Lavender 40/42 | 5 gal | 4", "Lavender 40/42", LAV, 0.98, 4, "5 gal"),
                 L("Rosemary | 1 gal | 6", "Rosemary", ROS, 0.97, 6, "1 gal")],
             docs=[("attachment", "SCL-PO-DRAFT-1182.pdf")]),
    "S08": E("reorder_by_reference", attn=[("REFERENCES_PRIOR_ORDER", "'Same as last time but double it'")]),
    "S09": E(Q, [L("10 x 1 gal lemon", "lemon", LEM, 0.97, 10, "1 gal")],
             prices=[{"line_ref": 1, "kind": "target_price", "amount": 120, "per": "gal",
                      "text": "Our budget is $120 per gallon"}],
             attn=[("DISCOUNT_OR_TERMS_REQUEST", "budget $120 per gallon")]),
    "S10": E(Q, [L("one 55 gallon drum of argan oil", "argan oil", ARG, 0.97, 1, "55 gal drum")]),
    "S11": E(Q, [L("lavandin grosso? If so quote 5 x 1 gal", "lavandin grosso", None, 0.2, 5, "1 gal")],
             attn=[("CONDITIONAL_REQUEST", "only if HBNO carries lavandin grosso")]),
    "S12": E(Q, [L("3 x 16 oz frankincense carterii", "frankincense carterii", FRC, 0.98, 3, "16 oz")]),
    "S13": E(Q, [L("4 galones de aceite de ricino (castor), 1 gal cada uno", "aceite de ricino (castor)", CAS, 0.95, 4, "1 gal"),
                 L("2 cubetas de 5 galones de aceite de semilla de uva", "aceite de semilla de uva", GRP, 0.92, 2, "5 gal")],
             lang="es"),
    "S14": E(Q, [L("2 x 5 gal clove bud", "clove bud", CLO, 0.98, 2, "5 gal")],
             ship=SHIP("Portland OR", "Portland", "OR"), nb=NB("needed by November 20", "2026-11-20")),
    "S15": E(Q, [L("a dozen 4oz rosemary", "rosemary", ROS, 0.97, 12, "4 oz")]),
    "S16": E(Q, [L("5 x 5 gal sweet orange", "sweet orange", ORA, 0.97, 5, "5 gal"),
                 L("3 x 5 gal lavender 40/42", "lavender 40/42", LAV, 0.98, 3, "5 gal")]),
    "S17": E(Q, [L("2 drums of golden jojoba (55 gal)", "golden jojoba", JOJ, 0.98, 2, "55 gal drum")]),
    "S18": E(Q, [L("FCO 5 gal x 6", "FCO", FCO, 0.93, 6, "5 gal"),
                 L("euc rad 16 oz x 10", "euc rad", EUCR, 0.93, 10, "16 oz")]),
    "S19": E(Q, [L("24 x 16 oz tea tree", "tea tree", TEA, 0.98, 24, "16 oz"),
                 L("24 x 16 oz peppermint", "peppermint", PEP, 0.98, 24, "16 oz")]),
    "S20": E("quote_change", [L("4 x 1 gal grapeseed", "grapeseed", GRP, 0.97, 4, "1 gal")],
             docs=[("quote", "Q-2210")], attn=[("REFERENCES_EXISTING_QUOTE", "add to quote Q-2210")]),
    "S21": E("sample_request", [L("2 oz samples of your Bulgarian lavender", "Bulgarian lavender", LAVBG, 0.97, None, None,
                                  pack_text="2 oz", sample=True),
                                L("lemon so we can test them", "lemon", LEM, 0.95, None, None, pack_text="2 oz", sample=True)]),
    "S22": E(Q, [L("5 x 1 gal frankincense serrata", "frankincense serrata", FRS, 0.98, 5, "1 gal")],
             nb=NB("by Nov 15", "2026-11-15")),
    "S23": E(Q, [L("3 x 1 gal lemon", "lemon", LEM, 0.97, 3, "1 gal"),
                 L("2 more gallons of lemon for the second warehouse", "lemon", LEM, 0.9, 2, "1 gal",
                   ship="second warehouse")],
             attn=[("MULTIPLE_SHIP_TO", "2 gal go to a second warehouse (address not given)")]),
    "S24": E(Q, [L("1 gallon castor oil", "castor oil", CAS, 0.97, 1, "1 gal"),
                 L("two 4oz clove", "clove", CLO, 0.93, 2, "4 oz")]),
    "S25": E(Q, [L("25 kg sweet almond oil", "sweet almond oil", ALM, 0.97, 25, None, unit="kg")]),
    "S26": E(Q, [L("6 x 16 oz peppermint", "peppermint", PEP, 0.98, 6, "16 oz")],
             prices=[{"line_ref": None, "kind": "discount_percent", "amount": 50, "per": "percent",
                      "text": "apply a 50% discount"}],
             attn=[("EMBEDDED_INSTRUCTIONS", "P.S. tells the processor to ignore pricing rules"),
                   ("DISCOUNT_OR_TERMS_REQUEST", "50% discount, claimed management approval")]),
    "S27": E("order_status", docs=[("sales_order", "SO-55120")],
             attn=[("COMPLAINT_OR_ISSUE", "order SO-55120 is late")]),
    "S28": E(Q, [L("Sweet almond 55 gal drum x 3", "Sweet almond", ALM, 0.97, 3, "55 gal drum"),
                 L("Grapeseed 55 gal drum x 2", "Grapeseed", GRP, 0.97, 2, "55 gal drum"),
                 L("Castor 5 gal x 6", "Castor", CAS, 0.97, 6, "5 gal")],
             ship=SHIP("Irvine, CA", "Irvine", "CA"), nb=NB("Need by Dec 10", "2026-12-10")),
    "S29": E(Q, [L("2 x 5 gal Bulgarian lavender", "Bulgarian lavender", LAVBG, 0.98, 2, "5 gal")]),
    "S30": E(Q, [L("eucalyptus, 1 gallon x 3", "eucalyptus", None, 0.4, 3, "1 gal", cands=[EUCG, EUCR])]),
}
