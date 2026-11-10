# HBNO email-to-quote agent: report

## Accuracy on the 30 samples

Live run on 11 Nov 2026 with Claude Sonnet 5.5 (`out/`, scored by `eval/score.py` against `eval/expected.json`):

| Measure | Result |
|---|---|
| Emails fully correct | 29 / 30 |
| Intent, quote vs triage, customer match | 30 / 30 each |
| Priced lines exact (SKU, quantity, unit price, amount) | 34 / 34 |
| Order totals exact to the cent | 27 / 27 |
| Blocker and warning flags | 100% recall, 97% precision |
| Critical errors (guessed SKU, wrong price, wrong customer, forbidden flag) | 0 |

The one miss is S30. The model also flagged "We prepay as usual" as a terms remark for the rep to check. That is an extra warning, not a wrong quote. I left the expected answer as it was.

Two caveats. I labelled `expected.json` myself, so this measures agreement with my reading of the emails. And 30 emails is a small sample. The prompt's examples are invented, not copied from the samples, so the figure isn't inflated by the prompt having seen the answers.

19 drafts came out `ready_for_review` and 11 `needs_rep_action` (a blocker such as an ambiguous product, a missing pack size, a credit hold or a request that isn't a quote).

## Cost per email

$0.0094 per email, $0.28 for all 30, at the list prices set in `agent/config.py`. No email needed a retry. Output tokens are two-thirds of the cost (18,651 tokens over the run). The system prompt and catalog are cached, so the 265,205 cached input tokens cost $0.05 in total. Opus 5.5 is a one-line switch (`HBNO_MODEL`) at roughly twice the cost.

## Guardrails

The model only extracts. Matching, pricing, credit, stock, dates and the database write are plain Python with 56 tests (`docs/GUARDRAILS.md` has the full design).

- **Prices never come from the model.** The extraction schema has no price field. Prices come only from `price_levels.csv`. A customer's target price or discount ("50% off, management approved") is compared with the floor and flagged, never applied.
- **Nothing the model writes is trusted on its own.** Each line must quote the customer's new message word for word. The product must be named in those words, not only in the model's own field, and the quantity must appear in the message. Weights, liters and cases are never converted to containers.
- **Below 0.85 confidence, nothing is guessed.** Ambiguous ("eucalyptus"), unknown ("lavandin"), inactive or unsized products produce a blocker and an unpriced line with candidates listed.
- **Minimal data to the model.** Phones, emails, street addresses, ZIP codes and the signature name are redacted. The model never sees the sender address, customer record, credit, stock or prices.
- **Email is data.** Injected instructions raise `EMBEDDED_INSTRUCTIONS` and change nothing.
- **Safe writes.** The duplicate check runs before the model call, and a quote's header, lines and number are written in one SQLite transaction. A rerun creates no duplicate; a crash leaves nothing half-written.
- **Nothing is sent.** Every quote is `Draft - pending rep approval`. The cover email is a code template, so it can only contain table prices.

## What I'd change to create Estimates in NetSuite

1. Read prices, stock and credit live instead of from CSVs: quantity pricing on the item's price levels, `quantityavailable` per location, and the customer's credit limit, balance and hold status.
2. Post Estimates through the REST record API, using the idempotency key (from the email's Message-ID in production) as `externalId` so NetSuite itself rejects duplicates.
3. Find or create a prospect for new leads before posting, since an Estimate needs an entity. The payload already carries this as a prerequisite.
4. Add a custom "Draft - pending rep approval" Estimate status with an approval workflow. Run the integration under a role that can create Estimates but not approve or email them.
5. Keep unpriced lines as Description-item lines carrying their flags, and set subsidiary and location per account. Store flags in custom body and line fields.
6. Replace the JSON input with a mailbox trigger, and attach the original email to the Estimate.

## Tools, models and time

- Built with Claude Code in VS Code (Claude Opus 5.5) for code, review and documentation.
- The agent calls Claude Sonnet 5.5 through the Anthropic API (`anthropic` 2.7.0). Python 3.12, `jsonschema`, `python-dotenv`, SQLite.
- About 6 hours.
