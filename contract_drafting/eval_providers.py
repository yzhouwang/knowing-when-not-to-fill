"""
eval_providers.py -- record-time LLM backends for the Gauntlet (eval-only).

These callers let the benchmark RECORD real model behavior across providers
without putting eval/provider concerns into the production llm.py. Replaying a
recorded cassette needs none of this (it reads from disk), so these are used
only with `gauntlet --record`.

The capability difference the benchmark exposes:

  OpenAIStrictCaller -- provider-native STRICT json_schema: a HARD constraint,
    out-of-enum is unrepresentable. Delegates to the production
    llm.call_llm_structured path verbatim (so we measure the real mechanism),
    with an explicit key + a higher token budget for reasoning models.

  DeepSeekCaller -- DeepSeek exposes NO strict json_schema (the API returns
    "response_format type is unavailable"); only json_object (JSON mode), which
    guarantees valid JSON but NOT schema/enum conformance. Its constrained arm is
    therefore SOFT and must never be reported as a by-construction guarantee. (It
    is also a reasoning model, hence the large max_tokens.)
"""
from __future__ import annotations

import json
from typing import Optional

from openai import OpenAI

from contract_drafting import demo_mars_beat as demo
from contract_drafting import llm

# Reasoning models (deepseek-v4-pro, gpt-5.x) spend tokens on hidden reasoning
# before emitting content; a small budget truncates the answer.
_EVAL_MAX_TOKENS = 8192


class OpenAIStrictCaller(demo.LLMCaller):
    """OpenAI backend using the production strict-json_schema path (hard enforce)."""

    def __init__(self, api_key: str, max_tokens: int = _EVAL_MAX_TOKENS):
        self.api_key = api_key
        self.max_tokens = max_tokens

    def text(self, question, context, *, provider, model, system_prompt):
        return llm.call_llm(
            question, context, provider="openai", model=model,
            api_key=self.api_key, max_tokens=self.max_tokens, system_prompt=system_prompt,
        )

    def structured(self, question, context, json_schema, *, provider, model, system_prompt):
        return llm.call_llm_structured(
            question, context, json_schema, provider="openai", model=model,
            api_key=self.api_key, max_tokens=self.max_tokens, system_prompt=system_prompt,
        )


class DeepSeekCaller(demo.LLMCaller):
    """DeepSeek backend (OpenAI-compatible API). Constrained arm is json_object
    (SOFT) because DeepSeek has no strict json_schema."""

    BASE_URL = "https://api.deepseek.com"
    DEFAULT_MODEL = "deepseek-v4-pro"

    def __init__(self, api_key: str, max_tokens: int = _EVAL_MAX_TOKENS):
        self.client = OpenAI(api_key=api_key, base_url=self.BASE_URL)
        self.max_tokens = max_tokens

    def text(self, question, context, *, provider, model, system_prompt):
        r = self.client.chat.completions.create(
            model=model or self.DEFAULT_MODEL,
            messages=[
                {"role": "system", "content": system_prompt or ""},
                {"role": "user", "content": f"{question}\n\n{context}"},
            ],
            max_tokens=self.max_tokens,
        )
        return r.choices[0].message.content or ""

    def structured(self, question, context, json_schema, *, provider, model, system_prompt):
        # SOFT constraint: json_object guarantees valid JSON, NOT schema conformance.
        sys = (system_prompt or "") + " Output ONLY a single JSON object that conforms to the schema."
        r = self.client.chat.completions.create(
            model=model or self.DEFAULT_MODEL,
            messages=[
                {"role": "system", "content": sys},
                {"role": "user", "content": f"{question}\n\n{context}"},
            ],
            response_format={"type": "json_object"},
            max_tokens=self.max_tokens,
        )
        content = r.choices[0].message.content or ""
        return json.loads(content)


def make_record_caller(provider: str, api_key: Optional[str] = None) -> demo.LLMCaller:
    """Build the record-time inner caller for a provider. api_key falls back to
    the provider's conventional env var inside each caller/llm path."""
    import os
    p = (provider or "").strip().lower()
    if p == "deepseek":
        return DeepSeekCaller(api_key or os.environ["DEEPSEEK_API_KEY"])
    if p == "openai":
        return OpenAIStrictCaller(api_key or os.environ["OPENAI_API_KEY"])
    if p == "anthropic":
        return demo.LiveCaller()  # production path via ANTHROPIC_API_KEY
    # Fail closed: an unknown provider must NOT fall through to LiveCaller, because
    # llm.call_llm treats any unknown provider as the OpenAI path -- the cassette and
    # report would be labeled with the requested provider while responses came from
    # OpenAI, silently misreporting cross-provider results.
    raise ValueError(
        f"unknown provider {provider!r}: supported = deepseek, openai, anthropic"
    )
