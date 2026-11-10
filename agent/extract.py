"""Model call: email text -> structured extraction. The model never sees or produces prices."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass, field

import jsonschema

from . import config
from .data import RefData, catalog_for_prompt


class ExtractionError(Exception):
    pass


@dataclass
class ExtractionResult:
    extraction: dict
    model: str
    prompt_version: str
    usage: dict = field(default_factory=dict)
    cost_usd: float = 0.0
    attempts: int = 1


def _strip_comment(text: str) -> str:
    return re.sub(r"^\s*<!--.*?-->\s*", "", text, flags=re.S)


# Keywords the API's strict tool mode may not accept. They are still enforced locally by jsonschema.
_STRICT_UNSUPPORTED = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
                       "minLength", "maxLength", "maxItems", "minItems", "pattern"}


def _strict_schema(schema):
    if isinstance(schema, dict):
        return {k: _strict_schema(v) for k, v in schema.items() if k not in _STRICT_UNSUPPORTED}
    if isinstance(schema, list):
        return [_strict_schema(v) for v in schema]
    return schema


class Prompts:
    def __init__(self, ref: RefData):
        system_tpl = _strip_comment((config.PROMPTS_DIR / "extract_system.md").read_text(encoding="utf-8"))
        self.user_tpl = _strip_comment((config.PROMPTS_DIR / "extract_user.md").read_text(encoding="utf-8"))
        self.tool = json.loads((config.PROMPTS_DIR / "extraction_schema.json").read_text(encoding="utf-8"))
        self.system = system_tpl.replace("{{CATALOG}}", catalog_for_prompt(ref))
        self.product_names = set(ref.products)
        self.version = hashlib.sha256(
            (self.system + self.user_tpl + json.dumps(self.tool, sort_keys=True)).encode()).hexdigest()[:12]

    def user_message(self, email_date: str, prepared: dict) -> str:
        nonce = secrets.token_hex(6)
        return (self.user_tpl.replace("{{EMAIL_DATE}}", email_date)
                .replace("{{SUBJECT}}", prepared["subject"])
                .replace("{{BODY}}", prepared["body"])
                .replace("{{QUOTED}}", prepared["quoted"] or "(none)")
                .replace("{{NONCE}}", nonce))


def validate(extraction: dict, prompts: Prompts) -> None:
    """Schema validation (full schema, including keywords stripped for strict mode)."""
    try:
        jsonschema.validate(extraction, prompts.tool["input_schema"])
    except jsonschema.ValidationError as e:
        raise ExtractionError(f"schema: {e.message} at {list(e.absolute_path)}") from e


def cost_of(model: str, usage: dict) -> float:
    p = config.PRICING.get(model)
    if not p:
        return 0.0
    i, o, cr, cw = p
    return round((usage.get("input_tokens", 0) * i + usage.get("output_tokens", 0) * o
                  + usage.get("cache_read_input_tokens", 0) * cr
                  + usage.get("cache_creation_input_tokens", 0) * cw) / 1e6, 6)


class AnthropicExtractor:
    name = "anthropic"

    def __init__(self, ref: RefData, model: str | None = None, effort: str | None = None):
        import anthropic  # imported lazily so fixture mode needs no key
        self.anthropic = anthropic
        self.client = anthropic.Anthropic()          # reads ANTHROPIC_API_KEY from the environment
        self.prompts = Prompts(ref)
        self.model = model or config.MODEL
        self.effort = effort or config.EFFORT
        self.strict = True
        self.use_fallback = self.model in config.FALLBACK_MODELS   # dropped if the API key can't use the beta

    def _call(self, user_text: str):
        tool = {**copy.deepcopy(self.prompts.tool)}
        tool["input_schema"] = _strict_schema(tool["input_schema"]) if self.strict else tool["input_schema"]
        if self.strict:
            tool["strict"] = True
        kwargs = dict(
            model=self.model,
            max_tokens=config.MAX_TOKENS,
            system=[{"type": "text", "text": self.prompts.system, "cache_control": {"type": "ephemeral"}}],
            tools=[tool],
            tool_choice={"type": "auto"},     # forced tool_choice is rejected on current models; the prompt requires the call
            output_config={"effort": self.effort},
            messages=[{"role": "user", "content": user_text}],
        )
        if self.use_fallback:
            kwargs.update(betas=[config.FALLBACK_BETA], fallbacks="default")
        return self.client.beta.messages.create(**kwargs)

    def extract(self, email_date: str, prepared: dict) -> ExtractionResult:
        user_text = self.prompts.user_message(email_date, prepared)
        usage_total: dict[str, int] = {}
        served_by = self.model
        last_err = "unknown"
        attempt = downgrades = 0
        while attempt <= config.MAX_RETRIES:
            try:
                resp = self._call(user_text)
            except self.anthropic.BadRequestError as e:
                # A 400 means our request shape, not the email. Drop optional features once each
                # (they stay off for the rest of the run) without using up a model retry.
                msg = str(e).lower()
                if self.strict and ("strict" in msg or "schema" in msg) and downgrades < 2:
                    self.strict = False           # strict schema rejected: keep local validation
                    downgrades += 1
                    last_err = f"strict schema rejected: {e}"
                    continue
                if self.use_fallback and downgrades < 2:
                    self.use_fallback = False     # this API key may not have the fallback beta
                    downgrades += 1
                    last_err = f"request rejected with refusal fallback on; retrying without it: {e}"
                    continue
                raise ExtractionError(f"bad request: {e}") from e
            except (self.anthropic.RateLimitError, self.anthropic.APIConnectionError,
                    self.anthropic.InternalServerError) as e:
                attempt += 1
                last_err = f"transient API error: {e}"
                time.sleep(2 * attempt)
                continue
            except self.anthropic.APIStatusError as e:
                raise ExtractionError(f"API error {e.status_code}: {e}") from e
            attempt += 1

            u = resp.usage
            for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                usage_total[k] = usage_total.get(k, 0) + (getattr(u, k, 0) or 0)
            served_by = getattr(resp, "model", self.model) or self.model

            if resp.stop_reason == "refusal":
                last_err = "model refused"
                continue
            if resp.stop_reason == "max_tokens":
                last_err = "hit max_tokens"
                continue
            calls = [b for b in resp.content if getattr(b, "type", None) == "tool_use" and b.name == "record_extraction"]
            if len(calls) != 1:
                last_err = f"expected 1 record_extraction call, got {len(calls)}"
                continue
            extraction = calls[0].input if isinstance(calls[0].input, dict) else json.loads(calls[0].input)
            try:
                validate(extraction, self.prompts)
            except ExtractionError as e:
                last_err = str(e)
                continue
            return ExtractionResult(extraction, served_by, self.prompts.version, usage_total,
                                    cost_of(served_by, usage_total), attempt)
        err = ExtractionError(last_err)
        err.usage = usage_total                  # type: ignore[attr-defined]
        err.cost_usd = cost_of(served_by, usage_total)  # type: ignore[attr-defined]
        raise err


class FixtureExtractor:
    """Offline test double: returns hand-written extractions from tests/fixture_extractions.py.

    Used only to test the deterministic pipeline (matching, pricing, flags, store) without an
    API key. Its output is NOT model output and must never be submitted as the agent's results.
    """
    name = "fixture"

    def __init__(self, ref: RefData):
        from tests.fixture_extractions import FIXTURES
        self.fixtures = FIXTURES
        self.prompts = Prompts(ref)

    def extract_for(self, email_id: str) -> ExtractionResult:
        if email_id not in self.fixtures:
            raise ExtractionError(f"no fixture for {email_id}")
        ex = copy.deepcopy(self.fixtures[email_id])
        validate(ex, self.prompts)
        return ExtractionResult(ex, "fixture (hand-written, not model output)", self.prompts.version)
