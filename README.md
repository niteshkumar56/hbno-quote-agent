# HBNO email-to-quote agent

Reads customer emails, extracts what they want, and writes a **draft quote** that a sales rep approves. The quote has SKUs, table prices, totals, a 5-business-day expiry and flags. Nothing is sent automatically.

The model (Claude) only reads the email. Matching, pricing, credit, stock, dates and the database write are plain Python. The model never sees prices, credit data or contact details. See [docs/GUARDRAILS.md](docs/GUARDRAILS.md).

## Submission contents

| Deliverable | Where |
|---|---|
| Source code and run instructions | This repo; this README |
| Draft quotes for the 30 samples, plus triage output for the 3 that aren't quote requests | [`out/`](out/): `<id>.json` (full record) and `<id>_cover_email.txt` (rep notes and draft email) for each email. Live run with Claude Sonnet 5.5 on 11 Nov 2026. |
| How it works and the choices made | [`docs/WRITEUP.md`](docs/WRITEUP.md) |
| Architecture diagrams | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) |
| One-page report: accuracy, cost per email, guardrails, NetSuite plan, tools and hours | [`docs/REPORT.md`](docs/REPORT.md) |
| Guardrail design in detail | [`docs/GUARDRAILS.md`](docs/GUARDRAILS.md) |

## Run it from scratch

Requires Python 3.10 or newer.

```bash
cd hbno-quote-agent
python -m venv .venv
# Windows:      .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # Windows: copy .env.example .env
# edit .env and set ANTHROPIC_API_KEY=...
```

Process the sample emails:

```bash
python run.py data/sample_emails.json
```

Process **any new file** in the same format (a JSON array of `{id, sender, to, subject, date, body}`):

```bash
python run.py path/to/new_emails.json --out out_new
```

Drafts are dated the day they are created and are valid for 5 business days from then. To reproduce a run exactly, fix the date with `--as-of 2026-11-10`.

Score against the hand-labelled answers (sample emails only):

```bash
python eval/score.py --out out
```

### No API key? Test the pipeline offline

```bash
python -m unittest discover -s tests -t .                                   # 55 guardrail tests
python run.py data/sample_emails.json --extractor fixture --out out_fixture --db out_fixture/test.db
python eval/score.py --out out_fixture
```

`--extractor fixture` replaces the model with **hand-written** extractions (`tests/fixture_extractions.py`). It tests everything after extraction: matching, pricing, flags, the store and the cover emails. It is not model output, and it can't process new emails.

## What you get

| Output | Contents |
|---|---|
| `quotes.db` (SQLite) | `quotes`, `quote_lines`, `processed_emails` (unique idempotency key), `counters` |
| `out/<id>.json` | Full record: quote number, customer or new lead, lines (SKU, qty, unit price, amount, match confidence, flags), totals, expiry, status `Draft - pending rep approval`, header flags, a NetSuite Estimate-shaped payload, and an audit block (model, prompt version, tokens, cost, the exact redacted text sent to the model, raw extraction) |
| `out/<id>_cover_email.txt` | Internal rep notes, plus a draft customer email built from a code template (prices come from the table, never from the model) |
| `out/run_summary.json` | Counts, tokens, cost, cost per email |
| `out/eval_report.json` | Written by `eval/score.py` |

Emails that aren't quote requests (order status, samples, "same as last time") get a **triage** record with a routing reason instead of a quote. So does any malformed input record (`INVALID_INPUT`), so every email in the file produces an output.

## Safety properties (tested in `tests/test_guardrails.py`)

- **Running twice creates no duplicates.** The key is a hash of sender, date, subject and normalized body. It's checked before the model call, so a rerun costs nothing, and backed by a `UNIQUE` constraint.
- **A crash leaves no half-written quote.** Header, lines, counter and record are written in one SQLite transaction. The test simulates a crash between header and lines.
- **Prices only come from `price_levels.csv`.** Discount demands and injected instructions raise flags; they don't change prices.
- **Lines must quote the customer's new message.** A line lifted from quoted history, or invented by the model, is discarded.
- **Code makes the final SKU match.** Confidence is the lower of the model's and the code's scores, with a 0.85 threshold. Below it, the line goes to a rep and is never guessed.
- **The model's own fields are checked:** the matched product must be named in the customer's verbatim words, the quantity must appear in the message, and weights, liters or cases are never converted to containers.
- **Contact details are redacted before the model call:** phones, emails, street addresses, ZIP codes and the name in the signature.

## Configuration

| File | Purpose |
|---|---|
| `.env` | `ANTHROPIC_API_KEY`; optional `HBNO_MODEL` (default `claude-sonnet-5-5`) and `HBNO_EFFORT` (default `medium`) |
| `config/flags.json` | Every flag with its severity (blocker / warning / info) |
| `config/holidays.json` | Company holidays skipped in expiry |
| `config/synonyms.json` | Extra product names accepted by the matcher (e.g. Spanish) |
| `agent/config.py` | Match threshold, free-mail domains, routing, model pricing |
| `prompts/` | System prompt, user template, tool schema (versioned by hash in every record) |

## Layout

```
run.py                 CLI entry point
agent/redact.py        input guard: quoted-history split, contact-detail redaction, tag escaping
agent/extract.py       model call (strict tool schema, retries, refusal handling, cost)
agent/matching.py      deterministic SKU matching + confidence
agent/pricing.py       tier price, floor, tier-break opportunity
agent/rules.py         flags, credit, stock, dates, record assembly, NetSuite payload
agent/store.py         SQLite, idempotency, atomic write, quote numbers (DQ-YYYY-NNNNN)
agent/cover_email.py   rep notes + customer draft (template)
eval/expected.json     hand-labelled answers for the 30 samples
eval/score.py          accuracy scorer
tests/                 guardrail tests + fixture extractions
docs/GUARDRAILS.md     design of the safety layers and the NetSuite mapping
```
