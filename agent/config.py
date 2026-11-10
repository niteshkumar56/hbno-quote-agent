"""Static settings. Everything business-tunable lives here or in config/*.json."""
from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CONFIG_DIR = ROOT / "config"
PROMPTS_DIR = ROOT / "prompts"

# --- Model ---------------------------------------------------------------
# Override with HBNO_MODEL / HBNO_EFFORT in .env. The model only extracts; it never prices.
MODEL = os.getenv("HBNO_MODEL", "claude-sonnet-5-5")
EFFORT = os.getenv("HBNO_EFFORT", "medium")          # low | medium | high
MAX_TOKENS = 16000
MAX_RETRIES = 1                                      # one retry on schema/validation failure
# Models that accept server-side refusal fallbacks (`fallbacks: "default"`).
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# USD per million tokens: (input, output, cache_read, cache_write_5m). Used for the cost report only.
PRICING = {
    "claude-opus-5-5":   (4.00, 20.00, 0.20, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
    "claude-haiku-5-5":  (0.10, 0.50, 0.01, 0.125),
    "claude-fable-5-1":  (10.00, 50.00, 0.25, 12.50),
}

# --- Business rules ------------------------------------------------------
MATCH_THRESHOLD = 0.85            # below this a line is never auto-quoted
QUOTE_STATUS = "Draft - pending rep approval"
QUOTE_VALID_BUSINESS_DAYS = 5     # the brief's rule; counted from the day the draft is created
QUOTE_PREFIX = "DQ"               # never collides with customer-facing Q-xxxx numbers
MAX_BODY_CHARS = 20000
MAX_QTY = 10000
CENT = Decimal("0.01")

FREEMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com", "live.com",
    "aol.com", "icloud.com", "me.com", "proton.me", "protonmail.com", "gmx.com", "mail.com",
    "zoho.com", "yandex.com",
    "freemail.example",  # the pack's synthetic stand-in for a free-mail provider
}

# NetSuite Estimate placeholders: account-specific internal ids, to be set per HBNO account.
NETSUITE = {
    "subsidiary": {"id": None},                 # OneWorld subsidiary internal id
    "location": {"id": None},                   # shipping warehouse location internal id
    "entity_status": {"id": None},              # a custom "Draft - pending rep approval" estimate status
    "description_item": "AI-UNRESOLVED-LINE",   # a Description item used for unpriced lines
}

# Sender domains that belong to HBNO itself (a rep forwarding a customer's email). The domain of
# the email's `to` address is always treated as internal too.
INTERNAL_DOMAINS: set[str] = set()

ROUTING = {
    "reorder_by_reference": "Sales rep: pull customer's order history",
    "sample_request": "Sales rep: sample program",
    "order_placement": "Order entry: firm order / PO, not a quote",
    "order_status": "Customer service: order status",
    "product_question": "Sales rep: product information",
    "complaint_or_return": "Customer service: complaint / return",
    "other": "Inbox review: not a sales request",
}
