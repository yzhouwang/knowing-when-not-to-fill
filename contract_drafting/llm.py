"""
llm.py — LLM provider routing for contract drafting.

Extracted from law-research/law_qa.py. Supports Anthropic and OpenAI providers.
"""
from __future__ import annotations

import copy
import json
import logging
import os
from datetime import datetime
from typing import Any, Optional

from anthropic import Anthropic
from openai import OpenAI

log = logging.getLogger(__name__)

DEFAULT_LLM_PROVIDER = "anthropic"
DEFAULT_LLM_ANTHROPIC = "claude-sonnet-4-20250514"
DEFAULT_LLM_OPENAI = "gpt-4o"
MAX_TOKENS_LLM = 4096

# Name of the synthetic tool / json_schema used to force structured output.
_STRUCTURED_TOOL_NAME = "emit_fields"


def _call_anthropic(
    question: str, context: str, *, model: str, max_tokens: int,
    system_prompt: Optional[str] = None, api_key: Optional[str] = None,
) -> str:
    key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise ValueError("ANTHROPIC_API_KEY is not set.")
    client = Anthropic(api_key=key)
    system = system_prompt or "You are a legal document assistant."
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": f"{question}\n\n{context}"}],
    )
    return msg.content[0].text


def _call_openai(
    question: str, context: str, *, model: str, max_tokens: int,
    system_prompt: Optional[str] = None, api_key: Optional[str] = None,
) -> str:
    key = api_key or os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise ValueError("OPENAI_API_KEY is not set.")
    client = OpenAI(api_key=key)
    system = system_prompt or "You are a legal document assistant."
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"{question}\n\n{context}"},
    ]
    from openai import BadRequestError
    try:
        resp = client.chat.completions.create(
            model=model, max_completion_tokens=max_tokens, messages=messages,
        )
    except BadRequestError:
        resp = client.chat.completions.create(
            model=model, max_tokens=max_tokens, messages=messages,
        )
    return resp.choices[0].message.content or ""


def call_llm(
    question: str,
    context: str,
    *,
    provider: str = DEFAULT_LLM_PROVIDER,
    model: Optional[str] = None,
    max_tokens: int = MAX_TOKENS_LLM,
    api_key: Optional[str] = None,
    system_prompt: Optional[str] = None,
) -> str:
    """Route to appropriate LLM provider."""
    provider_norm = (provider or "").strip().lower()
    if model:
        resolved_model = model
    elif provider_norm == "anthropic":
        resolved_model = DEFAULT_LLM_ANTHROPIC
    elif provider_norm == "openai":
        resolved_model = DEFAULT_LLM_OPENAI
    else:
        resolved_model = DEFAULT_LLM_OPENAI

    if provider_norm == "anthropic":
        return _call_anthropic(
            question, context, model=resolved_model,
            max_tokens=max_tokens, system_prompt=system_prompt, api_key=api_key,
        )
    return _call_openai(
        question, context, model=resolved_model,
        max_tokens=max_tokens, system_prompt=system_prompt, api_key=api_key,
    )


# ---------------------------------------------------------------------------
# Tier-1: provider-native constrained / structured-output generation.
#
# Both branches ask the provider to return a JSON object conforming to a
# caller-supplied JSON Schema (including enum constraints) via the provider's
# native structured-output mode. The strength of the guarantee differs by
# provider: OpenAI strict json_schema is a HARD constraint (an out-of-enum value
# is unrepresentable), while Anthropic's tool input_schema is a STRONG STEER, not
# a hard guarantee. Callers that need a guarantee must still re-validate the
# returned dict (e.g. with schema_validator.validate_template_data).
# ---------------------------------------------------------------------------

# T9: cache of OpenAI-strict-massaged schemas, keyed by a stable hash of the
# *input* schema. Mirrors the simple caching style of schema_validator.py's
# `_schema_cache`. Avoids rebuilding the massaged copy on repeated calls with
# the same schema (the common case: one schema reused for every field-emit).
_massaged_schema_cache: dict[str, dict] = {}


def _massage_for_openai_strict(schema: dict) -> dict:
    """Rewrite a draft-07-style JSON Schema for OpenAI strict structured output.

    OpenAI's `response_format=json_schema` with ``strict=True`` imposes extra
    requirements beyond plain JSON Schema:

    * every object must set ``"additionalProperties": false``;
    * every object must list *all* of its properties in ``"required"``;
    * it expects ``$defs`` (not draft-07's ``definitions``) for shared
      sub-schemas, though it does honour ``$ref``.

    This helper deep-copies the input and, recursively, on every object node:
    sets ``additionalProperties=false`` and ``required=list(properties)``. At
    the top level it renames ``definitions`` -> ``$defs`` and rewrites any
    ``#/definitions/`` ``$ref`` to ``#/$defs/``. It also drops meta keys OpenAI
    rejects (``$schema``, ``$id``, ``$comment``). ``enum`` / ``$ref`` / ``type``
    are preserved untouched. Returns the massaged copy.

    Results are cached (T9) keyed by a stable hash of the *input* schema, so
    repeated calls with the same schema return the same massaged dict without
    recomputation.
    """
    # Stable cache key: json.dumps(..., sort_keys=True) is order-independent.
    cache_key = json.dumps(schema, sort_keys=True)
    cached = _massaged_schema_cache.get(cache_key)
    if cached is not None:
        # Return a copy: handing out the cached object by reference lets a caller
        # that mutates the result silently poison the cache for every later call.
        return copy.deepcopy(cached)

    massaged = copy.deepcopy(schema)

    # Rename top-level draft-07 "definitions" -> "$defs" and rewrite $refs.
    if "definitions" in massaged:
        massaged["$defs"] = massaged.pop("definitions")
        _rewrite_definitions_refs(massaged)

    # Recursively enforce the strict-mode object requirements + drop bad keys.
    _strict_walk(massaged)

    _massaged_schema_cache[cache_key] = massaged
    return copy.deepcopy(massaged)


def _rewrite_definitions_refs(node: Any) -> None:
    """In-place: rewrite every ``#/definitions/...`` $ref to ``#/$defs/...``."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/definitions/"):
            node["$ref"] = ref.replace("#/definitions/", "#/$defs/", 1)
        for value in node.values():
            _rewrite_definitions_refs(value)
    elif isinstance(node, list):
        for item in node:
            _rewrite_definitions_refs(item)


# Keys OpenAI strict structured output does not support; stripped from every node
# so the massager handles a raw Concerto schema.json on its own. Two groups:
#   - Concerto meta ($decorators/$class would sit illegally beside a $ref);
#   - JSON-Schema validation keywords OpenAI strict rejects (default, minLength,
#     pattern, ...). The enum is preserved (that IS the constraint we care about),
#     and the post-hoc validator still enforces these, so dropping them only
#     affects the generation grammar, not correctness.
_OPENAI_REJECTED_KEYS = (
    "$schema", "$id", "$comment", "$decorators", "$class",
    "default", "minLength", "maxLength", "pattern", "format",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "multipleOf", "minItems", "maxItems",
)


def _strict_walk(node: Any) -> None:
    """In-place recursive massage for OpenAI strict mode.

    On every dict node: drop rejected meta keys, and — when it describes an
    object with ``properties`` — set ``additionalProperties=false`` and
    ``required`` to the full property list. Recurses into nested schemas
    (``properties`` values, ``$defs``, ``items``, combinators, etc.).
    """
    if isinstance(node, dict):
        for bad in _OPENAI_REJECTED_KEYS:
            node.pop(bad, None)

        props = node.get("properties")
        if isinstance(props, dict):
            node["additionalProperties"] = False
            # strict mode: *every* property must be required.
            node["required"] = list(props.keys())

        for value in node.values():
            _strict_walk(value)
    elif isinstance(node, list):
        for item in node:
            _strict_walk(item)


def _call_anthropic_structured(
    question: str, context: str, json_schema: dict, *,
    model: str, max_tokens: int,
    system_prompt: Optional[str] = None, api_key: Optional[str] = None,
) -> dict:
    """Anthropic structured output via a forced tool call.

    Declares a single tool whose ``input_schema`` IS the caller's JSON Schema
    and forces the model to call it (``tool_choice`` = that tool). This strongly
    steers the model toward a conforming object but is NOT a hard guarantee the
    way OpenAI strict mode is; callers needing a guarantee should re-validate the
    returned dict. We extract the ``tool_use`` block and return its ``.input``.
    """
    key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise ValueError("ANTHROPIC_API_KEY is not set.")
    client = Anthropic(api_key=key)
    system = system_prompt or "You are a legal document assistant."
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": f"{question}\n\n{context}"}],
        tools=[{
            "name": _STRUCTURED_TOOL_NAME,
            "description": "Emit the contract field values.",
            # Pass the schema through verbatim — the enum constraint is wired
            # straight into the provider's structured-output enforcement.
            "input_schema": json_schema,
        }],
        tool_choice={"type": "tool", "name": _STRUCTURED_TOOL_NAME},
    )
    for block in msg.content:
        if getattr(block, "type", None) == "tool_use":
            if not isinstance(block.input, dict):
                raise ValueError(
                    "Anthropic tool_use input was not a JSON object "
                    f"(got {type(block.input).__name__})."
                )
            return block.input
    raise ValueError("Anthropic response contained no tool_use block.")


def _call_openai_structured(
    question: str, context: str, json_schema: dict, *,
    model: str, max_tokens: int,
    system_prompt: Optional[str] = None, api_key: Optional[str] = None,
) -> dict:
    """OpenAI structured output via ``response_format=json_schema`` strict mode.

    The schema is massaged (T9-cached) to satisfy OpenAI strict-mode rules,
    then passed as the ``json_schema`` response format with ``strict=True``.
    The model's message content is a JSON string conforming to that schema,
    which we parse and return as a dict.
    """
    key = api_key or os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise ValueError("OPENAI_API_KEY is not set.")
    client = OpenAI(api_key=key)
    system = system_prompt or "You are a legal document assistant."
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"{question}\n\n{context}"},
    ]
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": _STRUCTURED_TOOL_NAME,
            "schema": _massage_for_openai_strict(json_schema),
            "strict": True,
        },
    }
    from openai import BadRequestError
    try:
        resp = client.chat.completions.create(
            model=model, max_completion_tokens=max_tokens,
            messages=messages, response_format=response_format,
        )
    except BadRequestError:
        # Fallback ONLY for the older max_tokens vs max_completion_tokens param
        # split (same response_format). A genuine schema rejection re-raises here.
        resp = client.chat.completions.create(
            model=model, max_tokens=max_tokens,
            messages=messages, response_format=response_format,
        )
    content = resp.choices[0].message.content
    if not content:
        raise ValueError("OpenAI structured response was empty.")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        # Strict mode should yield valid JSON; a parse failure usually means the
        # response was truncated (finish_reason="length"). Surface it clearly
        # rather than letting an opaque decode error escape.
        raise ValueError(f"OpenAI structured response was not valid JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise ValueError(
            f"OpenAI structured response was not a JSON object (got {type(parsed).__name__})."
        )
    return parsed


def call_llm_structured(
    question: str,
    context: str,
    json_schema: dict,
    *,
    provider: str = DEFAULT_LLM_PROVIDER,
    model: Optional[str] = None,
    max_tokens: int = MAX_TOKENS_LLM,
    api_key: Optional[str] = None,
    system_prompt: Optional[str] = None,
) -> dict:
    """Force an LLM to return a dict that conforms to ``json_schema``.

    Uses provider-native structured output (Anthropic forced tool call /
    OpenAI ``response_format=json_schema`` strict mode). OpenAI strict mode hard-
    enforces the schema (out-of-enum is unrepresentable); the Anthropic forced
    tool call strongly steers but does not hard-guarantee, so callers needing a
    guarantee should re-validate the returned dict. Returns the parsed dict.
    Routes by ``provider`` exactly like :func:`call_llm`.

    WARNING -- well-typed is NOT correct. This guarantees the output conforms to the
    schema (a value IN the enum), NOT that it is the value the user asked for. When a
    request names something the schema cannot represent (e.g. "the laws of Ontario"),
    the constraint forces a valid-but-WRONG value (Ontario -> Delaware) with no signal,
    and an omitted field renders the .cto default. The Gauntlet eval measured ~56-78%
    such silent substitution on un-representable inputs. Before using output in a real
    contract, run contract_drafting.intent_check.verify_intent(instruction, fields)
    and review/abstain on any warning. See TODOS.md "Product hardening".
    """
    provider_norm = (provider or "").strip().lower()
    if model:
        resolved_model = model
    elif provider_norm == "anthropic":
        resolved_model = DEFAULT_LLM_ANTHROPIC
    elif provider_norm == "openai":
        resolved_model = DEFAULT_LLM_OPENAI
    else:
        resolved_model = DEFAULT_LLM_OPENAI

    if provider_norm == "anthropic":
        return _call_anthropic_structured(
            question, context, json_schema, model=resolved_model,
            max_tokens=max_tokens, system_prompt=system_prompt, api_key=api_key,
        )
    return _call_openai_structured(
        question, context, json_schema, model=resolved_model,
        max_tokens=max_tokens, system_prompt=system_prompt, api_key=api_key,
    )
