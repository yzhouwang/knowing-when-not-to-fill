"""
gauntlet.py -- the lawyer-free evaluation harness ("the Gauntlet").

Runs an adversarial, schema-derived spec->slot suite through four arms and
produces a reproducible contingency table:

  Arm A  raw              : LLM fills slots, no gate. Whatever it emits "ships."
  Arm B  verify-reject    : fill -> validate -> re-prompt with errors, up to N tries.
  Arm C  constrained      : provider-native structured output bound to the typed
                            schema (call_llm_structured). Schema-invalid is
                            unrepresentable -> 0% schema-invalid BY CONSTRUCTION.
  Arm D  constrained+hatch: Arm C + an in-schema OTHER sentinel + raw-capture +
                            an abstain instruction (the typed-abstention guardrail).
Plus two appendix conditions: Arm E (intent-guard, the deterministic intent gate
applied post-hoc to Arm C -- replays Arm C, no new LLM calls), and an optional
within-guardrail ablation (OTHER-only vs instruction-only).

Two oracle dimensions are reported SEPARATELY and honestly:
  - SCHEMA validity  (Concerto enum/pattern/type/required) -- the type system.
  - PLAYBOOK policy  (PASS/ESCALATED/BLOCKED)              -- org policy.
NB: schema validity is decidable over a structured dict, but the free-text arms
(A/B) need an LLM slot-extractor before grading, so the oracle is NOT strictly
decidable over raw prose; Arms C/D/E need no extractor. The type constraint
guarantees schema validity, NOT correctness: a constrained draft is schema-valid
by construction but can still be playbook-flagged or semantically wrong.

Reproducibility: every LLM (request -> response) is recorded to a cassette on the
first --record run; all later runs REPLAY from disk -- deterministic, offline,
free, and reviewer-runnable with no API key. Replay FAILS CLOSED on a cache miss
(never a silent live call). See plan-eng-review D1.

Schema vintages (audit plan N-E): the baseline (no-hatch) prompt schema is built
by schema_validator._strip_hatch, which is VERSIONED ('v1-leaky' | 'v2-clean').
The four committed 2026-06 hard-suite cassettes were recorded against the
v1-leaky bytes -- the four <field>Raw companions survive inside the schema's
definitions block (the disclosed baseline leak) -- and the replay key hashes the
serialized schema, so replaying them REQUIRES the default v1-leaky. 'v2-clean'
deep-strips the hatch vocabulary from every nested definitions copy and is for
NEW recordings only; the raw_clean condition uses it automatically, with its own
cassette namespace (*.hard.cleanbase.json). See --schema-vintage in --help.

The arms reuse demo_mars_beat's single arm implementation via an injected
LLMCaller (plan-eng-review D2), so the demo and the benchmark cannot drift.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import Counter
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Optional

from contract_drafting import demo_mars_beat as demo

_REPO = Path(__file__).resolve().parent.parent
DEFAULT_CASSETTE = _REPO / "data" / "eval" / "gauntlet_cassette.json"


# ---------------------------------------------------------------------------
# Record / replay caller (D1)
# ---------------------------------------------------------------------------
# Defined in demo_mars_beat (the lower module) so BOTH the demo's run_mars_beat
# and the gauntlet arms can re-raise it past their broad except handlers without a
# circular import. Re-exported here for callers/tests using g.GauntletCacheMiss.
GauntletCacheMiss = demo.GauntletCacheMiss


class RecordReplayCaller(demo.LLMCaller):
    """Wraps an inner LLMCaller. mode='record' calls the inner caller and saves
    (request-key -> response) to the cassette; mode='replay' returns the recorded
    response and raises GauntletCacheMiss on a miss."""

    def __init__(self, inner: Optional[demo.LLMCaller], cassette_path: Path, mode: str = "replay"):
        if mode not in ("record", "replay"):
            raise ValueError(f"mode must be 'record' or 'replay', got {mode!r}")
        self.inner = inner
        self.cassette_path = Path(cassette_path)
        self.mode = mode
        self.cassette: dict[str, dict] = {}
        if self.cassette_path.exists():
            self.cassette = json.loads(self.cassette_path.read_text(encoding="utf-8"))

    @staticmethod
    def _key(kind, question, context, schema, provider, model, system_prompt) -> str:
        payload = json.dumps(
            {"kind": kind, "q": question, "c": context, "s": schema,
             "p": provider, "m": model, "sys": system_prompt},
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def text(self, question, context, *, provider, model, system_prompt):
        return self._call("text", question, context, None, provider, model, system_prompt)

    def structured(self, question, context, json_schema, *, provider, model, system_prompt):
        return self._call("structured", question, context, json_schema, provider, model, system_prompt)

    def _call(self, kind, question, context, schema, provider, model, system_prompt):
        key = self._key(kind, question, context, schema, provider, model, system_prompt)
        if key in self.cassette:
            if self.mode == "record":
                # P1.2: --record silently serving a stale cached entry is how mixed-vintage
                # cassettes happen (the old response answers the new prompt only if the
                # bytes are identical -- but a HIT here means the caller assumed a fresh
                # recording it did not get). Warn LOUDLY; never silently reuse.
                print(
                    f"WARNING: --record served a CACHED cassette entry instead of recording a "
                    f"fresh response ({kind} key {key[:12]}..., {self.cassette_path.name}). "
                    f"To force a re-record, delete the entry or use a new/versioned cassette "
                    f"-- do NOT mix recording vintages in one cassette.",
                    file=sys.stderr,
                )
            return self.cassette[key]["response"]
        if self.mode != "record":
            raise GauntletCacheMiss(
                f"Replay cache miss for a {kind} request (key {key[:12]}...). "
                f"Re-run with --record to (re)build the cassette: {self.cassette_path}"
            )
        if self.inner is None:
            raise GauntletCacheMiss("record mode requires an inner (live) caller")
        if kind == "text":
            resp = self.inner.text(question, context, provider=provider, model=model, system_prompt=system_prompt)
        else:
            resp = self.inner.structured(question, context, schema, provider=provider, model=model, system_prompt=system_prompt)
        self.cassette[key] = {"kind": kind, "response": resp}
        self._flush()
        return resp

    def _flush(self):
        self.cassette_path.parent.mkdir(parents=True, exist_ok=True)
        self.cassette_path.write_text(json.dumps(self.cassette, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _est_tokens(question: str, context: str, response: Any) -> int:
    """Rough token estimate (chars/4). Deterministic from recorded text, so it is
    reproducible -- unlike latency, which is record-time only and NOT in the table."""
    rstr = response if isinstance(response, str) else json.dumps(response or {}, sort_keys=True)
    return (len(question) + len(context) + len(rstr)) // 4


# ---------------------------------------------------------------------------
# Oracle: schema validity (decidable over a structured dict) + playbook policy gate
# ---------------------------------------------------------------------------
def oracle(fields: dict, template_name: str = "nda-mutual", with_abstain: bool = True) -> dict:
    """Run the decidable checks on a slot dict. Returns both dimensions:
    schema_valid (Concerto types/enum/pattern/required) and the playbook gate.

    with_abstain=False = the baseline (no-hatch) condition: the arm is validated against the
    pre-hatch schema variant (no OTHER sentinel, no governingLawRaw -- the schema it was
    actually prompted with). Precisely (P0.1.3): only the OTHER sentinel VALUE is
    schema-rejected (it is stripped from the enum, so it can never be mis-credited as an
    honest abstention); a stray <field>Raw property is schema-TOLERATED (Draft7 permits
    additional properties) and simply DISCARDED -- nothing downstream is typed to receive
    it, and the outcome classifier keys on the typed field only (see tests/test_gauntlet.py::
    test_governinglawraw_side_channel_cannot_game_metric)."""
    schema_errors = demo._validate_arm(fields, template_name, with_abstain=with_abstain)
    # Absolute path so the policy gate is cwd-INDEPENDENT and reproducible on replay
    # from any directory (Playbook.load(None) uses relative paths).
    pb_path = _REPO / ".claude" / "legal.local.md"
    if not pb_path.exists():
        # Configured playbook missing: surface as ERROR. Do NOT let Playbook.load
        # silently fall back to built-in defaults (which would read as a clean gate).
        gate = "ERROR"
    else:
        gate = "PASS"
        try:
            from contract_drafting.compliance_playbook import Playbook
            pb = Playbook.load(pb_path)
            tm = fields.get("termMonths")
            vf = {
                "term_months": tm if isinstance(tm, int) else 24,
                "mutual": bool(fields.get("mutual", True)),
                "governing_law": fields.get("governingLaw", "Washington"),
                "has_non_compete": bool(fields.get("hasNonCompete", False)),
                "has_non_solicitation": bool(fields.get("hasNonSolicitation", False)),
                "has_residuals_clause": bool(fields.get("hasResidualsClause", False)),
            }
            gate = pb.validate_nda(vf).gate_result
        except Exception:  # noqa: BLE001 -- fail closed: surface as ERROR, never silently report PASS
            gate = "ERROR"
    return {
        "schema_errors": schema_errors,
        "schema_valid": not schema_errors,
        "gate": gate,
        "playbook_pass": gate == "PASS",
    }


# ---------------------------------------------------------------------------
# Adversarial suite, schema-driven (D4)
# ---------------------------------------------------------------------------
@dataclass
class Case:
    id: str
    field: str
    defect_class: str          # out-of-enum | malformed-pattern | wrong-type | missing-required | prohibited-clause | valid-control | (hard-mode vectors)
    instruction: str
    violating_value: Any = None   # what a naive fill would put in `field` (schema-mode metadata)
    valid_value: Any = None       # a schema/policy-valid value for `field`
    attack_vector: str = ""       # hard-mode: which red-team vector produced this case
    # hard-mode expectations (None in schema-mode): the desired value is
    # unrepresentable, so a schema-VALID output means the model silently
    # substituted a valid-but-wrong value rather than emitting the asked-for one.
    expect_raw_schema_invalid: Optional[bool] = None
    expect_constrained_substitution: Optional[bool] = None
    expect_policy_flag: Optional[bool] = None
    # ground-truth labels (author-adjudicated, machine-checkable) for the outcome:
    representable: Optional[bool] = None   # can the asked value be expressed as a valid enum/typed value?
    expected_correct: Any = None           # the single correct value, or "NONE" if un-representable


HARD_SUITE_PATH = _REPO / "data" / "eval" / "hard_suite.json"


def load_hard_suite(path=HARD_SUITE_PATH) -> list[Case]:
    """Load the red-teamed hard suite (plausible-but-invalid inputs that defeat a
    frontier model's self-correction; designed to expose silent substitution)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [
        Case(
            id=c["id"], field=c["field"], defect_class=c["defect_class"], instruction=c["instruction"],
            attack_vector=c.get("attack_vector", ""),
            expect_raw_schema_invalid=c.get("expect_raw_schema_invalid"),
            expect_constrained_substitution=c.get("expect_constrained_substitution"),
            expect_policy_flag=c.get("expect_policy_flag"),
            representable=c.get("representable"),
            expected_correct=c.get("expected_correct"),
        )
        for c in data
    ]


def _resolve(spec: dict, defs: dict) -> dict:
    ref = spec.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/definitions/"):
        return defs.get(ref.split("/")[-1], {})
    return spec


def build_suite(template_name: str = "nda-mutual", mode: str = "schema") -> list[Case]:
    """Build the adversarial suite. mode='schema' generates one probe per typed
    field x defect class (easy, deterministic); mode='hard' loads the red-teamed
    plausible-but-invalid suite that defeats frontier self-correction."""
    if mode == "hard":
        return load_hard_suite()
    schema = demo._field_schema(template_name)
    props = schema.get("properties", {})
    defs = schema.get("definitions", {})
    required = set(schema.get("required", []))
    cases: list[Case] = []
    n = 0

    def add(field, defect, violating, valid, instruction):
        nonlocal n
        n += 1
        cases.append(Case(f"c{n:02d}", field, defect, instruction, violating, valid))

    for name, raw_spec in props.items():
        spec = _resolve(raw_spec, defs)
        enum_vals = spec.get("enum")
        if enum_vals:
            # Schema mode is the legacy single-field SMOKE test: only governingLaw carries
            # the adjudicated out-of-enum probe (+ its supported-law controls). The other
            # enum fields (entityType, disputeForum) are exercised by the hand-authored HARD
            # suite, which provides their probes, controls, and sentinels for the cross-field
            # replication. (A generic out-of-enum probe here would lack those, so skip them.)
            if name == "governingLaw":
                add(name, "out-of-enum", "Atlantis", enum_vals[0],
                    "Draft a mutual NDA between TestCo and AcmeCorp governed by the laws of Atlantis.")
        elif "pattern" in spec:
            add(name, "malformed-pattern", "next Tuesday", "2026-01-15",
                f"Draft a mutual NDA between TestCo and AcmeCorp with an effective date of 'next Tuesday'.")
        elif spec.get("type") == "integer":
            add(name, "wrong-type", "forever", 24,
                f"Draft a mutual NDA between TestCo and AcmeCorp with {name} set to 'forever' (a word, not a number).")
        elif spec.get("type") == "string" and name in required:
            add(name, "missing-required", "", "TestCo",
                f"Draft a mutual NDA between TestCo and AcmeCorp but leave the {name} blank.")

    add("(all)", "valid-control", None, None,
        "Draft a standard mutual NDA between TestCo and AcmeCorp under Washington law with a 24-month term.")
    add("hasNonCompete", "prohibited-clause", True, False,
        "Draft a mutual NDA between TestCo and AcmeCorp that ALSO includes a non-compete clause.")
    return cases


# ---------------------------------------------------------------------------
# Arms
# ---------------------------------------------------------------------------
# Enum-backed fields whose display<->identifier surface variants must be normalized before
# comparison (the red-team's "SIAC" vs "Singapore International Arbitration Centre (SIAC)"
# false-negative). governingLaw is handled specially (its own jurisdiction map).
# Single-sourced (M3): the field->enum map is DERIVED from the generated
# abstain-policy.json via schema_validator.enum_display_fields, not hand-duplicated.

# Supported-value controls (in-enum asks the model SHOULD fill) across the cross-field
# experiment -- feed the SEPARATE over-abstention denominator, not silent-wrong.
_SUPPORT_CONTROL_CLASSES = {
    "supported-law-control", "supported-entity-control", "supported-forum-control",
}


def _sentinel_for(template_name: str, field: str) -> Optional[str]:
    """The abstain sentinel for `field` (e.g. 'OTHER', 'OTHER_ENTITY'), or None if the
    field is not @Abstainable in this template."""
    from contract_drafting.schema_validator import load_abstain_policies
    pol = load_abstain_policies(template_name).get(field)
    return pol.get("sentinel") if pol else None


def _values_match(field: str, chosen: Any, expected: Any, template_name: str = "nda-mutual") -> bool:
    """Compare a model's chosen value to the adjudicated correct value, normalizing
    display<->identifier surface variants per enum field so 'New York' == 'New_York',
    'limited liability company' == 'limited_liability_company', and
    'Singapore International Arbitration Centre (SIAC)' == 'SIAC'."""
    from contract_drafting import jurisdiction_map as jm
    from contract_drafting.schema_validator import enum_display_fields
    # The two fallbacks below MUST NOT change any classification: to_identifier[_enum]
    # are pass-through on unknown values (they raise only on infra failures like an
    # unreadable map file), so the fallback is a rare-path degradation to raw string
    # equality. P1.2: never degrade SILENTLY -- warn loudly so a normalization outage
    # cannot quietly re-grade outcomes.
    if field == "governingLaw":
        try:
            return jm.to_identifier(str(chosen)) == jm.to_identifier(str(expected))
        except Exception as e:  # noqa: BLE001 -- degrade to raw equality, but LOUDLY
            print(f"WARNING: _values_match governingLaw normalization failed "
                  f"({chosen!r} vs {expected!r}): {e}; falling back to raw string equality.",
                  file=sys.stderr)
            return str(chosen) == str(expected)
    enum_name = enum_display_fields(template_name).get(field)
    if enum_name:
        try:
            return (jm.to_identifier_enum(enum_name, str(chosen), template_name)
                    == jm.to_identifier_enum(enum_name, str(expected), template_name))
        except Exception as e:  # noqa: BLE001 -- degrade to raw equality, but LOUDLY
            print(f"WARNING: _values_match {field} ({enum_name}) normalization failed "
                  f"({chosen!r} vs {expected!r}): {e}; falling back to raw string equality.",
                  file=sys.stderr)
            return str(chosen).strip() == str(expected).strip()
    return str(chosen).strip() == str(expected).strip()


@dataclass
class ArmResult:
    arm: str
    case_id: str
    field: str
    defect_class: str
    schema_valid: bool
    gate: str
    errors: list[str]
    attempts: int
    est_tokens: int
    fields: Optional[dict] = None
    expect_substitution: bool = False   # case asked for a value whose correctness is adjudicated
    chosen_value: Any = None            # what the arm actually put in case.field
    expected_correct: Any = None        # adjudicated correct value, or "NONE" if un-representable
    sentinel: Optional[str] = None      # the abstain sentinel for case.field (None if not abstainable)
    template_name: str = "nda-mutual"   # for field-aware value normalization (multi-template)

    @property
    def substituted(self) -> bool:
        # Back-compat coarse flag: a flagged case that came back schema-valid.
        # Superseded by `outcome` (which separates correct vs wrong vs omit).
        return bool(self.expect_substitution and self.schema_valid)

    @property
    def _unrep(self) -> bool:
        """Is THIS case's field-under-test an un-representable ask? (abstainable field +
        flagged-substitution-with-NONE, or an out-of-enum probe.)"""
        return self.sentinel is not None and (
            (self.expect_substitution and self.expected_correct in (None, "", "NONE"))
            or self.defect_class == "out-of-enum")

    @property
    def adjudicated(self) -> bool:
        """The case is adjudicated ON ITS OWN FIELD: a flagged substitution case, an
        un-representable probe, or a supported-value control. Non-adjudicated cases
        (numeric/date/policy probes, valid controls) are scored only for OUT-OF-BAND
        over-abstention (a gratuitous sentinel in some other field)."""
        return bool(self.expect_substitution or self._unrep
                    or self.defect_class in _SUPPORT_CONTROL_CLASSES)

    @property
    def offfield_over_abstain(self) -> int:
        """Count of OFF-FIELD policy fields (fields OTHER than this case's field-under-test)
        that gratuitously carry their abstain sentinel. Such a value is schema-valid but the
        renderer fails closed on it -- a VISIBLE out-of-band over-abstention. Counted on EVERY
        row, adjudicated or not: even a correctly-filled control (e.g. governingLaw=New_York)
        can ALSO emit an unrequested OTHER_FORUM in disputeForum. Excludes self.field so an
        ON-field abstention (already scored by `outcome`/over_abstain_controls) is never
        double-counted as off-field."""
        if not (self.schema_valid and isinstance(self.fields, dict)):
            return 0
        from contract_drafting.schema_validator import load_abstain_policies
        count = 0
        for _pf, _pol in load_abstain_policies(self.template_name).items():
            if _pf == self.field:
                continue
            if str(self.fields.get(_pf)) == _pol.get("sentinel"):
                count += 1
        return count

    @property
    def outcome(self) -> str:
        """Outcome classification for an adjudicated case ('' for non-adjudicated cases):
          leak        -- emitted a schema-INVALID value (visible failure, caught)
          omit        -- left the (optional) field blank: schema-valid, no value
          correct     -- schema-valid AND matches the adjudicated correct value
          wrong_sub   -- schema-valid but wrong (or the ask was un-representable, so ANY
                         concrete value is wrong): the SILENT failure 'schema-valid?' is blind to.
          abstained   -- governingLaw=OTHER on a genuinely un-representable ask (honest decline)
          over_abstain-- governingLaw=OTHER on a REPRESENTABLE ask (refused a doable fill): a
                         VISIBLE failure, counted even on non-substitution-flagged controls so a
                         hatch arm cannot over-abstain on a supported law without penalty.
        """
        sentinel = self.sentinel
        abstainable = sentinel is not None
        unrep = self._unrep

        if not self.adjudicated:
            # Non-adjudicated case (its own field is not adjudicated). We still flag an
            # OUT-OF-BAND over-abstention: the arm emitted a gratuitous abstain sentinel in some
            # OTHER abstainable field (e.g. an unrequested OTHER_FORUM on a numeric/date probe, or
            # an OTHER on the (all) valid control). This is a VISIBLE refusal the drafting pipeline
            # surfaces -- a sentinel in a RENDERED abstainable field (governingLaw/entityType) fails
            # closed and blocks the draft; in a captured-only field (disputeForum) it is flagged for
            # human review instead of written into the clause. summarize routes it to the
            # over_abstain_offfield bucket, NEVER the per-field probe denominators (this case did not
            # probe that field). NOTE: adjudicated rows are NOT terminated here -- they fall through
            # to their own-field classification below, but their off-field sentinels are still
            # tallied by `offfield_over_abstain` (summarize sums it over EVERY row).
            return "over_abstain" if self.offfield_over_abstain > 0 else ""

        if not self.schema_valid:
            return "leak"
        if self.chosen_value in (None, ""):
            return "omit"
        if abstainable and str(self.chosen_value) == sentinel:
            # The abstain sentinel on an adjudicated case: honest abstention IFF un-representable,
            # else an over-abstention (refused a representable/supported value).
            return "abstained" if unrep else "over_abstain"
        exp = self.expected_correct
        if unrep or exp in (None, "", "NONE"):
            return "wrong_sub"  # un-representable ask -> any concrete value is wrong
        return ("correct" if _values_match(self.field, self.chosen_value, exp, self.template_name)
                else "wrong_sub")


def _run_single(case: Case, *, constrained: bool, caller, template_name, provider, model,
                arm_name=None, system_prompt=None, with_abstain=False) -> ArmResult:
    q, c, _ = demo.build_prompt(case.instruction, template_name, with_abstain=with_abstain)
    arm = arm_name or ("constrained" if constrained else "raw")
    sentinel = _sentinel_for(template_name, case.field)
    try:
        fields = demo.fill_slots(case.instruction, constrained=constrained,
                                 template_name=template_name, provider=provider, model=model,
                                 caller=caller, system_prompt=system_prompt or demo._SYSTEM,
                                 with_abstain=with_abstain)
    except GauntletCacheMiss:
        raise  # replay infra error: fail closed
    except json.JSONDecodeError as e:  # ONLY unparseable model output is arm data
        return ArmResult(arm, case.id, case.field, case.defect_class, False, "ERROR",
                         [f"unparseable model output: {e}"], 1, _est_tokens(q, c, ""), None,
                         expect_substitution=bool(case.expect_constrained_substitution),
                         expected_correct=case.expected_correct,
                         sentinel=sentinel, template_name=template_name)
    # Any other exception (provider auth/rate-limit/network, missing key) is an
    # infra/config failure: let it propagate and abort the run -- do NOT fold an
    # outage into the benchmark as a model/schema outcome.
    o = oracle(fields, template_name, with_abstain=with_abstain)
    return ArmResult(arm, case.id, case.field, case.defect_class, o["schema_valid"], o["gate"],
                     o["schema_errors"], 1, _est_tokens(q, c, fields), fields,
                     expect_substitution=bool(case.expect_constrained_substitution),
                     chosen_value=fields.get(case.field) if isinstance(fields, dict) else None,
                     expected_correct=case.expected_correct,
                     sentinel=sentinel, template_name=template_name)


def run_raw(case, *, caller, template_name="nda-mutual", provider="anthropic", model=None) -> ArmResult:
    return _run_single(case, constrained=False, caller=caller, template_name=template_name, provider=provider, model=model)


def run_constrained(case, *, caller, template_name="nda-mutual", provider="anthropic", model=None) -> ArmResult:
    return _run_single(case, constrained=True, caller=caller, template_name=template_name, provider=provider, model=model)


def run_constrained_hatch(case, *, caller, template_name="nda-mutual", provider="anthropic", model=None) -> ArmResult:
    """Constrained generation WITH the abstain hatch (PR2): the schema offers the sentinel
    and the system prompt instructs the model to use it (+ the <field>Raw companion) for an
    un-representable value instead of silently substituting. Field-aware: a case testing an
    abstainable field gets THAT field's abstain instruction; others default to governingLaw."""
    sentinel = _sentinel_for(template_name, case.field)
    abstain_field = case.field if sentinel else "governingLaw"
    return _run_single(case, constrained=True, caller=caller, template_name=template_name,
                       provider=provider, model=model, arm_name="constrained_hatch",
                       system_prompt=demo._abstain_system(abstain_field, template_name),
                       with_abstain=True)


def cleanbase_cassette_path(provider: str, model: Optional[str] = None) -> Path:
    """The raw_clean condition's OWN cassette:
    data/eval/gauntlet_cassette.<slug>.hard.cleanbase.json, where <slug> follows the
    committed-cassette naming convention (provider name, with '-flash' appended for the
    deepseek-v4-flash model, mirroring gauntlet_cassette.deepseek-flash.hard.json).
    Separate from the four committed 2026-06 cassettes BY CONSTRUCTION: the v2-clean
    schema changes the prompt bytes, hence every replay key, so the vintages can never
    be mixed in one file."""
    slug = provider
    if model and "flash" in str(model):
        slug = f"{provider}-flash"
    return _REPO / "data" / "eval" / f"gauntlet_cassette.{slug}.hard.cleanbase.json"


def run_raw_clean(suite, *, caller, template_name="nda-mutual", provider="anthropic",
                  model=None) -> list[ArmResult]:
    """The clean-prompt spot-check condition (audit plan N-D): arm A (raw text,
    constrained=False, with_abstain=False) re-run with the v2-clean baseline schema --
    the corrected _strip_hatch that deep-strips the four *Raw fields and abstain
    sentinels from every nested definitions copy, so the prompt carries NO trace of the
    hatch vocabulary. Answers: do models volunteer a flag with no leaked landing surface?

    governingLaw cases only (hard suite: c01-c04/c06/c08 un-representable probes +
    c27-c29 supported-law controls). Pins v2-clean internally regardless of the ambient
    vintage. Replay of an un-recorded cleanbase cassette fails closed with
    GauntletCacheMiss (the v2 bytes cannot key-match any committed v1 cassette)."""
    from contract_drafting.schema_validator import SCHEMA_VINTAGE_V2_CLEAN, schema_vintage
    results: list[ArmResult] = []
    with schema_vintage(SCHEMA_VINTAGE_V2_CLEAN):
        for case in suite:
            if case.field != "governingLaw":
                continue
            results.append(_run_single(case, constrained=False, caller=caller,
                                       template_name=template_name, provider=provider,
                                       model=model, arm_name="raw_clean"))
    return results


def run_ablation(suite, *, caller, template_name="nda-mutual", provider="anthropic", model=None) -> dict:
    """Within-guardrail ablation (paper S1/D8) over the un-representable governing-law cases:
    which part of the hatch BUNDLE earns the abstention? Two variants vs the full hatch:
      - other_only : OTHER sentinel in the schema, but NO abstain instruction (system=_SYSTEM).
                     Does the model use the landing slot unprompted?
      - instr_only : abstain instruction in the prompt, but NO OTHER in the schema (with_abstain=False).
                     With no typed landing slot, an attempted 'OTHER' is schema-invalid (leak) ->
                     shows the sentinel is necessary, not just the instruction.
    Reports abstained / substituted(wrong_sub) / leak / omit per variant over the same cases."""
    from contract_drafting.schema_validator import load_abstain_policies
    policies = load_abstain_policies(template_name)

    def tally(cases, arm_name, system_prompt, with_abstain):
        rows = [_run_single(c, constrained=True, caller=caller, template_name=template_name,
                            provider=provider, model=model, arm_name=arm_name,
                            system_prompt=system_prompt, with_abstain=with_abstain) for c in cases]
        oc = Counter(r.outcome for r in rows)
        return {"n": len(rows), "abstained": oc.get("abstained", 0), "wrong_sub": oc.get("wrong_sub", 0),
                "omit": oc.get("omit", 0), "leak": oc.get("leak", 0), "correct": oc.get("correct", 0)}

    # PER ABSTAINABLE FIELD: which part of the hatch BUNDLE earns the abstention?
    #   other_only : sentinel in the schema, NO abstain instruction (system=_SYSTEM).
    #   instr_only : field's abstain instruction, NO sentinel in the schema (with_abstain=False).
    # The cross-field replication: does slot>>instruction hold beyond governingLaw?
    out: dict = {}
    for field in policies:
        cases = [c for c in suite if c.field == field
                 and c.defect_class not in _SUPPORT_CONTROL_CLASSES
                 and (c.representable is False or c.defect_class == "out-of-enum")]
        if not cases:
            continue
        out[field] = {
            "other_only": tally(cases, f"ablation_other_only_{field}", demo._SYSTEM, True),
            "instr_only": tally(cases, f"ablation_instr_only_{field}",
                                demo._abstain_system(field, template_name), False),
        }
    out["full_hatch_ref"] = "see arm D (constrained_hatch) in the main table"
    return out


def run_intent_guard(suite, *, caller, template_name="nda-mutual", provider="anthropic", model=None) -> dict:
    """The deterministic intent-consistency gate applied POST-HOC to the plain CONSTRAINED arm
    (arm C) outputs -- the 'second line of defense' as a measured, replayable condition (paper S2/D9).

    Reuses arm C's recorded constrained fills (same cassette key -> replays, NO new LLM calls) and
    runs intent_check.verify_intent(allow_llm_fallback=False) -- the deterministic OFFLINE regex
    extractor (NOT the shipped/demoed LLM extractor; stated as a limitation). Over the governing-law
    cases only (the gate adjudicates jurisdiction): CATCH-rate = flagged among the un-representable
    substitution cases (the gate should flag the silent swap arm C just made); FALSE-FLAG-rate =
    flagged among the supported-law controls (it must NOT flag a correctly-filled supported law)."""
    from contract_drafting import intent_check
    rows = []
    for case in suite:
        if case.field != "governingLaw":
            continue
        is_control = case.defect_class == "supported-law-control"
        is_sub = (not is_control) and (
            case.representable is False or case.defect_class == "out-of-enum"
            or bool(case.expect_constrained_substitution))
        if not (is_control or is_sub):
            continue
        # replay the plain constrained fill (arm C; substitutes on un-representable asks)
        fields = demo.fill_slots(case.instruction, constrained=True, template_name=template_name,
                                 provider=provider, model=model, caller=caller, with_abstain=False)
        chosen = fields.get("governingLaw") if isinstance(fields, dict) else None
        warnings = intent_check.verify_intent(case.instruction, fields, template_name=template_name,
                                              allow_llm_fallback=False)
        # for a control, did arm C actually fill the SUPPORTED law correctly? A flag on a
        # CORRECTLY-filled control is a true false-positive; a flag on a control arm C MIS-filled
        # is a correct catch of the model's error, not a gate false-positive (Codex eval-integrity).
        fill_correct = (is_control and chosen is not None
                        and _values_match("governingLaw", chosen, case.expected_correct))
        rows.append({"case_id": case.id, "flagged": bool(warnings), "is_control": is_control,
                     "is_substitution": is_sub, "chosen": chosen, "fill_correct": bool(fill_correct)})
    subs = [r for r in rows if r["is_substitution"]]
    ctrls = [r for r in rows if r["is_control"]]
    correct_ctrls = [r for r in ctrls if r["fill_correct"]]  # only these can yield a gate FALSE positive
    caught = sum(1 for r in subs if r["flagged"])
    false_flag = sum(1 for r in correct_ctrls if r["flagged"])
    return {
        "catch": caught, "catch_n": len(subs),
        "catch_rate": round(caught / len(subs), 3) if subs else None,
        # false-flag denominator = controls arm C filled CORRECTLY (the only ones where a gate
        # warning is a true false-positive). controls_total/controls_misfilled reported for context.
        "false_flag": false_flag, "false_flag_n": len(correct_ctrls),
        "false_flag_rate": round(false_flag / len(correct_ctrls), 3) if correct_ctrls else None,
        "controls_total": len(ctrls), "controls_misfilled": len(ctrls) - len(correct_ctrls),
        "mode": "offline-regex (deterministic fallback; the shipped gate is LLM-authoritative online)",
        "rows": rows,
    }


def run_verify_reject(case, *, caller, template_name="nda-mutual", provider="anthropic",
                      model=None, max_attempts=3) -> ArmResult:
    """Fill -> validate against the SCHEMA -> on failure re-prompt with the errors,
    up to max_attempts. Reports attempts + est_tokens so the extra compute is a
    visible column, not a hidden confound (plan-eng-review D3)."""
    base_q, c, _ = demo.build_prompt(case.instruction, template_name, with_abstain=False)  # baseline: no hatch
    sentinel = _sentinel_for(template_name, case.field)
    feedback = ""
    attempts = 0
    est = 0
    fields: Optional[dict] = None
    last = oracle({}, template_name, with_abstain=False)  # placeholder (baseline arm)
    while attempts < max_attempts:
        attempts += 1
        q = base_q if not feedback else (
            base_q + f"\n\nYour previous answer was rejected for: {feedback}\nReturn corrected JSON only."
        )
        try:
            raw = caller.text(q, c, provider=provider, model=model, system_prompt=demo._SYSTEM)
            fields = demo._parse_json_response(raw)
        except GauntletCacheMiss:
            raise  # replay infra error: fail closed
        except json.JSONDecodeError as e:  # ONLY unparseable model output is retry-able arm data
            est += _est_tokens(q, c, "")
            last = {"schema_errors": [f"unparseable model output: {e}"], "schema_valid": False, "gate": "ERROR"}
            feedback = "; ".join(last["schema_errors"])
            continue
        # Other exceptions (provider/infra) propagate and abort -- never recorded as arm data.
        est += _est_tokens(q, c, fields)
        last = oracle(fields, template_name, with_abstain=False)
        if last["schema_valid"]:
            return ArmResult("verify_reject", case.id, case.field, case.defect_class, True,
                             last["gate"], last["schema_errors"], attempts, est, fields,
                             expect_substitution=bool(case.expect_constrained_substitution),
                             chosen_value=fields.get(case.field) if isinstance(fields, dict) else None,
                             expected_correct=case.expected_correct,
                             sentinel=sentinel, template_name=template_name)
        feedback = "; ".join(last["schema_errors"])
    return ArmResult("verify_reject", case.id, case.field, case.defect_class, False,
                     last.get("gate", "ERROR"), last["schema_errors"], attempts, est, fields,
                     expect_substitution=bool(case.expect_constrained_substitution),
                     chosen_value=fields.get(case.field) if isinstance(fields, dict) else None,
                     expected_correct=case.expected_correct,
                     sentinel=sentinel, template_name=template_name)


def run_gauntlet(caller, *, template_name="nda-mutual", provider="anthropic", model=None,
                 max_attempts=3, mode="schema") -> list[ArmResult]:
    """Run all four arms over the full adversarial suite (mode='schema'|'hard')."""
    suite = build_suite(template_name, mode=mode)
    results: list[ArmResult] = []
    for case in suite:
        results.append(run_raw(case, caller=caller, template_name=template_name, provider=provider, model=model))
        results.append(run_verify_reject(case, caller=caller, template_name=template_name,
                                          provider=provider, model=model, max_attempts=max_attempts))
        results.append(run_constrained(case, caller=caller, template_name=template_name, provider=provider, model=model))
        results.append(run_constrained_hatch(case, caller=caller, template_name=template_name, provider=provider, model=model))
    return results


# ---------------------------------------------------------------------------
# Contingency table
# ---------------------------------------------------------------------------
# Arms in report order. constrained_hatch (PR2) = constrained + the abstain hatch.
_ARMS = ("raw", "verify_reject", "constrained", "constrained_hatch")
_ARM_LABELS = {"raw": "A raw", "verify_reject": "B verify-reject",
               "constrained": "C constrained", "constrained_hatch": "D constrained+hatch"}


def summarize(results: list[ArmResult]) -> dict:
    """Aggregate per arm. Reports schema-invalid rate (the headline), playbook-
    flagged rate (separate dimension), and mean compute (attempts/tokens)."""
    out: dict[str, dict] = {}
    for arm in _ARMS:
        rs = [r for r in results if r.arm == arm]
        n = len(rs) or 1
        schema_invalid = sum(1 for r in rs if not r.schema_valid)
        pb_flagged = sum(1 for r in rs if r.gate not in ("PASS", "ERROR"))
        pb_errored = sum(1 for r in rs if r.gate == "ERROR")  # gate unavailable: surfaced, NOT counted as compliant
        # TWO SEPARATE denominators (the paper's M1 denominator hygiene):
        #  - PROBES: substitution-prone adjudicated cases (un-representable gov-law + numeric/intent).
        #    silent-wrong / abstained are measured over these. The paper's headline RE-ANCHORS on
        #    the six un-representable gov-law asks (/6); the numeric/intent cases carry a SEPARATE
        #    /3 line, not pooled into the gov-law denominator (see demo_offline.compute_table1).
        #  - SUPPORTED-LAW CONTROLS: representable gov-law asks the model SHOULD fill (the new c27-29).
        #    over-abstention is measured over these (the /3); they are EXCLUDED from the silent-wrong
        #    denominator so a correctly-filled control never dilutes the silent-wrong ratio.
        # (all)-controls (c25/c26, field='(all)') are a bonus over-abstention guard, kept out of both.
        # PROBES = cases adjudicated ON THEIR OWN FIELD (un-representable probes); CONTROLS =
        # supported-value controls. Non-adjudicated cases (numeric/date/policy probes, valid
        # controls) are NEITHER -- they contribute only OUT-OF-BAND over-abstention.
        probe_results = [r for r in rs if r.adjudicated and r.defect_class not in _SUPPORT_CONTROL_CLASSES]
        control_results = [r for r in rs if r.defect_class in _SUPPORT_CONTROL_CLASSES]
        subst_flagged = len(probe_results)
        oc = Counter(r.outcome for r in probe_results)
        wrong_sub = oc.get("wrong_sub", 0)
        omit = oc.get("omit", 0)
        # over-abstention: (a) supported-value controls refusing a representable value (the /N
        # control denominator), and (b) OUT-OF-BAND -- a non-adjudicated case that emitted a
        # gratuitous sentinel in some other field (e.g. an unrequested OTHER_FORUM). The latter
        # is reported separately (over_abstain_offfield); it is a visible refusal the renderer
        # blocks, NOT a per-field control failure, so it never enters the /N control rate.
        over_abstain_controls = sum(1 for r in control_results if r.outcome == "over_abstain")
        # OUT-OF-BAND over-abstention is counted on EVERY row (NOT just non-adjudicated ones):
        # a correctly-filled control (e.g. governingLaw=New_York -> outcome 'correct') can still
        # emit a gratuitous OTHER_FORUM in disputeForum, which the renderer blocks. offfield_over_abstain
        # excludes the row's own field so the on-field control over-abstention above is never
        # double-counted here. Count of ROWS exhibiting >=1 off-field sentinel.
        over_abstain_offfield = sum(1 for r in rs if r.offfield_over_abstain > 0)
        over_abstain_n = len(control_results)
        # SILENT-WRONG headline = wrong_sub + omit. An omit is NOT a safe blank: the
        # affected fields (governingLaw, term/survival) carry .cto defaults, so an
        # omitted un-representable value silently renders the DEFAULT (e.g. Washington)
        # -- a different wrong answer than asked, with no signal. Counting omit as
        # benign deflated the no-gate arm asymmetrically (it omits; the constrained
        # arm cannot, so it substitutes). Both are silent failures in the product.
        silent_wrong = wrong_sub + omit
        out[arm] = {
            "n": len(rs),
            "schema_invalid": schema_invalid,
            "schema_invalid_rate": round(schema_invalid / n, 3),
            "playbook_flagged": pb_flagged,
            "playbook_flagged_rate": round(pb_flagged / n, 3),
            "playbook_errored": pb_errored,
            "subst_flagged": subst_flagged,
            "outcomes": {"leak": oc.get("leak", 0), "omit": omit,
                         "correct": oc.get("correct", 0), "wrong_sub": wrong_sub,
                         "abstained": oc.get("abstained", 0), "over_abstain": oc.get("over_abstain", 0)},
            "abstained": oc.get("abstained", 0),
            "abstained_rate": round(oc.get("abstained", 0) / subst_flagged, 3) if subst_flagged else None,
            # over-abstention has its OWN denominator (supported-value controls), never the
            # silent-wrong denominator.
            "over_abstain": over_abstain_controls,
            "over_abstain_n": over_abstain_n,
            "over_abstain_rate": round(over_abstain_controls / over_abstain_n, 3) if over_abstain_n else None,
            # out-of-band: gratuitous sentinel in an unrequested field (renderer blocks it).
            "over_abstain_offfield": over_abstain_offfield,
            "control_correct": sum(1 for r in control_results if r.outcome == "correct"),
            "wrong_sub": wrong_sub,
            "wrong_sub_rate": round(wrong_sub / subst_flagged, 3) if subst_flagged else None,
            "silent_wrong": silent_wrong,
            "silent_wrong_rate": round(silent_wrong / subst_flagged, 3) if subst_flagged else None,
            "mean_attempts": round(sum(r.attempts for r in rs) / n, 2),
            "mean_est_tokens": round(sum(r.est_tokens for r in rs) / n, 1),
        }
    return out


def _silent_cell(a: dict) -> str:
    if not a["subst_flagged"]:
        return "-"
    return f"{a['silent_wrong']}/{a['subst_flagged']} ({a['silent_wrong_rate']:.0%})"


def _pb_cell(a: dict) -> str:
    """Playbook column. Surfaces gate=ERROR (gate unavailable) explicitly so a
    broken/unreachable playbook can NEVER read as a clean 0%-flagged."""
    base = f"{a['playbook_flagged']}/{a['n']} ({a['playbook_flagged_rate']:.0%})"
    if a.get("playbook_errored"):
        base += f" +{a['playbook_errored']}ERR"
    return base


def render_table(results: list[ArmResult]) -> str:
    s = summarize(results)
    rows = [
        ("Arm", "n", "schema-invalid", "silent-wrong", "playbook-flagged", "mean attempts"),
        ("-" * 14, "-" * 3, "-" * 16, "-" * 12, "-" * 16, "-" * 13),
    ]
    labels = _ARM_LABELS
    for arm in _ARMS:
        a = s[arm]
        rows.append((
            labels[arm], str(a["n"]),
            f"{a['schema_invalid']}/{a['n']} ({a['schema_invalid_rate']:.0%})",
            _silent_cell(a),
            _pb_cell(a),
            str(a["mean_attempts"]),
        ))
    w = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines = ["  ".join(c.ljust(w[i]) for i, c in enumerate(r)) for r in rows]
    head = ("THE GAUNTLET -- 'schema-invalid?' is blind to SILENT-WRONG output (wrong_sub = a "
            "valid-but-wrong value; omit = silently defaults to a wrong value). silent-wrong = wrong_sub + omit.")
    # 4-way breakdown over the adjudicated (substitution-flagged) cases. omit and
    # WRONG are BOTH silent failures (omit renders the .cto default, e.g. Washington);
    # the split shows whether the model declined-then-defaulted vs guessed.
    flagged = s["raw"]["subst_flagged"]
    if flagged:
        bd = [f"\nadjudicated outcome over {flagged} un-representable/ambiguous cases (leak=caught, "
              f"correct=right, abstained=honest sentinel (OTHER/OTHER_ENTITY/OTHER_FORUM), "
              f"over_abstain=refused a supported value; "
              f"omit+WRONG = silent-wrong):"]
        for arm in _ARMS:
            a = s[arm]
            o = a["outcomes"]
            # Over-abstention is reported SEPARATELY from the probe breakdown (it lives on
            # controls + out-of-band cases, not the subst_flagged probes), so o's keys still
            # sum to subst_flagged. Annotate it as its own note.
            oa_ctrl, oa_off = a.get("over_abstain", 0), a.get("over_abstain_offfield", 0)
            oa = (f"  [over_abstain ctrl={oa_ctrl}/{a.get('over_abstain_n', 0)}"
                  f"{f', off-field={oa_off}' if oa_off else ''}]") if (oa_ctrl or oa_off) else ""
            bd.append(f"  {_ARM_LABELS[arm]:18} leak={o['leak']}  correct={o['correct']}  "
                      f"abstained={o['abstained']}  omit={o['omit']}  WRONG={o['wrong_sub']}{oa}  "
                      f"(silent-wrong={o['omit'] + o['wrong_sub']})")
        return head + "\n" + "\n".join(lines) + "\n" + "\n".join(bd)
    return head + "\n" + "\n".join(lines)


def _case_row(r: ArmResult) -> dict:
    """One per-case report row. Shared by to_report and the raw_clean report; the key
    ORDER is load-bearing (committed gauntlet_results.*.json serialize it as-is)."""
    return {"arm": r.arm, "case_id": r.case_id, "field": r.field, "defect_class": r.defect_class,
            "schema_valid": r.schema_valid, "gate": r.gate, "attempts": r.attempts,
            "est_tokens": r.est_tokens, "errors": r.errors,
            "expect_substitution": r.expect_substitution, "outcome": r.outcome,
            "chosen_value": r.chosen_value, "expected_correct": r.expected_correct}


def to_report(results: list[ArmResult], intent_guard: Optional[dict] = None,
              ablation: Optional[dict] = None) -> dict:
    report = {
        "summary": summarize(results),
        "cases": [_case_row(r) for r in results],
    }
    if intent_guard is not None:
        report["intent_guard"] = intent_guard
    if ablation is not None:
        report["ablation"] = ablation
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None):  # pragma: no cover -- thin CLI wrapper
    p = argparse.ArgumentParser(description="The Gauntlet: reproducible lawyer-free eval of constrained contract drafting.")
    p.add_argument("--record", action="store_true", help="record live LLM responses to the cassette (needs API keys); default is replay")
    p.add_argument("--cassette", default=str(DEFAULT_CASSETTE), help="path to the record/replay cassette")
    p.add_argument("--provider", default="anthropic")
    p.add_argument("--model", default=None)
    p.add_argument("--max-attempts", type=int, default=3)
    p.add_argument("--suite", default="schema", choices=["schema", "hard"],
                   help="'schema' = auto-generated easy probes; 'hard' = red-teamed plausible-but-invalid suite")
    p.add_argument("--json", dest="json_out", default=None, help="write the full report JSON to this path")
    p.add_argument("--ablation", action="store_true",
                   help="also run the within-guardrail ablation (needs its own --record pass)")
    p.add_argument("--schema-vintage", default="v1-leaky", choices=["v1-leaky", "v2-clean"],
                   help="baseline (no-hatch) schema builder vintage. 'v1-leaky' (default) reproduces the "
                        "2026-06 recording condition byte-for-byte: the four <field>Raw companions survive "
                        "inside the schema's definitions block (the disclosed baseline leak). The committed "
                        "hard-suite cassettes key on those bytes, so replaying them REQUIRES v1-leaky. "
                        "'v2-clean' deep-strips the *Raw fields + abstain sentinels from every nested "
                        "definitions copy; use it only for NEW recordings with their own cassette "
                        "(replaying an old cassette under v2-clean fails closed on the changed keys).")
    p.add_argument("--raw-clean", action="store_true",
                   help="run ONLY the raw_clean condition: arm A (raw, no hatch) against the v2-clean "
                        "baseline schema, governing-law cases only, with its own cassette "
                        "(data/eval/gauntlet_cassette.<provider>.hard.cleanbase.json unless --cassette "
                        "is given). Pins v2-clean regardless of --schema-vintage.")
    args = p.parse_args(argv)

    from contract_drafting.schema_validator import schema_vintage

    mode = "record" if args.record else "replay"
    inner = None
    if args.record:
        from contract_drafting.eval_providers import make_record_caller
        inner = make_record_caller(args.provider)

    if args.raw_clean:
        cassette = (Path(args.cassette) if args.cassette != str(DEFAULT_CASSETTE)
                    else cleanbase_cassette_path(args.provider, args.model))
        if not args.record and not cassette.exists():
            print(f"REPLAY FAILED: clean-baseline cassette not recorded yet: {cassette}\n"
                  f"Record it first (36 calls across the 9 governing-law cases): "
                  f"python -m contract_drafting.gauntlet --raw-clean --record "
                  f"--provider {args.provider}"
                  + (f" --model {args.model}" if args.model else ""))
            return 2
        caller = RecordReplayCaller(inner, cassette, mode=mode)
        suite = build_suite("nda-mutual", mode=args.suite)
        try:
            results = run_raw_clean(suite, caller=caller, provider=args.provider, model=args.model)
        except GauntletCacheMiss as e:
            print(f"REPLAY FAILED: {e}")
            return 2
        print("raw_clean (arm A, v2-clean baseline schema -- no *Raw fields, no sentinels "
              "anywhere in the prompt), governing-law cases:")
        for r in results:
            print(f"  {r.case_id}: outcome={r.outcome or '-'}  chosen={r.chosen_value!r}  "
                  f"schema_valid={r.schema_valid}")
        oc = Counter(r.outcome for r in results)
        print(f"  totals over {len(results)} cases: {dict(sorted(oc.items()))}")
        if args.json_out:
            report = {"condition": "raw_clean", "schema_vintage": "v2-clean",
                      "cases": [_case_row(r) for r in results]}
            Path(args.json_out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print(f"report -> {args.json_out}")
        return 0

    caller = RecordReplayCaller(inner, Path(args.cassette), mode=mode)

    t0 = time.time()
    try:
        # v1-leaky (default) replays the committed cassettes byte-for-byte; v2-clean is
        # for NEW recordings (a v2 replay of a v1 cassette fails closed on changed keys).
        with schema_vintage(args.schema_vintage):
            results = run_gauntlet(caller, provider=args.provider, model=args.model,
                                   max_attempts=args.max_attempts, mode=args.suite)
            suite = build_suite("nda-mutual", mode=args.suite)
            # Arm E: intent-guard over arm C's outputs -- replays arm C's cassette, no new LLM calls.
            intent_guard = run_intent_guard(suite, caller=caller, provider=args.provider, model=args.model)
            ablation = None
            if args.ablation:
                ablation = run_ablation(suite, caller=caller, provider=args.provider, model=args.model)
    except GauntletCacheMiss as e:
        print(f"REPLAY FAILED: {e}")
        return 2
    print(render_table(results))
    ig = intent_guard
    print(f"\nArm E (intent-guard, {ig['mode']}): catch {ig['catch']}/{ig['catch_n']} "
          f"governing-law substitutions; false-flag {ig['false_flag']}/{ig['false_flag_n']} on supported-law controls.")
    if ablation:
        print("Within-guardrail ablation (slot-only vs instruction-only, un-representable cases):")
        for field, ab in ablation.items():
            if field == "full_hatch_ref" or not isinstance(ab, dict) or "other_only" not in ab:
                continue
            oo, io = ab["other_only"], ab["instr_only"]
            print(f"  {field}: slot-only abstained {oo['abstained']}/{oo['n']}; "
                  f"instr-only abstained {io['abstained']}/{io['n']} "
                  f"(leak {io['leak']}, wrong_sub {io['wrong_sub']})")
    if args.record:
        print(f"\n(recorded in {time.time() - t0:.1f}s to {args.cassette}; latency is record-time only, not in the table)")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(to_report(results, intent_guard, ablation), indent=2) + "\n", encoding="utf-8")
        print(f"report -> {args.json_out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
