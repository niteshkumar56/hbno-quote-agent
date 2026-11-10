"""Deterministic product matching. The model's suggestion is one input; code decides.

Scores (per catalog product):
  1.00  a catalog alias / name / configured synonym appears as a whole phrase, and every other
        meaningful word in the request is also explained by that product
  0.80  an alias appears, but the request has extra qualifying words (e.g. 'clove LEAF')
  0.70  request words are a subset of one product's name (e.g. 'coconut' -> Fractionated Coconut)
  ~     close spelling (typo) of an alias, scaled below 1
Final confidence = min(model confidence, code score). A model pick the code cannot support scores 0.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field

from .data import RefData

STOPWORDS = {
    "oil", "oils", "essential", "eo", "carrier", "of", "the", "a", "an", "and", "pure", "bulk", "some",
    "your", "our", "please", "pls", "quote", "x", "each", "for", "cada", "uno", "aceite", "de", "del",
    "la", "el", "huile", "d", "drum", "drums", "gal", "gallon", "gallons", "galones", "galon", "oz",
    "ounce", "pail", "pails", "cubeta", "cubetas", "jug", "jugs", "bottle", "bottles", "lb", "lbs",
    "kg", "sample", "samples", "more", "also",
}
# Marketing filler that never distinguishes one catalog product from another. Words that DO name a
# grade, origin or plant part (organic, bulgarian, leaf, food...) are deliberately NOT here, so they
# still block an exact match.
FILLER_WORDS = {
    "natural", "premium", "therapeutic", "grade", "quality", "high", "top", "best", "finest", "fine",
    "genuine", "authentic", "fresh", "wholesale", "standard", "regular",
}
STOPWORDS |= FILLER_WORDS


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"[^a-z0-9/ ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def tokens(text: str) -> list[str]:
    return [t for t in norm(text).split() if t not in STOPWORDS and not re.fullmatch(r"\d+(?:/\d+)?", t)
            or re.fullmatch(r"\d+/\d+", t)]


def _has_phrase(text: str, phrase: str) -> bool:
    return re.search(rf"(?<![a-z0-9/]){re.escape(phrase)}(?![a-z0-9/])", text) is not None


@dataclass
class MatchResult:
    product: str | None                 # chosen product name, or None
    score: float                        # code score for the chosen product (0 if none)
    candidates: list[str] = field(default_factory=list)   # product names a rep should consider
    reason: str = ""


def _names(ref: RefData, product: str) -> list[str]:
    p = ref.products[product]
    base = norm(re.sub(r"\b(essential oil|oil)\b", "", p.name, flags=re.I))
    return sorted({norm(a) for a in p.aliases} | {norm(p.name), base} |
                  {norm(s) for s in ref.synonyms.get(product, [])}, key=len, reverse=True)


def code_match(ref: RefData, product_text: str) -> MatchResult:
    text = norm(product_text)
    req_tokens = set(tokens(product_text))
    if not text:
        return MatchResult(None, 0.0, [], "empty product text")

    phrase_hits: dict[str, tuple[float, int]] = {}      # product -> (score, longest phrase len)
    for prod in ref.products:
        names = _names(ref, prod)
        hits = [n for n in names if n and _has_phrase(text, n)]
        if not hits:
            continue
        explained = set()
        for n in names:
            if _has_phrase(text, n):
                explained |= set(tokens(n)) | set(n.split())
        leftover = req_tokens - explained
        phrase_hits[prod] = (1.0 if not leftover else 0.8, max(len(h) for h in hits))

    if phrase_hits:
        best_len = max(v[1] for v in phrase_hits.values())
        top = [p for p, v in phrase_hits.items() if v[1] == best_len]
        if len(top) == 1:
            p = top[0]
            score = phrase_hits[p][0]
            return MatchResult(p, score, [p], "alias match" if score == 1.0 else "alias match with extra qualifiers")
        return MatchResult(None, 0.0, sorted(top), "several products match equally")

    # No alias phrase: are the request words a subset of product names? (e.g. 'eucalyptus')
    if req_tokens:
        subset = [p for p in ref.products
                  if req_tokens <= {t for n in _names(ref, p) for t in n.split()}]
        if len(subset) == 1:
            return MatchResult(subset[0], 0.7, subset, "partial name; not specific enough to auto-quote")
        if len(subset) > 1:
            return MatchResult(None, 0.0, sorted(subset), "ambiguous: request fits several products")

    # Typo tolerance against aliases.
    best, best_r = None, 0.0
    for prod in ref.products:
        for n in _names(ref, prod):
            r = difflib.SequenceMatcher(None, text, n).ratio()
            if r > best_r:
                best, best_r = prod, r
    if best and best_r >= 0.88:
        return MatchResult(best, round(best_r * 0.95, 3), [best], f"close spelling ({best_r:.2f})")
    if best and best_r >= 0.75:
        return MatchResult(None, 0.0, [best], f"weak similarity ({best_r:.2f}); likely not carried")
    return MatchResult(None, 0.0, [], "no catalog product matches")


def products_mentioned(ref: RefData, text: str) -> set[str]:
    """Catalog products the customer's own words refer to. Used to check the model's product_text:
    a line is only matched if its product is named in the verbatim source_text. Several products
    may be named in one sentence ('5 gal each of peppermint and lemon')."""
    t = norm(text)
    hits = {p for p in ref.products if any(n and _has_phrase(t, n) for n in _names(ref, p))}
    if hits:
        return hits
    cm = code_match(ref, text)
    return ({cm.product} if cm.product else set()) | set(cm.candidates)


def resolve(ref: RefData, product_text: str, model_product: str | None, model_conf: float,
            model_candidates: list[str]) -> dict:
    """Combine model suggestion and code match into a final decision."""
    cm = code_match(ref, product_text)
    model_product = model_product if model_product in ref.products else None
    candidates = sorted(set(cm.candidates) | {c for c in model_candidates if c in ref.products})

    if model_product and cm.product == model_product:
        conf = min(model_conf, cm.score)
        return {"product": model_product, "confidence": round(conf, 3), "candidates": candidates,
                "status": "matched", "reason": cm.reason}
    if cm.product is None and len(candidates) > 1:
        return {"product": None, "confidence": 0.0, "candidates": candidates, "status": "ambiguous",
                "reason": cm.reason or "model listed several candidates"}
    if model_product is None and cm.product is None:
        return {"product": None, "confidence": 0.0, "candidates": candidates, "status": "no_match",
                "reason": cm.reason}
    # Model and code disagree, or only one side found a product: never auto-quote.
    pick = cm.product or model_product
    return {"product": pick, "confidence": round(min(model_conf, cm.score) if cm.product == pick else 0.0, 3),
            "candidates": candidates or [pick], "status": "unconfirmed",
            "reason": f"model={model_product!r}, code={cm.product!r} ({cm.reason})"}
