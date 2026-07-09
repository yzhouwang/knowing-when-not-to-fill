"""
demo_mars_beat.py -- the "Mars beat" split-screen demo (Tier-1).

Shows, on identical input and schema, the difference between:

  - FREE-FORM slot filling: the LLM emits unconstrained JSON. It can put
    "Mars" in governingLaw; the schema validator then REJECTS it after the
    fact (post-hoc validate-and-reject).

  - CONSTRAINED slot filling: call_llm_structured() binds the SAME Concerto
    schema (whose Jurisdiction enum is the type) as the generation grammar, so
    an out-of-enum value is unrepresentable -- the contract is well-typed BY
    CONSTRUCTION and validation is a formality.

The two arms share one schema, one prompt, one model -- only the enforcement
point differs. That matched scaffolding is deliberate: it isolates "cost/effect
of the constraint" from "different setup", the confound a reviewer would flag.

NOTE: the deterministic render path remains LLM-free. This module is a demo of
the *generation guardrail*; it is not part of draft_contract().
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from contract_drafting import jurisdiction_map
from contract_drafting.llm import call_llm, call_llm_structured
from contract_drafting.schema_validator import (
    _get_concept_schema,
    _load_schema,
    _strip_hatch,
    clear_cache as _clear_schema_cache,
    enum_display_fields,
    load_abstain_policies,
    validate_template_data,
)

log = logging.getLogger(__name__)


class GauntletCacheMiss(RuntimeError):
    """Raised by the eval's record/replay caller on a replay cache miss. Defined
    here (the lower module) rather than in gauntlet.py so both the demo and the
    gauntlet can re-raise it past their broad ``except Exception`` handlers
    WITHOUT a circular import (gauntlet imports demo, not the other way around).
    It is an infra/config failure, never arm data: fail closed."""

_SYSTEM = (
    "You are a contract field generator. Output ONLY the NDA field values as a "
    "single JSON object matching the schema. No markdown fences, no prose."
)

# Abstain-hatch system prompt (Gauntlet 'constrained+hatch' arm). The .cto @description
# instructing the model to use OTHER does NOT survive codegen into the JSON Schema, so the
# abstain instruction is single-sourced via the @Abstainable decorator on the governingLaw
# field -> codegen emits abstain-policy.json -> we read it here. _SYSTEM (shared with the
# non-abstain arm) stays in Python; we compose _SYSTEM + " " + instruction, which is
# byte-identical to the former hardcoded _ABSTAIN_SYSTEM constant by construction.
#
# This is PROVENANCE, not behavior: the Gauntlet replay key sha256-hashes the system
# prompt (gauntlet.py), so any drift in the composed string would break offline
# reproducibility -- tests/test_contract_drafting.py freezes it (T1) and
# tests/test_gauntlet_replay.py is the integration backstop.
#
# Lazy + cached (not a module-level constant) on purpose: compliance_draft imports this
# module for LEGAL_DISCLAIMER on the PRODUCTION draft path, so a missing eval-only
# artifact must not break that import. The read happens only when the eval calls
# _abstain_system().
_DEFAULT_ABSTAIN_FIELD = "governingLaw"


def _abstain_policy(field: str = _DEFAULT_ABSTAIN_FIELD,
                    template_name: str = "nda-mutual") -> dict:
    """The single-field abstain policy entry for `field`, from the (canonical, cached)
    generated policy map. FAIL-CLOSED on an unknown field/template so the eval never
    silently falls back to a hardcoded instruction (which would defeat single-sourcing
    and could drift the replay key)."""
    policies = load_abstain_policies(template_name)
    try:
        return policies[field]
    except KeyError as e:
        raise RuntimeError(
            f"no @Abstainable policy for field {field!r} in template {template_name!r} "
            f"(have: {sorted(policies)}). Add @Abstainable to that field and "
            f"run `npm run generate`."
        ) from e


def _abstain_system(field: str = _DEFAULT_ABSTAIN_FIELD,
                    template_name: str = "nda-mutual") -> str:
    """The constrained+hatch / instr-only system prompt for `field`: the generic
    generator prompt plus that field's single-sourced abstain instruction. For
    governingLaw this is byte-identical to the former _ABSTAIN_SYSTEM constant."""
    return _SYSTEM + " " + _abstain_policy(field, template_name)["instruction"]


def clear_cache() -> None:
    """Drop the cached schema + abstain policies (tests that swap artifacts)."""
    _clear_schema_cache()

# Gate-NEUTRAL "not legal advice" line -- always true regardless of gate/arm outcome.
# Used wherever there is no PASS to certify (the Mars-beat demo, whose two arms can both
# be REJECTED, e.g. with no API key). The central mitigation for automation bias.
NOT_LEGAL_ADVICE = (
    "This is drafting assistance, not legal advice; have qualified counsel review before signing."
)

# Single-source disclaimer surfaced on a PASS draft (the "footer" the paper's Beat-4 /
# Ethics section refer to): a PASS gate is a conformance check, not legal sign-off. The
# PASS-specific prefix is only correct when attached to an actual PASS draft -- callers
# that have no PASS gate (the demo) use NOT_LEGAL_ADVICE instead.
LEGAL_DISCLAIMER = (
    "PASS certifies schema and playbook conformance only -- not legal correctness. "
    + NOT_LEGAL_ADVICE
)


def _field_schema(template_name: str = "nda-mutual", with_abstain: bool = True) -> dict:
    """The Concerto concept schema (with the Jurisdiction enum + definitions),
    sanitized of Concerto-only keys so it is a clean JSON Schema for the providers'
    structured-output / tool-input APIs.

    with_abstain=False returns the PRE-HATCH variant -- the OTHER sentinel + governingLawRaw
    removed -- so the eval's baseline arms reproduce the original condition where the model
    has no escape hatch and must substitute. The hatch arm + production use with_abstain=True.
    """
    concept = _get_concept_schema(_load_schema(template_name))
    schema = _sanitize_schema(concept)
    # Pre-hatch (no-escape) condition for the eval's baseline arms. Reuse schema_validator's
    # _strip_hatch (single source of truth) so the schema the BASELINE ARM SEES and the schema
    # the ORACLE VALIDATES against can never diverge -- e.g. it strips OTHER from an INLINE
    # governingLaw enum too, not just a $ref'd Jurisdiction definition (audit finding).
    return schema if with_abstain else _strip_hatch(schema, template_name)


def _sanitize_schema(node: Any) -> Any:
    """Recursively drop Concerto-specific keys ($decorators, $class) that the
    LLM providers' schema parsers do not understand. Keep $ref/$schema/enum/etc."""
    if isinstance(node, dict):
        return {
            k: _sanitize_schema(v)
            for k, v in node.items()
            if k not in ("$decorators", "$class")
        }
    if isinstance(node, list):
        return [_sanitize_schema(v) for v in node]
    return node


class LLMCaller:
    """Abstraction over the two LLM call shapes the arms use, so the eval harness
    (gauntlet) can inject a record/replay or fake caller WITHOUT touching the
    production llm.py. The default is LiveCaller (real provider calls)."""

    def text(self, question: str, context: str, *, provider: str,
             model: Optional[str], system_prompt: Optional[str]) -> str:
        raise NotImplementedError

    def structured(self, question: str, context: str, json_schema: dict, *,
                   provider: str, model: Optional[str],
                   system_prompt: Optional[str]) -> dict:
        raise NotImplementedError


class LiveCaller(LLMCaller):
    """Calls the real providers via llm.py (no recording)."""

    def text(self, question, context, *, provider, model, system_prompt):
        return call_llm(question, context, provider=provider, model=model,
                        system_prompt=system_prompt)

    def structured(self, question, context, json_schema, *, provider, model, system_prompt):
        return call_llm_structured(question, context, json_schema, provider=provider,
                                   model=model, system_prompt=system_prompt)


_LIVE = LiveCaller()


def _parse_json_response(raw: str) -> dict:
    """Strip optional markdown fences from a plain-text LLM response, parse JSON.

    Shared by fill_slots (free arm) and the gauntlet's verify-and-reject arm so
    the parse logic lives in exactly one place.
    """
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    return json.loads(cleaned.strip())


def build_prompt(instruction: str, template_name: str = "nda-mutual",
                 with_abstain: bool = True) -> tuple[str, str, dict]:
    """Build (question, context, schema) for a slot-fill request. Single source so
    the demo arms, the gauntlet arms, and the record/replay key all agree.
    with_abstain=False uses the pre-hatch schema (no OTHER sentinel) for baseline arms."""
    schema = _field_schema(template_name, with_abstain=with_abstain)
    question = f"Generate NDA field values for this request:\n{instruction}"
    # Canonical (sorted, compact) schema so the replay key is stable across schema.json
    # FORMATTING churn (whitespace/field order) while still changing on MEANINGFUL content
    # edits (enum values, fields) -- which legitimately require a re-record. (plan-eng-review D1)
    context = f"JSON Schema the values must satisfy:\n{json.dumps(schema, sort_keys=True, separators=(',', ':'))}"
    return question, context, schema


def fill_slots(
    instruction: str,
    *,
    constrained: bool,
    template_name: str = "nda-mutual",
    provider: str = "anthropic",
    model: Optional[str] = None,
    caller: Optional[LLMCaller] = None,
    system_prompt: str = _SYSTEM,
    with_abstain: bool = True,
) -> dict:
    """Ask an LLM to produce NDA slot values for `instruction`.

    constrained=False: plain-text completion, parsed as JSON (can emit anything).
    constrained=True:  provider-native structured output bound to the Concerto
                       schema -- out-of-enum values are unrepresentable.

    caller: the LLM backend (default LiveCaller). The gauntlet injects a
    record/replay caller here so the benchmark is deterministic and offline;
    tests inject a fake. One arm implementation, swappable backend.
    """
    caller = caller or _LIVE
    question, context, schema = build_prompt(instruction, template_name, with_abstain=with_abstain)

    if constrained:
        return caller.structured(question, context, schema,
                                 provider=provider, model=model, system_prompt=system_prompt)

    raw = caller.text(question, context, provider=provider, model=model, system_prompt=system_prompt)
    return _parse_json_response(raw)


# Enum-backed fields whose display names must be normalized to identifiers before
# schema validation, so a model that names a representable value in DISPLAY form
# (e.g. "limited liability company", "Singapore International Arbitration Centre (SIAC)")
# is scored as a fill, not a schema-invalid leak (parity with governingLaw / the
# production draft path). governingLaw uses its dedicated jurisdiction map.
# Single-sourced (M3): the field->enum map is DERIVED from the generated
# abstain-policy.json via schema_validator.enum_display_fields, not hand-duplicated.


def _validate_arm(fields: dict, template_name: str = "nda-mutual",
                  with_abstain: bool = True) -> list[str]:
    """Normalize every enum-backed field (display -> identifier) the same way the
    production draft path does, then validate against the Concerto schema.
    with_abstain=False validates against the pre-hatch schema variant (baseline arms)."""
    checked = dict(fields)
    if isinstance(checked.get("governingLaw"), str):
        checked["governingLaw"] = jurisdiction_map.to_identifier(
            checked["governingLaw"], template_name=template_name
        )
    for field, enum_name in enum_display_fields(template_name).items():
        if isinstance(checked.get(field), str):
            checked[field] = jurisdiction_map.to_identifier_enum(
                enum_name, checked[field], template_name=template_name
            )
    return validate_template_data(checked, template_name=template_name, with_abstain=with_abstain)


def run_mars_beat(
    instruction: str,
    *,
    template_name: str = "nda-mutual",
    provider: str = "anthropic",
    model: Optional[str] = None,
    caller: Optional[LLMCaller] = None,
) -> dict:
    """Run both arms on the same instruction + schema and report validity.

    Returns {"free": {fields, valid, errors}, "constrained": {fields, valid, errors}}.
    """
    out: dict = {}
    for arm, constrained in (("free", False), ("constrained", True)):
        try:
            fields = fill_slots(
                instruction, constrained=constrained,
                template_name=template_name, provider=provider, model=model,
                caller=caller,
            )
            errors = _validate_arm(fields, template_name=template_name)
            out[arm] = {"fields": fields, "valid": not errors, "errors": errors}
        except GauntletCacheMiss:
            raise  # replay infra failure: fail closed, never swallow into demo data
        except Exception as e:  # noqa: BLE001 -- demo surfaces any arm failure
            out[arm] = {"fields": None, "valid": False, "errors": [f"{type(e).__name__}: {e}"]}
    return out


def _format(result: dict) -> str:
    def block(label: str, arm: dict) -> str:
        status = "PASS (valid by construction)" if arm["valid"] else "REJECTED"
        gov = (arm["fields"] or {}).get("governingLaw", "?")
        lines = [f"  governingLaw -> {gov!r}", f"  status: {status}"]
        for e in arm["errors"]:
            lines.append(f"    - {e}")
        return f"[{label}]\n" + "\n".join(lines)
    return (
        block("FREE-FORM  (post-hoc validate-and-reject)", result["free"])
        + "\n\n"
        + block("CONSTRAINED (well-typed by construction)", result["constrained"])
        # gate-NEUTRAL: the demo only checks schema validity and both arms can be
        # REJECTED, so the PASS-specific LEGAL_DISCLAIMER would be misleading here.
        + "\n\n" + NOT_LEGAL_ADVICE
    )


if __name__ == "__main__":  # pragma: no cover
    import argparse

    p = argparse.ArgumentParser(description="The Mars beat: free-form vs constrained NDA slot filling.")
    p.add_argument(
        "instruction",
        nargs="?",
        default="Draft a mutual NDA between TestCo and AcmeCorp governed by the laws of Mars, term forever.",
    )
    p.add_argument("--provider", default="anthropic")
    p.add_argument("--model", default=None)
    args = p.parse_args()

    print(_format(run_mars_beat(args.instruction, provider=args.provider, model=args.model)))
