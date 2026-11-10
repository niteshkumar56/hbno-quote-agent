# Guardrails

The model has one job: turning free-text email into structured fields. Everything that touches money, credit, stock or the quote record is plain code that can be tested. The model never sees prices, credit, stock or contact details, so it can't leak or change them. Each layer below assumes the layer before it can fail.

```
email JSON
  │
  ├─ 1. Input guard ─────── idempotency check, size limits, quote-history split, contact-detail redaction
  ├─ 2. Model call ──────── extraction only, strict tool schema, prompt hash logged
  ├─ 3. Output guard ────── schema validation, verbatim check on source_text, catalog-name whitelist
  ├─ 4. Business rules ─── SKU match + confidence, tier price, floor, credit, stock, expiry  (code only)
  ├─ 5. Atomic write ───── one SQLite transaction, unique idempotency key
  └─ 6. Human gate ─────── status "Draft - pending rep approval", nothing sent, cover email is a template
```

## 1. Input guard (before the model)

| Guard | Why |
|---|---|
| **Idempotency key** = SHA-256 of `sender` (lower-cased), `date`, `subject` and the whitespace-normalized `body`. Checked before any model call. | A repeat run returns the existing quote and costs nothing. The key doesn't use `id`, so the same email resent under a new id is still caught. In production it would use the email's Message-ID. |
| **Customer lookup by domain in code**, from the sender address. The sender address is never sent to the model. | Identity comes from data the customer can't edit, not from a name in the body ("Kate" appears at two companies). |
| **Contact-detail redaction**: phone numbers, email addresses and US street addresses are replaced with `[PHONE]`, `[EMAIL]` and `[ADDRESS]`. City and state are kept because the ship-to needs them. | The brief says contact details must not reach the AI service. Signatures (S01, S19) contain all three. |
| **Signature-name redaction**: the person's name in the signature (after "Thanks,", in the last sentence, or on the first line of a trailing signature block) becomes `[NAME]`, whatever the mailbox (`orders@` signed "Tom" included). Names are never redacted inside the request, and catalog words are never treated as names. | A name is a contact detail. Redacting the sender's name everywhere broke requests when the name was also a product (`rosemary@` asking for rosemary). Known over-redaction: a company name standing alone in the last sentence ("We prepay. - Kalamata Naturals") is also replaced; the model doesn't need it. |
| **Quoted-history split**: text after `>` lines or "On … wrote:" is kept in the record but marked as quoted history. The model is told never to extract lines from it. If the customer wrote unquoted text *below* `>`-quoted history, that text isn't processed and `CLARIFICATION_NEEDED` tells the rep to read the original. | S20: the quoted castor oil must not turn into a quote line. |
| **Invalid input** (missing field, sender without `@`, unreadable date) still produces an output file: a triage record with `INVALID_INPUT` (blocker). | Every input email gets a record; nothing disappears silently. |
| **Size limit**: bodies over 20,000 characters are truncated and flagged. | Controls cost and blocks oversized inputs. |
| **Delimiting**: the email goes inside `<email_body id=NONCE>`, and any `<email_body` or `</email_body` text inside it is escaped. | The email can't close the data block and inject text outside it. |

**What the model receives:** email date, redacted subject, redacted body, and the catalog's product names, pack sizes and aliases.
**What it never receives:** sender, customer record, prices, floors, tiers, stock, credit limits, balances, terms.

## 2. Model call

- **Extraction only.** The prompt (`prompts/extract_system.md`) forbids pricing, unit conversion, guessing pack sizes, and mapping a "similar" product to the nearest catalog item.
- **One strict tool call.** `record_extraction` uses `strict: true` with the JSON schema in `prompts/extraction_schema.json`. Every field is required and `additionalProperties` is false. Current models reject forced `tool_choice`, so the prompt requires the call and code rejects any response without exactly one call. The full schema, including length and range limits, is validated locally with `jsonschema`.
- **Model and settings:** `claude-sonnet-5-5` at effort `medium`, configurable with `HBNO_MODEL` and `HBNO_EFFORT`. Server-side refusal fallback is on when the API key allows it: if the API rejects the request with the fallback beta on, it is retried once without it (and without strict mode if the schema is rejected), and these downgrades don't use up the retry. The system prompt and catalog are prompt-cached, so the per-email cost is mostly the email itself.
- **Reproducibility:** current models don't accept sampling parameters such as temperature. Reproducibility comes from the strict schema, the checks in code, and storing the prompt version hash, model id and raw extraction on every record.
- **Injection handling.** The email is defined as data. Any instruction in it raises `EMBEDDED_INSTRUCTIONS` and changes nothing. Even if the model followed an injected instruction, layers 3 to 6 still hold: the model has no price fields to set and no way to approve anything.
- **Retry policy.** One retry on a transient error, a refusal, a cut-off response or a schema failure. After that the email gets an `EXTRACTION_FAILED` triage record and goes to a rep. The agent never falls back to guessing.

## 3. Output guard (after the model, before any business logic)

| Check | On failure |
|---|---|
| JSON schema validation | `EXTRACTION_FAILED` |
| `source_text` must appear in the redacted body or subject, compared case- and whitespace-insensitively | The line is dropped and `CLARIFICATION_NEEDED` raised. This catches invented lines. |
| `catalog_product` must be an exact catalog product name or null | Treated as null, which leads to `SKU_NO_MATCH` |
| The matched product must also be named in the verbatim `source_text`, not just in the model-written `product_text` | `LOW_MATCH_CONFIDENCE`, not priced. Stops the model renaming "lavandin grosso" to "lavender 40/42". Cost: a translation not in `config/synonyms.json` becomes a false blocker. |
| `quantity` must appear, as digits or a number word ("a dozen", "two", "dos"), in `source_text` or elsewhere in the new message | `QTY_NOT_IN_SOURCE`, not priced. Catches invented quantities; can't catch a swap between two numbers that are both in the text. |
| `quantity` must be greater than 0 and at most 10,000; `pack_size` must be in the allowed set | Line flagged `QTY_MISSING` or `PACK_SIZE_UNAVAILABLE` |
| `quantity_unit` must be `container` (or `gal` with a 1 gal pack) whenever a pack size is set | `UNIT_NOT_SELLABLE`. Weight, liters and cases are never converted, even if the model fills in a pack size. |
| `needed_by.date` must be a valid date | Date dropped; text kept |
| Any field that looks like a price outside `customer_price_requests` | Can't happen, because the schema has no such field |

## 4. Business rules (deterministic code, never the model)

**SKU matching and confidence (computed in code, final):**
1. Find the candidate products. Take the model's `catalog_product`, then check it independently: normalize the customer's `product_text` and test it against the catalog name and aliases (exact match, alias contained in the text, then fuzzy match).
2. Confidence = min(model confidence, code match score). The code score is a rule tier, not a calibrated probability: 1.0 when a name/alias appears and every other word is explained, 0.8 when extra qualifying words remain ("clove **leaf**", "**organic** lavender 40/42"), 0.7 for a partial name, typo ratio × 0.95 for close spellings, and 0 if the code finds no support for the model's pick. Marketing filler ("natural", "premium", "therapeutic grade") is ignored; grade, origin and plant-part words are not. So the 0.85 threshold means "exact alias with nothing unexplained, or a very close typo".
3. Resolve the SKU only when there is exactly one product, a pack size, and that product is sold in that pack size.
4. **Threshold 0.85.** Below it, the line gets `LOW_MATCH_CONFIDENCE` with candidates listed, and no SKU or price. Lines never fall back to the nearest match.
5. An inactive SKU gets `SKU_INACTIVE` and isn't priced.

**Pricing (`price_levels.csv` only):**
- Unit price = `unit_price_usd` of the highest `min_qty` that is at or below the quantity. Line amount = quantity × unit price, using `Decimal` and rounded half-up to cents. Order total = sum of the priced lines.
- Integrity guard: if the computed price is below `price_floor_usd`, raise `PRICE_BELOW_FLOOR` (blocker). This would mean the table is corrupt.
- A customer target price below the floor raises `CUSTOMER_PRICE_BELOW_FLOOR`. A target at or above the floor but below the table price raises `CUSTOMER_PRICE_REQUEST`. A percentage or amount off is first turned into the per-container price it implies (S26: 50% off $32.30 = $16.15, below the $27.20 floor); an order-level percentage is checked on every priced line. Requests attach to lines by the model's `line_ref`, so a request for a discarded line attaches to nothing. **The table price is never changed.**
- `TIER_BREAK_OPPORTUNITY` (info) when buying the next tier's minimum quantity would cost the same or less in total (S19: 25 × $25.50 = $637.50 < 24 × $27.00 = $648.00). The quantity is never changed.
- `TIER_CONSOLIDATION_POSSIBLE` (info) when the same SKU is on several lines and the combined quantity reaches a cheaper tier. Lines are still priced separately.

**Customer and credit:**
- Unknown domain gives a new lead, with `NEW_LEAD` and `CREDIT_NOT_ESTABLISHED`. Free-mail domains (a configurable list) add `FREEMAIL_DOMAIN`. If no ship-to was given, add `SHIP_TO_MISSING`. If the email names a known customer's company ("Anna from Bloom & Bath Co" on a Gmail address), `POSSIBLE_EXISTING_CUSTOMER` (warning) names the account. It is never linked automatically, because anyone can type a company name.
- A forwarded email (`Fwd:`/`Fw:` subject or a forwarded-message block) raises `FORWARDED_EMAIL` (blocker). If the sender is internal (same domain as the `to` address, or `INTERNAL_DOMAINS`), the customer is recorded as unknown, not as a new lead, and the original sender is not parsed.
- `credit_hold = Y` gives `CREDIT_HOLD` (blocker).
- When terms are not Prepaid and open balance + quote total > credit limit, raise `CREDIT_LIMIT_EXCEEDED` (warning) and show the amount over. If some lines are unpriced, the flag says the total excludes them. Over-limit is a warning, not a blocker: a rep can take a deposit or partial shipment, whereas a hold is an AR stop.
- `terms = Prepaid` gives `PREPAID_TERMS` (info) and no limit check. A $0 limit on a prepaid account is not a breach.
- Each quote is checked on its own. Other open drafts for the same customer are listed as an information note, not added to the credit check.

**Stock:** when quantity > `available_qty`, raise `STOCK_INSUFFICIENT` (warning) and show the available quantity. Lines of the same SKU on one quote are checked together. The line stays priced at the requested quantity; the rep decides on a partial shipment or lead time. Drafts don't reserve stock.

**Dates:**
- Quote date = the day the draft is created (`--as-of` to fix it for a reproducible run), never earlier than the email. The email's date is kept as `received_date`. Expiry = quote date + 5 business days (the brief's rule), skipping weekends and the company holidays in `config/holidays.json`. Veterans Day is not a company holiday by default. Counting from the email date would make drafts reviewed a week later expire on arrival.
- If `price_levels.csv` gives a line a shorter validity than 5 days, `LINE_VALIDITY_SHORTER` (warning) tells the rep; the quote's expiry is not changed automatically.
- A needed-by date on a weekend or holiday raises `NEEDED_BY_NON_BUSINESS_DAY`. One within 5 business days of the email (short notice), or already passed when the draft is made, raises `NEEDED_BY_AT_RISK`.

**Intent routing:**
- `quote_request` and `quote_change` always produce a draft quote, even when every line is blocked, so the rep has one item to review.
- `sample_request`, `reorder_by_reference`, `order_status`, `order_placement`, `product_question`, `complaint_or_return` and `other` produce a **triage record** (no quote number) with a routing reason.
- A secondary intent is never dropped: it raises `SECONDARY_REQUEST` (warning) with its routing, or `NEEDS_ORDER_HISTORY` when the email also refers to a previous order.

## 5. Atomic, idempotent write

- Quote header, lines, flags, the extraction JSON, model id, prompt version and token usage are written in **one SQLite transaction**. Either all of it lands or none of it does.
- `quotes.idempotency_key` is `UNIQUE`. A second run hits the constraint and returns the existing quote number, with no duplicate. A crash before commit leaves nothing behind.
- Quote numbers come from a counter table inside the same transaction, formatted as `DQ-2026-00001`. The `DQ` prefix can't collide with customer-facing numbers like `Q-2210`.
- The JSON files in `out/` are generated from the database after commit, so they can be rebuilt at any time.

## 6. Human gate

- Every quote has status **`Draft - pending rep approval`**. The agent has no send, approve or post capability.
- Any blocker flag sets `review_state = needs_rep_action`. Otherwise it's `ready_for_review`.
- **The cover email is a code template**, not model output. It fills in customer name, lines, prices, totals and expiry from the database. The model never sees those values, so it can't write them. Blocked lines become questions to the customer ("Could you confirm which pack size you need for lavender 40/42: 4 oz, 16 oz, 1 gal or 5 gal?").

## Audit trail (per quote)

Raw email (unredacted, local only), redacted text sent to the model, model id, prompt version hash, extraction JSON, token usage and cost, every flag with its source (code or model), and timestamps.

## Mapping to NetSuite (future)

| Draft quote field | NetSuite Estimate |
|---|---|
| `customer_id` | `entity` |
| new lead | `entity` is null in the payload and `prerequisites` says to find or create a prospect by email first, then set `entity` to its internal id. An Estimate can't be posted against a bare email address. |
| — | `subsidiary`, `location`: account-specific internal ids, placeholders in `config.NETSUITE`. `currency` = USD. |
| `quote_date` | `trandate` |
| `expiry_date` | `duedate` (Expires) |
| status | `entitystatus` = a "Draft – Pending Rep" status, plus an approval workflow |
| unpriced (blocked) lines | A Description item line ("UNPRICED - needs rep: …") with no rate and its flags, so the Estimate never silently drops a requested product |
| `ship_to` | `shipaddress` / `shippingaddress` subrecord. Per-line ship-to needs Multiple Shipping Routes, or split Estimates. |
| `needed_by` | `custbody_needed_by` |
| lines: `sku`, `qty`, `unit_price`, `amount` | `item` (internal id resolved by SKU), `quantity`, `rate`, `amount`, `price` = Custom (-1) so the agreed rate is fixed |
| flags | `custbody_ai_flags` (JSON), plus `custcol_ai_line_flags` |
| idempotency key | `externalid` (NetSuite rejects duplicates) |
| source email | `custbody_source_message_id`, plus the email attached as a Message record |

In production the agent would read prices, stock and credit live from NetSuite (price levels and quantity pricing, `quantityavailable`, `creditlimit`/`balance`/`creditholdoverride`) instead of from CSVs. It would post Estimates through REST or SuiteTalk, authenticating with token-based auth and a dedicated role that has create permission on Estimates but no approval rights.
