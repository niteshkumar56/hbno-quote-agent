# Architecture

## End to end

Everything runs locally except one call to the Anthropic API. That call receives redacted email text and catalog names, nothing else.

```mermaid
flowchart TD
    IN[/"emails.json<br/>id, sender, to, subject, date, body"/] --> V{"Valid record?"}
    V -- no --> TI["Triage record<br/>INVALID_INPUT"]
    V -- yes --> D{"Already processed?<br/>idempotency key in quotes.db"}
    D -- yes --> OLD["Return the stored record<br/>no model call, no cost"]
    D -- no --> R["Input guard<br/>split quoted history, redact phones, emails,<br/>addresses, ZIPs and signature name, escape tags"]

    subgraph API["Anthropic API: the only external call"]
        X["Claude Sonnet 5.5<br/>strict tool record_extraction<br/>schema checked, 1 retry"]
    end

    R -->|"redacted subject and body, email date"| X
    CAT[("catalog.csv<br/>names, pack sizes, aliases only")] -->|"system prompt, cached"| X
    X -- "fails after retry" --> EF["Triage record<br/>EXTRACTION_FAILED"]
    X -->|"extraction JSON: intent, lines, ship-to,<br/>needed-by, customer prices, attention codes"| G["Output guard<br/>each line must quote the new message;<br/>product and quantity must be in the customer's words"]
    G --> I{"Quote request?"}
    I -- no --> TR["Triage record<br/>routing reason"]
    I -- yes --> BR["Business rules, code only<br/>match SKU, tier price, floor, credit,<br/>stock, expiry, flags"]

    PR[("price_levels.csv")] --> BR
    CU[("customers.csv")] --> BR
    ST[("stock.csv")] --> BR
    CF[("config: flags, holidays, synonyms")] --> BR

    BR --> W["Atomic write<br/>one SQLite transaction:<br/>header, lines, DQ number, record"]
    TR --> W
    W --> DB[("quotes.db")]
    W --> OUT[/"out/ID.json and out/ID_cover_email.txt<br/>NetSuite Estimate payload inside"/]
    TI --> OUT
    EF --> OUT
    OLD --> OUT
    OUT --> REP(["Sales rep reviews, edits and sends<br/>status: Draft - pending rep approval"])
```

What the model never receives: the sender address, the customer record, prices, floors, tiers, stock, credit limits, balances or terms. The extraction schema has no price field, so model output cannot reach a price.

## When a line gets a price

A line is priced only if it passes every check below. A line can collect several blockers; any blocker leaves it unpriced, with candidate SKUs listed for the rep. Warnings and info flags never change the price.

```mermaid
flowchart TD
    L["Line extracted by the model"] --> A{"source_text found in the<br/>customer's new message or subject?"}
    A -- no --> DROP["Line dropped<br/>CLARIFICATION_NEEDED"]
    A -- yes --> B{"Exactly one catalog product,<br/>named in the customer's words,<br/>confidence at least 0.85?"}
    B -- no --> B1["Unpriced, blocker<br/>SKU_NO_MATCH, SKU_AMBIGUOUS<br/>or LOW_MATCH_CONFIDENCE"]
    B -- yes --> C{"Pack size given, sold for this<br/>product, and counted in containers?"}
    C -- no --> C1["Unpriced, blocker<br/>PACK_SIZE_MISSING, PACK_SIZE_UNAVAILABLE<br/>or UNIT_NOT_SELLABLE"]
    C -- yes --> Q{"Quantity a whole number,<br/>1 to 10,000, found in the message?"}
    Q -- no --> Q1["Unpriced, blocker<br/>QTY_MISSING or QTY_NOT_IN_SOURCE"]
    Q -- yes --> S{"SKU active?"}
    S -- no --> S1["Unpriced, blocker<br/>SKU_INACTIVE"]
    S -- yes --> P["Priced from price_levels.csv<br/>tier chosen by quantity"]
    P --> F["Flags added, price unchanged<br/>STOCK_INSUFFICIENT, CUSTOMER_PRICE_BELOW_FLOOR,<br/>TIER_BREAK_OPPORTUNITY, LINE_VALIDITY_SHORTER"]
```

## Where each part lives

| Step | Code |
|---|---|
| Command line, run summary | `run.py` |
| Validation, duplicate check, orchestration, triage records | `agent/pipeline.py` |
| Input guard (quoted history, redaction, tag escaping) | `agent/redact.py` |
| Model call, retries, schema validation, cost | `agent/extract.py`, `prompts/` |
| Product matching and confidence | `agent/matching.py` |
| Tier pricing, floor, customer price requests | `agent/pricing.py` |
| Output guard, flags, credit, stock, dates, NetSuite payload | `agent/rules.py` |
| SQLite store, idempotency, quote numbers | `agent/store.py` |
| Rep notes and draft customer email (template) | `agent/cover_email.py` |
| Flag codes and severities | `config/flags.json` |
