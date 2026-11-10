"""Tests for the fixes agreed in the design review (Q-numbers refer to that review)."""
from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from agent import redact
from agent.pipeline import process_email
from agent.rules import build_record
from agent.store import Store
from tests.test_guardrails import EMAILS, FX, REF, build


def codes(flags) -> set[str]:
    return {f["code"] for f in flags}


def all_codes(rec) -> set[str]:
    return codes(rec["flags"]) | {f["code"] for l in rec["lines"] for f in l["flags"]}


def ex(eid):
    return copy.deepcopy(FX.extract_for(eid).extraction)


# ---------------------------------------------------------------- Q8: model-written fields are verified
class ModelFieldsVerified(unittest.TestCase):
    def test_renamed_product_is_not_priced(self):
        # Model rewrites 'lavandin grosso' as 'lavender 40/42'; source_text is genuine.
        e = ex("S11")
        e["lines"][0].update(product_text="lavender 40/42", catalog_product="Lavender Essential Oil 40/42",
                             product_confidence=0.99)
        line = build("S11", e)["lines"][0]
        self.assertIsNone(line["unit_price"])
        self.assertIn("LOW_MATCH_CONFIDENCE", codes(line["flags"]))

    def test_invented_quantity_is_not_priced(self):
        e = ex("S18")
        e["lines"][0]["quantity"] = 60                     # email says 'FCO 5 gal x 6'
        line = build("S18", e)["lines"][0]
        self.assertIsNone(line["unit_price"])
        self.assertIn("QTY_NOT_IN_SOURCE", codes(line["flags"]))

    def test_quantity_found_elsewhere_in_message_is_accepted(self):
        line = build("S03")["lines"][0]                    # 'maybe 6 of each' is outside source_text
        self.assertNotIn("QTY_NOT_IN_SOURCE", codes(line["flags"]))

    def test_number_words_accepted(self):
        self.assertIsNotNone(build("S15")["lines"][0]["unit_price"])    # 'a dozen'
        self.assertIsNotNone(build("S24")["lines"][1]["unit_price"])    # 'two 4oz clove'

    def test_line_naming_two_products_still_matches(self):
        email = dict(EMAILS["S01"], id="T-MULTI", body="Please quote 5 gal each of peppermint and lemon, 2 of each.")
        e = ex("S01")
        e["lines"][0].update(source_text="5 gal each of peppermint and lemon", product_text="lemon",
                             catalog_product="Lemon Essential Oil", quantity=2, pack_size="5 gal")
        line = build("S01", e, email)["lines"][0]
        self.assertEqual(line["sku"], "EO-LEM-5GAL")
        self.assertIsNotNone(line["unit_price"])


# ---------------------------------------------------------------- Q9: non-container units always block
class NonContainerUnits(unittest.TestCase):
    def test_weight_unit_with_pack_size_is_not_priced(self):
        e = ex("S25")
        e["lines"][0].update(pack_size="55 gal drum")      # model wrongly fills a pack for '25 kg'
        line = build("S25", e)["lines"][0]
        self.assertIsNone(line["unit_price"])
        self.assertIn("UNIT_NOT_SELLABLE", codes(line["flags"]))

    def test_gallons_in_one_gallon_jugs_is_priced(self):
        e = ex("S23")
        e["lines"][1]["quantity_unit"] = "gal"             # '2 more gallons of lemon', pack 1 gal
        self.assertIsNotNone(build("S23", e)["lines"][1]["unit_price"])

    def test_unknown_unit_without_pack_is_a_missing_pack_size(self):
        # Live Sonnet output for S03 / S21: unit 'unknown', no pack size.
        e = ex("S03")
        for l in e["lines"]:
            l["quantity_unit"] = "unknown"
        self.assertIn("PACK_SIZE_MISSING", codes(build("S03", e)["lines"][0]["flags"]))
        self.assertNotIn("UNIT_NOT_SELLABLE", codes(build("S03", e)["lines"][0]["flags"]))
        e = ex("S21")
        for l in e["lines"]:
            l["quantity_unit"] = "unknown"
        self.assertIn("PACK_SIZE_UNAVAILABLE", all_codes(build("S21", e)))
        self.assertNotIn("UNIT_NOT_SELLABLE", all_codes(build("S21", e)))

    def test_cases_are_not_priced(self):
        e = ex("S01")
        e["lines"][0]["quantity_unit"] = "case"
        line = build("S01", e)["lines"][0]
        self.assertIsNone(line["unit_price"])
        self.assertIn("UNIT_NOT_SELLABLE", codes(line["flags"]))


# ---------------------------------------------------------------- Q10: price requests follow the model's line_ref
class PriceRequestLineRef(unittest.TestCase):
    def _with_dropped_first_line(self, req_ref):
        e = ex("S09")
        bogus = {**e["lines"][0], "line_ref": 1, "source_text": "4 x 5 gal argan", "product_text": "argan",
                 "catalog_product": "Argan Oil"}
        e["lines"][0]["line_ref"] = 2
        e["lines"].insert(0, bogus)                        # not in the email: dropped by the output guard
        e["customer_price_requests"][0]["line_ref"] = req_ref
        return build("S09", e)["lines"]

    def test_request_attaches_to_the_line_the_model_meant(self):
        lines = self._with_dropped_first_line(2)
        self.assertEqual(len(lines), 1)
        self.assertIn("CUSTOMER_PRICE_BELOW_FLOOR", codes(lines[0]["flags"]))

    def test_request_for_a_dropped_line_attaches_nowhere(self):
        lines = self._with_dropped_first_line(1)
        self.assertNotIn("customer_target_price", lines[0])
        self.assertFalse({"CUSTOMER_PRICE_BELOW_FLOOR", "CUSTOMER_PRICE_REQUEST"} & codes(lines[0]["flags"]))


# ---------------------------------------------------------------- Q11: percentage discounts vs the floor
class PercentDiscounts(unittest.TestCase):
    def test_fifty_percent_is_below_floor_and_price_unchanged(self):
        line = build("S26")["lines"][0]
        self.assertEqual(float(line["unit_price"]), 32.30)
        self.assertEqual(float(line["customer_target_price"]), 16.15)
        self.assertIn("CUSTOMER_PRICE_BELOW_FLOOR", codes(line["flags"]))

    def test_order_level_percent_applies_to_every_line(self):
        e = ex("S16")
        e["customer_price_requests"] = [{"line_ref": None, "kind": "discount_percent", "amount": 10,
                                         "per": "percent", "text": "10% off the order"}]
        rec = build("S16", e)
        for l in rec["lines"]:
            self.assertIn("CUSTOMER_PRICE_REQUEST", codes(l["flags"]), l["sku"])
        self.assertEqual(float(rec["order_total"]), 1824.00 + 3648.00)   # table prices unchanged


# ---------------------------------------------------------------- Q1 / Q2: expiry
class Expiry(unittest.TestCase):
    def test_expiry_counts_from_the_processing_date(self):
        rec = build_record(REF, EMAILS["S01"], redact.prepare(EMAILS["S01"], 20000), ex("S01"),
                           dt.date(2026, 11, 2), draft_date=dt.date(2026, 11, 10))
        self.assertEqual(rec["received_date"], "2026-11-02")
        self.assertEqual(rec["quote_date"], "2026-11-10")
        self.assertEqual(rec["expiry_date"], "2026-11-17")

    def test_pipeline_never_dates_a_draft_before_the_email(self):
        tmp = Path(tempfile.mkdtemp())
        store = Store(tmp / "q.db")
        try:
            r = process_email(EMAILS["S01"], REF, store, FX, tmp / "out", today=dt.date(2026, 10, 1))
            self.assertEqual(r["record"]["quote_date"], "2026-11-02")
            r = process_email(EMAILS["S02"], REF, store, FX, tmp / "out", today=dt.date(2026, 11, 10))
            self.assertEqual(r["record"]["quote_date"], "2026-11-10")
        finally:
            store.close()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_shorter_line_validity_is_flagged_not_applied(self):
        ref = copy.deepcopy(REF)
        ref.tiers["EO-LAV4042-1GAL"] = [dataclasses.replace(t, valid_days=3) for t in ref.tiers["EO-LAV4042-1GAL"]]
        rec = build_record(ref, EMAILS["S01"], redact.prepare(EMAILS["S01"], 20000), ex("S01"), dt.date(2026, 11, 2))
        self.assertEqual(rec["expiry_date"], "2026-11-09")                 # still 5 business days
        self.assertIn("LINE_VALIDITY_SHORTER", codes(rec["lines"][0]["flags"]))

    def test_needed_by_already_passed_is_at_risk(self):
        rec = build_record(REF, EMAILS["S02"], redact.prepare(EMAILS["S02"], 20000), ex("S02"),
                           dt.date(2026, 11, 2), draft_date=dt.date(2026, 12, 2))
        self.assertIn("NEEDED_BY_AT_RISK", codes(rec["flags"]))


# ---------------------------------------------------------------- Q15: invalid input still produces a record
class InvalidInput(unittest.TestCase):
    def test_bad_date_writes_a_triage_file(self):
        tmp = Path(tempfile.mkdtemp())
        store = Store(tmp / "q.db")
        try:
            bad = dict(EMAILS["S01"], id="BAD1", date="Mon, 02 Nov 2026 08:12:00 -0800")
            r = process_email(bad, REF, store, FX, tmp / "out")
            self.assertEqual(r["outcome"], "invalid_input")
            rec = json.loads((tmp / "out" / "BAD1.json").read_text(encoding="utf-8"))
            self.assertIn("INVALID_INPUT", codes(rec["flags"]))
            self.assertEqual(rec["record_type"], "triage")
            nameless = process_email({"body": "hi"}, REF, store, FX, tmp / "out")
            self.assertTrue((tmp / "out" / f"{nameless['email_id']}.json").exists())
        finally:
            store.close()
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- Q14: names redacted in the signature only
class SignatureNames(unittest.TestCase):
    def sent(self, email):
        p = redact.prepare(email, 20000, protected_words=REF.vocabulary)
        return p["subject"] + "\n" + p["body"]

    def test_shared_mailbox_signature_names_removed(self):
        for eid, name in (("S02", "Tom"), ("S04", "Priya"), ("S13", "Lucia"), ("S12", "Priya"), ("S05", "Kate")):
            self.assertNotIn(name, self.sent(EMAILS[eid]), eid)

    def test_request_text_untouched(self):
        self.assertIn("8 x 16 oz eucalyptus radiata and 4 x 1 gal sweet almond", self.sent(EMAILS["S05"]))
        self.assertIn("Thanks for the quote. Can you also add 4 x 1 gal grapeseed?", self.sent(EMAILS["S20"]))
        self.assertIn("Northwind Candle Works", self.sent(EMAILS["S02"]))         # company is not a contact detail

    def test_sender_name_that_is_a_product_is_not_redacted_from_the_request(self):
        email = dict(EMAILS["S01"], sender="rosemary@shop.example",
                     body="Please quote 2 x 1 gal rosemary and some sage.\nThanks,\nRosemary")
        self.assertIn("2 x 1 gal rosemary", self.sent(email))

    def test_lowercase_sign_off_name(self):
        self.assertNotIn("maria", self.sent(EMAILS["S03"]).lower())
        self.assertIn("maybe 6 of each", self.sent(EMAILS["S03"]))


# ---------------------------------------------------------------- Q16: an API key without the beta still works
class BetaFallback(unittest.TestCase):
    def test_rejected_beta_is_dropped_and_the_call_retried(self):
        from types import SimpleNamespace
        from agent.extract import AnthropicExtractor, Prompts

        class APIStatusError(Exception): pass                       # stand-ins for the SDK's error classes
        class BadRequestError(APIStatusError): pass
        class Transient(Exception): pass
        anthropic = SimpleNamespace(BadRequestError=BadRequestError, APIStatusError=APIStatusError,
                                    RateLimitError=Transient, APIConnectionError=Transient,
                                    InternalServerError=Transient)
        good = ex("S01")
        calls = []

        def create(**kw):
            calls.append(kw)
            if "betas" in kw:
                raise BadRequestError("betas: unknown beta 'server-side-fallback'")
            block = SimpleNamespace(type="tool_use", name="record_extraction", input=good)
            return SimpleNamespace(stop_reason="tool_use", content=[block], model=kw["model"],
                                   usage=SimpleNamespace(input_tokens=10, output_tokens=5))

        x = object.__new__(AnthropicExtractor)
        x.anthropic, x.prompts, x.model, x.effort, x.strict = anthropic, Prompts(REF), "claude-sonnet-5-5", "medium", True
        x.use_fallback = True
        x.client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
        r = x.extract("2026-11-02", redact.prepare(EMAILS["S01"], 20000))
        self.assertEqual(r.extraction["intent"], "quote_request")
        self.assertEqual(r.attempts, 1)                     # the downgrade does not use up the retry
        self.assertFalse(x.use_fallback)
        self.assertNotIn("betas", calls[-1])


# ---------------------------------------------------------------- Q4: credit flag notes unpriced lines
class CreditExposure(unittest.TestCase):
    def test_credit_flag_says_unpriced_lines_are_excluded(self):
        e = ex("S16")                                       # Pure Leaf: 18,500 of 20,000 used
        e["lines"].append({**e["lines"][1], "line_ref": 3, "source_text": "lavender 40/42", "pack_size": None})
        rec = build("S16", e)
        flag = next(f for f in rec["flags"] if f["code"] == "CREDIT_LIMIT_EXCEEDED")
        self.assertIn("excludes 1 unpriced line", flag["detail"])


# ---------------------------------------------------------------- Q5: marketing words don't block, grades do
class MarketingWords(unittest.TestCase):
    def test_filler_ignored_grade_kept(self):
        from agent.matching import code_match
        self.assertEqual(code_match(REF, "natural peppermint").score, 1.0)
        self.assertEqual(code_match(REF, "therapeutic grade tea tree").score, 1.0)
        self.assertEqual(code_match(REF, "premium quality lavender 40/42").score, 1.0)
        self.assertLess(code_match(REF, "organic lavender 40/42").score, 0.85)
        self.assertLess(code_match(REF, "food grade castor").score, 0.85)


# ---------------------------------------------------------------- Q7: possible existing customer
class PossibleExistingCustomer(unittest.TestCase):
    def test_freemail_sender_naming_a_customer_is_flagged_not_linked(self):
        email = dict(EMAILS["S06"], body="Hi, this is Anna from Bloom & Bath Co, writing from my personal email. "
                                         "How much for 2 x 5 gallon jojoba?")
        rec = build("S06", ex("S06") | {"lines": [dict(ex("S06")["lines"][0])]}, email)
        flag = next(f for f in rec["flags"] if f["code"] == "POSSIBLE_EXISTING_CUSTOMER")
        self.assertIn("C1001", flag["detail"])
        self.assertEqual(rec["customer"]["type"], "new_lead")
        self.assertIsNone(rec["customer"]["customer_id"])

    def test_known_customer_not_flagged(self):
        self.assertNotIn("POSSIBLE_EXISTING_CUSTOMER", codes(build("S01")["flags"]))


# ---------------------------------------------------------------- Q12: forwarded / internal emails
class Forwarded(unittest.TestCase):
    def test_internal_forward_is_blocked_and_not_a_new_lead(self):
        email = dict(EMAILS["S01"], id="FWD1", sender="rep@hbno.example", subject="Fwd: Quote request - lavender",
                     body="See below.\n\n---------- Forwarded message ---------\nFrom: someone@shop.example\n"
                          "Could you quote 10 x 1 gallon Lavender 40/42?")
        e = ex("S01"); e["lines"] = []
        rec = build("S01", e, email)
        self.assertIn("FORWARDED_EMAIL", codes(rec["flags"]))
        self.assertFalse({"NEW_LEAD", "CREDIT_NOT_ESTABLISHED"} & codes(rec["flags"]))
        self.assertEqual(rec["review_state"], "needs_rep_action")

    def test_customer_forwarding_is_flagged(self):
        email = dict(EMAILS["S01"], subject="Fw: Quote request - lavender")
        self.assertIn("FORWARDED_EMAIL", codes(build("S01", None, email)["flags"]))


# ---------------------------------------------------------------- Q13: text written below quoted history
class TextBelowQuote(unittest.TestCase):
    def test_reply_below_quote_is_flagged(self):
        email = dict(EMAILS["S20"], body="Hi,\n> On Oct 28, HBNO Sales wrote:\n> here is your quote\n\n"
                                         "Please also add 4 x 1 gal grapeseed. Mike")
        rec = build("S20", None, email)
        self.assertTrue(any(f["code"] == "CLARIFICATION_NEEDED" and "below the quoted" in f["detail"]
                            for f in rec["flags"]))

    def test_normal_top_posted_reply_not_flagged(self):
        self.assertFalse(any("below the quoted" in f["detail"] for f in build("S20")["flags"]))


# ---------------------------------------------------------------- Q17: secondary intents are never lost
class SecondaryIntents(unittest.TestCase):
    def test_secondary_order_status_is_flagged(self):
        e = ex("S01"); e["secondary_intents"] = ["order_status"]
        flag = next(f for f in build("S01", e)["flags"] if f["code"] == "SECONDARY_REQUEST")
        self.assertIn("order_status", flag["detail"])

    def test_secondary_reorder_needs_history(self):
        e = ex("S01"); e["secondary_intents"] = ["reorder_by_reference"]
        self.assertIn("NEEDS_ORDER_HISTORY", codes(build("S01", e)["flags"]))


# ---------------------------------------------------------------- Q18: stock summed per SKU
class StockPerSku(unittest.TestCase):
    def test_two_lines_of_one_sku_are_checked_together(self):
        ref = copy.deepcopy(REF)
        ref.stock["EO-LEM-1GAL"] = 4                        # 3 + 2 lemon: each fits, together they don't
        rec = build_record(ref, EMAILS["S23"], redact.prepare(EMAILS["S23"], 20000), ex("S23"), dt.date(2026, 11, 4))
        for l in rec["lines"]:
            self.assertIn("STOCK_INSUFFICIENT", codes(l["flags"]), l["line_no"])


# ---------------------------------------------------------------- Q19: NetSuite payload shape
class NetSuitePayload(unittest.TestCase):
    def payload(self, eid):
        from agent.rules import netsuite_estimate
        rec = build(eid); rec["idempotency_key"] = "k"
        return netsuite_estimate(rec)

    def test_new_lead_has_no_entity_and_a_prerequisite(self):
        p = self.payload("S06")
        self.assertIsNone(p["entity"])
        self.assertEqual(p["prerequisites"][0]["action"], "find_or_create_prospect")

    def test_blocked_lines_kept_as_description_lines(self):
        items = self.payload("S30")["item"]["items"]      # 'eucalyptus' is ambiguous: not priced
        self.assertEqual(len(items), 1)
        self.assertNotIn("rate", items[0])
        self.assertIn("eucalyptus", items[0]["description"])
        self.assertIn("SKU_AMBIGUOUS", items[0]["custcol_ai_flags"])

    def test_header_placeholders(self):
        p = self.payload("S01")
        for k in ("subsidiary", "location", "currency"):
            self.assertIn(k, p)


# ---------------------------------------------------------------- Q20: cases in the schema
class CaseUnit(unittest.TestCase):
    def test_schema_accepts_case(self):
        from agent.extract import Prompts
        unit = Prompts(REF).tool["input_schema"]["properties"]["lines"]["items"]["properties"]["quantity_unit"]
        self.assertIn("case", unit["enum"])


if __name__ == "__main__":
    unittest.main()
