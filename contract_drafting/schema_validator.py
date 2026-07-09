"""
schema_validator.py -- JSON Schema validation powered by Concerto-generated schemas.

Loads schema.json (generated build-time from model.cto by concerto-helper.js)
and validates template data dicts at runtime. No Node.js subprocess at runtime.
"""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from datetime import date as _date
from pathlib import Path

from jsonschema import Draft7Validator

log = logging.getLogger(__name__)

_TEMPLATES_BASE = Path(__file__).resolve().parent.parent / "data" / "templates" / "cicero"

# Cache loaded schemas to avoid re-reading from disk on every draft call
_schema_cache: dict[str, dict] = {}
_policy_cache: dict[str, dict] = {}


def _load_schema(template_name: str) -> dict:
    """Load and cache the JSON Schema for a template."""
    if template_name in _schema_cache:
        return _schema_cache[template_name]

    schema_path = _TEMPLATES_BASE / template_name / "schema.json"
    if not schema_path.exists():
        raise FileNotFoundError(f"Schema not found: {schema_path}")

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    _schema_cache[template_name] = schema
    return schema


def load_abstain_policies(template_name: str = "nda-mutual") -> dict:
    """Canonical loader for the generated per-field abstain policy map ({field: {sentinel,
    rawField, enum, instruction, representable}}). Returns {} (SOFT) if the template has no
    abstain-policy.json -- so _strip_hatch is a no-op for non-hatch templates -- but RAISES
    if the file exists yet is malformed (a real codegen drift, fail loud). Callers that
    REQUIRE a policy (the abstain system prompt) fail closed on a missing FIELD, not here."""
    if template_name in _policy_cache:
        return _policy_cache[template_name]
    path = _TEMPLATES_BASE / template_name / "abstain-policy.json"
    if not path.exists():
        _policy_cache[template_name] = {}
        return {}
    try:
        policies = json.loads(path.read_text(encoding="utf-8"))["policies"]
        if not isinstance(policies, dict):
            raise ValueError("'policies' is not an object")
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        raise RuntimeError(
            f"abstain-policy.json malformed at {path}: {e}. Run `npm run generate`."
        ) from e
    _policy_cache[template_name] = policies
    return policies


def enum_display_fields(template_name: str = "nda-mutual") -> dict[str, str]:
    """Single source for the field -> enum-name map of the enum-backed fields whose
    display<->identifier surface forms must be normalized (e.g. {"disclosingEntityType":
    "EntityType", "receivingEntityType": "EntityType", "disputeForum": "DisputeForum"}).

    DERIVED from the generated abstain-policy.json (which carries each @Abstainable
    field's enum name straight from the .cto) instead of being hand-duplicated in
    demo_mars_beat / gauntlet / demo_offline (M3). governingLaw is EXCLUDED: it is
    normalized via its dedicated jurisdiction map, not the general enum-display map.
    Templates without an abstain-policy.json yield {} (nothing to normalize)."""
    return {
        field: policy["enum"]
        for field, policy in load_abstain_policies(template_name).items()
        if isinstance(policy, dict) and policy.get("enum") and field != "governingLaw"
    }


def _get_concept_schema(schema: dict) -> dict:
    """Extract the @template concept schema from definitions."""
    definitions = schema.get("definitions", {})

    # Find the concept with @template decorator
    for key, defn in definitions.items():
        if defn.get("$decorators", {}).get("template") is not None:
            return {
                "$schema": schema.get("$schema", "http://json-schema.org/draft-07/schema#"),
                **defn,
                "definitions": definitions,
            }

    # Fallback: use the first definition
    if definitions:
        key = next(iter(definitions))
        return {
            "$schema": schema.get("$schema"),
            **definitions[key],
            "definitions": definitions,
        }

    raise KeyError("No concept found in schema definitions")


# --- Semantic checks beyond the JSON Schema ---------------------------------
# The schema's date "pattern" is purely STRUCTURAL (it passes 2026-02-30,
# 2026-13-45, 0000-00-00) and integer fields carry no range, so a model can emit
# absurd-but-well-typed values (the Gauntlet recorded survivalYears=9999). These
# add real calendar-date + sane-range checks.

# Sane per-field integer bounds (inclusive). Pending native Concerto range
# validators in model.cto (TODOS.md); enforced here at runtime meanwhile.
_INTEGER_BOUNDS: dict[str, dict[str, tuple[int, int]]] = {
    "nda-mutual": {
        "termMonths": (1, 120),
        "noticeDays": (0, 365),
        "survivalYears": (0, 50),
        "nonCompeteMonths": (0, 60),
        "nonSolicitationMonths": (0, 60),
    },
}


def _date_fields(concept_schema: dict) -> list[str]:
    """Property names whose schema pattern is the YYYY-MM-DD structural regex."""
    out = []
    for name, spec in (concept_schema.get("properties") or {}).items():
        pat = spec.get("pattern") if isinstance(spec, dict) else None
        if isinstance(pat, str) and r"\d{4}" in pat and r"\d{2}" in pat:
            out.append(name)
    return out


def _check_calendar_dates(data: dict, concept_schema: dict) -> list[str]:
    """The schema regex is structural only; verify date-pattern fields are REAL
    calendar dates (rejects 2026-02-30, 2026-13-45, 0000-00-00)."""
    errs = []
    for f in _date_fields(concept_schema):
        v = data.get(f)
        if isinstance(v, str) and len(v) == 10 and v[4:5] == "-" and v[7:8] == "-":
            try:
                _date(int(v[0:4]), int(v[5:7]), int(v[8:10]))
            except (ValueError, TypeError):
                errs.append(f"Invalid {f}: '{v}' is not a real calendar date.")
    return errs


def _check_integer_bounds(data: dict, template_name: str) -> list[str]:
    """Reject negative/zero/absurd integer values the type-only schema allows."""
    errs = []
    for f, (lo, hi) in _INTEGER_BOUNDS.get(template_name, {}).items():
        v = data.get(f)
        # accept int OR float: a whole-valued float (9999.0) passes Draft7 "integer"
        # AND would skip an int-only bounds check, so it must be range-checked here too.
        if isinstance(v, (int, float)) and not isinstance(v, bool) and (v < lo or v > hi):
            errs.append(f"{f} out of range: {v} (expected {lo}..{hi}).")
    return errs


# --- Schema vintages for the pre-hatch (baseline) schema builder -------------
# _strip_hatch is VERSIONED (audit plan N-E):
#
#   "v1-leaky" (the pinned DEFAULT) -- the 2026-06 recording condition, byte-exact.
#     Strips the <field>Raw companions + abstain sentinels from the TOP-LEVEL
#     properties (and any $ref'd enum definition), but leaves the duplicated concept
#     blob under definitions['...NDAData'] untouched -- its four *Raw fields survive
#     in the serialized baseline prompt (the disclosed "definitions-block leak").
#     The four committed hard-suite cassettes were recorded against these bytes and
#     the replay key sha256-hashes the prompt (which embeds the serialized schema),
#     so this vintage MUST keep producing byte-identical output or every committed
#     baseline replay fails closed. Do not "fix" it in place; use v2-clean instead.
#
#   "v2-clean" -- the corrected builder for NEW recordings (e.g. the gauntlet's
#     raw_clean condition): additionally deep-strips the *Raw fields and abstain
#     sentinels from EVERY nested definitions copy, so the baseline prompt carries
#     no trace of the hatch vocabulary.
#
# The active vintage is ambient (module-level, default v1-leaky) so callers that
# thread through demo_mars_beat.build_prompt -- whose signature is replay-keyed and
# cannot grow a vintage parameter without churn -- can select v2 via the
# schema_vintage() context manager. tests/test_schema_vintage.py pins the v1 bytes.
SCHEMA_VINTAGE_V1_LEAKY = "v1-leaky"
SCHEMA_VINTAGE_V2_CLEAN = "v2-clean"
SCHEMA_VINTAGES = (SCHEMA_VINTAGE_V1_LEAKY, SCHEMA_VINTAGE_V2_CLEAN)

_active_schema_vintage = SCHEMA_VINTAGE_V1_LEAKY


def active_schema_vintage() -> str:
    """The ambient _strip_hatch vintage (default: v1-leaky, the committed-cassette pin)."""
    return _active_schema_vintage


@contextmanager
def schema_vintage(vintage: str):
    """Temporarily select the _strip_hatch vintage for everything on this thread that
    builds a pre-hatch schema (demo_mars_beat.build_prompt/_field_schema and the oracle's
    validate_template_data(with_abstain=False)). Restores the previous vintage on exit,
    exception or not. The clean way for NEW eval conditions to opt into v2-clean without
    touching the replay-keyed v1 path."""
    global _active_schema_vintage
    if vintage not in SCHEMA_VINTAGES:
        raise ValueError(f"unknown schema vintage {vintage!r}; expected one of {SCHEMA_VINTAGES}")
    prev = _active_schema_vintage
    _active_schema_vintage = vintage
    try:
        yield
    finally:
        _active_schema_vintage = prev


def _deep_strip_hatch(node, raw_fields: set[str], sentinels: set[str]) -> None:
    """v2-clean's extra pass: remove the *Raw companion properties and abstain-sentinel
    enum values from EVERY nested dict/list (i.e. all definitions copies), in place."""
    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict):
            for rf in raw_fields:
                props.pop(rf, None)
        req = node.get("required")
        if isinstance(req, list):
            node["required"] = [r for r in req if r not in raw_fields]
        enum = node.get("enum")
        if isinstance(enum, list):
            node["enum"] = [v for v in enum if v not in sentinels]
        for v in node.values():
            _deep_strip_hatch(v, raw_fields, sentinels)
    elif isinstance(node, list):
        for v in node:
            _deep_strip_hatch(v, raw_fields, sentinels)


def _strip_hatch(concept_schema: dict, template_name: str = "nda-mutual",
                 vintage: str | None = None) -> dict:
    """Pre-hatch variant of a concept schema: for EVERY @Abstainable field, drop its
    <field>Raw companion and remove its sentinel from its enum. Used to validate the
    Gauntlet's baseline (no-hatch) arms against the schema they were actually prompted with
    -- so an extra hatch field or a sentinel value is judged by the no-hatch schema, not
    silently allowed by the full one. Field-parametric (governingLaw, entityType,
    disputeForum, ...) via the generated abstain policy; a no-op for non-hatch templates.

    vintage=None uses the ambient vintage (see schema_vintage; default v1-leaky).
    v1-leaky strips top-level properties/enums only and is byte-pinned to the committed
    2026-06 cassettes; v2-clean additionally deep-strips the *Raw fields + sentinels
    from every nested definitions copy (see the vintage comment block above)."""
    import copy
    vintage = vintage or _active_schema_vintage
    if vintage not in SCHEMA_VINTAGES:
        raise ValueError(f"unknown schema vintage {vintage!r}; expected one of {SCHEMA_VINTAGES}")
    s = copy.deepcopy(concept_schema)
    policies = load_abstain_policies(template_name)
    props = s.get("properties") or {}
    defs = s.get("definitions") or {}
    for field, policy in policies.items():
        sentinel = policy.get("sentinel")
        raw = policy.get("rawField")
        if raw:
            props.pop(raw, None)
        spec = props.get(field) or {}
        if isinstance(spec.get("enum"), list):
            spec["enum"] = [v for v in spec["enum"] if v != sentinel]
        ref = spec.get("$ref")
        if isinstance(ref, str):
            target = defs.get(ref.split("/")[-1])
            if isinstance(target, dict) and isinstance(target.get("enum"), list):
                target["enum"] = [v for v in target["enum"] if v != sentinel]
    if vintage == SCHEMA_VINTAGE_V2_CLEAN:
        raw_fields = {p.get("rawField") for p in policies.values()
                      if isinstance(p, dict) and p.get("rawField")}
        sentinels = {p.get("sentinel") for p in policies.values()
                     if isinstance(p, dict) and p.get("sentinel")}
        _deep_strip_hatch(s, raw_fields, sentinels)
    return s


def validate_template_data(
    data: dict,
    *,
    template_name: str = "nda-mutual",
    with_abstain: bool = True,
) -> list[str]:
    """Validate a data dict against the Concerto-generated JSON Schema.

    Returns a list of human-readable error messages (empty = valid).

    with_abstain=False validates against the pre-hatch schema variant (no OTHER sentinel,
    no governingLawRaw) -- for the Gauntlet's baseline arms, which were prompted without
    the hatch, so they must be judged against that schema, not the full post-hatch one.
    """
    try:
        schema = _load_schema(template_name)
    except FileNotFoundError as e:
        return [str(e)]

    try:
        concept_schema = _get_concept_schema(schema)
    except KeyError as e:
        return [str(e)]

    if not with_abstain:
        concept_schema = _strip_hatch(concept_schema, template_name)

    validator = Draft7Validator(concept_schema)
    errors: list[str] = []

    for error in sorted(validator.iter_errors(data), key=lambda e: list(e.path)):
        field = ".".join(str(p) for p in error.path) or "(root)"
        if error.validator == "enum":
            errors.append(
                f"Invalid {field}: '{error.instance}'. Must be one of the allowed values."
            )
        elif error.validator == "minLength":
            errors.append(f"{field} must not be empty.")
        elif error.validator == "pattern":
            errors.append(
                f"Invalid {field} format: '{error.instance}'. Expected YYYY-MM-DD."
            )
        elif error.validator == "type":
            expected = error.schema.get("type", "?")
            errors.append(
                f"{field} must be of type {expected}, got {type(error.instance).__name__}."
            )
        elif error.validator == "required":
            errors.append(f"Missing required field: {error.message}")
        else:
            errors.append(f"{field}: {error.message}")

    return errors


def validate_semantics(data: dict, *, template_name: str = "nda-mutual") -> list[str]:
    """Product-safety checks BEYOND the JSON Schema: real calendar dates + sane
    integer ranges. Returns human-readable errors (empty = ok).

    Kept SEPARATE from validate_template_data on purpose: the benchmark oracle
    measures *pure schema validity*, and a schema-valid-but-absurd value (e.g.
    survivalYears=9999, which the type system allows) is a SILENT SUBSTITUTION the
    eval must count as wrong_sub, not a schema error. Drafting paths call BOTH.
    """
    try:
        concept_schema = _get_concept_schema(_load_schema(template_name))
    except (FileNotFoundError, KeyError):
        concept_schema = {}
    return _check_calendar_dates(data, concept_schema) + _check_integer_bounds(data, template_name)


def governing_law_enum(template_name: str = "nda-mutual") -> set[str]:
    """The allowed governingLaw values from the template's JSON Schema (resolving a
    $ref to an enum). Empty set if governingLaw is unconstrained (free string).

    This is the source of truth for the intent gate's representability check, covering
    BOTH the nda-mutual Jurisdiction enum ($ref -> identifier values) AND the inline
    string enums on the other templates (consulting, joint-venture, ...), which have
    no jurisdictions.map.json but DO constrain governingLaw.
    """
    try:
        schema = _load_schema(template_name)
        concept = _get_concept_schema(schema)
    except (FileNotFoundError, KeyError):
        return set()
    gl = (concept.get("properties") or {}).get("governingLaw")
    if not isinstance(gl, dict):
        return set()
    if isinstance(gl.get("enum"), list):
        return set(gl["enum"])
    ref = gl.get("$ref")
    if isinstance(ref, str):
        target = (schema.get("definitions") or {}).get(ref.split("/")[-1], {})
        if isinstance(target.get("enum"), list):
            return set(target["enum"])
    return set()


def clear_cache() -> None:
    """Clear the schema + abstain-policy caches (useful in tests)."""
    _schema_cache.clear()
    _policy_cache.clear()
