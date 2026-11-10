"""Input guard: split quoted history, redact contact details, neutralise delimiter tags.

Everything here runs BEFORE the model sees the email. The brief forbids sending
customer contact details to the AI service, and signatures carry phones, emails
and street addresses.
"""
from __future__ import annotations

import re

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_URL = re.compile(r"\bhttps?://\S+|\bwww\.\S+", re.I)
_PHONE = re.compile(r"(?<![\w-])(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?:\s*(?:x|ext\.?)\s*\d{1,5})?(?![\w-])")
_STREET = re.compile(
    r"\b\d{1,6}\s+(?:[A-Z][A-Za-z.]*\s+){1,4}"
    r"(?:St|Street|Ave|Avenue|Blvd|Boulevard|Rd|Road|Dr|Drive|Ln|Lane|Way|Ct|Court|Pkwy|Parkway|Hwy|Highway|Pl|Place|Ter|Terrace|Cir|Circle|Sq|Square)\b\.?"
    r"(?:,?\s*(?:Suite|Ste\.?|Unit|Apt\.?|#)\s*[\w-]+)?")
_PO_BOX = re.compile(r"\bP\.?\s?O\.?\s+Box\s+\d+\b", re.I)
_ZIP_AFTER_STATE = re.compile(r"\b([A-Z]{2}),?\s+\d{5}(?:-\d{4})?\b")

# Lines that start quoted history in replies/forwards.
_QUOTE_START = re.compile(
    r"^\s*(?:>|On .{3,120}wrote:\s*$|-{2,}\s*Original Message\s*-{2,}|-{2,}\s*Forwarded message|From:\s.+|Sent from my )",
    re.I)

_GENERIC_LOCALPARTS = {
    "orders", "order", "buyer", "buyers", "purchasing", "purchase", "ops", "operations", "compras",
    "info", "sales", "admin", "accounts", "ap", "office", "procurement", "hello", "contact", "team",
}


def split_quoted(body: str) -> tuple[str, str]:
    """Return (new_message, quoted_history). Quoted text is never sent for extraction of lines."""
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if i > 0 and _QUOTE_START.match(line):
            return "\n".join(lines[:i]).rstrip(), "\n".join(lines[i:])
    return body, ""


def text_below_quote(quoted: str) -> str:
    """Unquoted text written after a '>'-quoted block (a reply written below the history). It is
    not processed; the rep is told so. Outlook-style history without '>' can't be judged and is
    ignored here."""
    lines = quoted.splitlines()
    marked = [i for i, l in enumerate(lines) if l.lstrip().startswith(">")]
    if not marked:
        return ""
    return "\n".join(l for l in lines[marked[-1] + 1:] if l.strip()).strip()


def sender_name_tokens(sender: str) -> list[str]:
    """Personal-name tokens from the sender local part (anna@, j.morales@), excluding role mailboxes."""
    local = sender.split("@", 1)[0].lower()
    if local in _GENERIC_LOCALPARTS:
        return []
    return [t for t in re.split(r"[._+-]", local) if len(t) >= 3 and t not in _GENERIC_LOCALPARTS]


# ----------------------------------------------------------------------------- signature names
_SIGNOFF = re.compile(
    r"\b(?:thanks|thank you|thx|many thanks|(?:best |kind |warm )?regards|best|cheers|sincerely|"
    r"gracias|saludos|atentamente|merci|cordialement)\b", re.I)
_SEPARATOR = re.compile(r"[,|\n(]|\s[-–]\s|\s[-–]$|^[-–]\s")
_NOT_NAMES = {
    "thanks", "thank", "you", "regards", "best", "cheers", "sincerely", "gracias", "saludos", "hi", "hello",
    "team", "sales", "orders", "purchasing", "procurement", "buyer", "ops", "operations", "asap", "urgent",
    "please", "pls", "ok", "okay", "yes", "no", "again", "dear", "all", "everyone", "hbno",
    # words that end a request, not a signature ('Need By Friday', 'Ship To Austin')
    "need", "needed", "by", "ship", "to", "for", "the", "and", "of", "quote", "request", "order", "rfq", "po",
    "re", "fwd", "fw", "today", "tomorrow", "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday", "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "jan", "feb", "mar", "apr", "jun", "jul", "aug",
    "sep", "sept", "oct", "nov", "dec", "q1", "q2", "q3", "q4", "eod", "eow", "hola", "bonjour",
}


def _chunk_at(text: str, start: int) -> tuple[int, int]:
    """The first name-like chunk starting at `start`: up to the first separator."""
    while start < len(text) and text[start] in " \t\n-–,:":
        start += 1
    m = _SEPARATOR.search(text, start)
    end = m.start() if m else len(text)
    return start, end


def _is_name(chunk: str, sender_tokens: list[str], protected: frozenset[str]) -> bool:
    words = chunk.split()
    if not 1 <= len(words) <= 3:
        return False
    for w in words:
        lw = w.lower().strip(".")
        if not re.fullmatch(r"[A-Za-zÀ-ÿ'.-]+", w) or lw in _NOT_NAMES or lw in protected:
            return False
        if not (w[0].isupper() and not (len(w) > 1 and w.isupper())) and lw not in sender_tokens:
            return False                      # lowercase words count only if they are the sender's name
    return True


def find_signature_name(text: str, sender: str, protected: frozenset[str] = frozenset()) -> tuple[int, int] | None:
    """Locate the sender's name in the signature at the end of the new message. Returns (start, end)
    or None. Looks, in order, after the last sign-off ('Thanks,' 'Regards, Tom'), in the last
    sentence ('...jojoba? Jorge'), and on the first line of a trailing paragraph ('Kevin Shaw\\nDirector')."""
    toks = sender_name_tokens(sender)
    body = text.rstrip()
    tail_start = sum(len(l) + 1 for l in body.split("\n")[:-6]) if body.count("\n") >= 6 else 0
    candidates: list[int] = []
    for m in _SIGNOFF.finditer(body, tail_start):
        before = body[:m.start()].rstrip(" \t")
        if before and before[-1] not in ".!?\n":
            continue                          # mid-sentence: 'your best price'
        rest = body[m.end():]
        if rest[:1] not in (",", "!", ".", "\n", "") and len(rest.split("\n")[0].split()) > 3:
            continue                          # 'Thanks for the quote. Can you also add...'
        candidates.append(m.end())
    if candidates:
        candidates = [candidates[-1]]
    last_stop = max(body.rfind(". "), body.rfind("? "), body.rfind("! "), body.rfind("\n"))
    candidates.append(last_stop + 1)
    para = body.rfind("\n\n", tail_start)
    if para != -1:
        candidates.append(para + 2)
    for c in candidates:
        s, e = _chunk_at(body, c)
        if s < e and _is_name(body[s:e].strip(), toks, protected):
            return s, s + len(body[s:e].rstrip())
    return None


def redact_signature_name(text: str, sender: str, protected: frozenset[str] = frozenset()) -> tuple[str, int]:
    """Replace the signature name, and the sender's own name tokens after it, with [NAME]."""
    span = find_signature_name(text, sender, protected)
    if not span:
        return text, 0
    s, e = span
    tail, n = text[e:], 1
    for tok in sender_name_tokens(sender):
        if tok not in protected:
            tail, k = re.subn(rf"\b{re.escape(tok)}\b", "[NAME]", tail, flags=re.I)
            n += k
    return text[:s] + "[NAME]" + tail, n


def redact(text: str, sender: str = "", protected: frozenset[str] = frozenset(),
           signature: bool = True) -> tuple[str, dict]:
    """Replace contact details with tokens. Returns (redacted_text, counts). Personal names are
    redacted only in the signature (signature=False for subjects), never inside the request."""
    counts: dict[str, int] = {}

    def sub(pattern: re.Pattern, repl, s: str, key: str) -> str:
        new, n = pattern.subn(repl, s)
        if n:
            counts[key] = counts.get(key, 0) + n
        return new

    text = sub(_EMAIL, "[EMAIL]", text, "email")
    text = sub(_URL, "[URL]", text, "url")
    text = sub(_PO_BOX, "[ADDRESS]", text, "address")
    text = sub(_STREET, "[ADDRESS]", text, "address")
    text = sub(_PHONE, "[PHONE]", text, "phone")
    text = sub(_ZIP_AFTER_STATE, r"\1 [ZIP]", text, "zip")
    text, n = redact_signature_name(text, sender, protected) if signature else (text, 0)
    if n:
        counts["name"] = counts.get("name", 0) + n
    return text, counts


def neutralise_tags(text: str) -> str:
    """Stop email text from opening/closing our data delimiters."""
    return re.sub(r"<\s*(/?)\s*(email_body|email_subject)", r"&lt;\1\2", text, flags=re.I)


def prepare(email: dict, max_chars: int, protected_words: frozenset[str] = frozenset()) -> dict:
    """Full input guard for one email. Returns what may be sent plus metadata kept locally.
    protected_words: catalog vocabulary, never treated as a personal name."""
    body = email.get("body") or ""
    truncated = len(body) > max_chars
    if truncated:
        body = body[:max_chars]
    new_msg, quoted = split_quoted(body)
    sender = email.get("sender", "")
    red_body, c1 = redact(new_msg, sender, protected_words)
    red_subject, c2 = redact(email.get("subject") or "", sender, protected_words, signature=False)
    # Quoted history is redacted and sent ONLY as a marked block so the model can see
    # referenced quote/order numbers; the prompt forbids extracting lines from it.
    red_quoted, c3 = redact(quoted, sender, protected_words) if quoted else ("", {})
    counts = {k: c1.get(k, 0) + c2.get(k, 0) + c3.get(k, 0) for k in {*c1, *c2, *c3}}
    return {
        "subject": neutralise_tags(red_subject),
        "body": neutralise_tags(red_body),
        "quoted": neutralise_tags(red_quoted),
        "redactions": counts,
        "truncated": truncated,
        "text_below_quote": bool(text_below_quote(quoted)),
    }
