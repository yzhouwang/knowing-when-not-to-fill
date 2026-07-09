"""
demo_offline.py -- the six-beat OFFLINE demo driver (the EMNLP demo video script).

Every beat runs with NO API key and NO network: all LLM responses replay from the
committed Gauntlet cassettes (data/eval/gauntlet_cassette.*.hard.json) via the same
RecordReplayCaller the eval uses -- a cache miss fails closed (GauntletCacheMiss),
never a silent live call. The render/playbook/audit steps are the real production
draft path (compliance_draft / cicero_bridge), which is LLM-free by construction.

Beats (paper Section 5, "Demonstration walkthrough"):

  beat 1  The invisible failure -- replay case c02's CONSTRAINED fill: validator
          green, playbook PASS, audit row written, and the rendered governing-law
          clause reads "England and Wales" (the silent substitution). No warning.
  beat 2  Reveal the silence -- static type-error explainer over the .cto: the
          asked value is not in the enum (FILE:LINE), the decoder substituted.
          (alias: explain --field governingLaw --asked 'laws of Scotland')
  beat 3  Flip on the guardrail -- same input, HATCH arm: governingLaw=OTHER +
          governingLawRaw captured; the production path fails closed -> ESCALATED
          + audit row; the intent gate retroactively flags beat 1's draft.
  beat 4  The calibration race -- the three supported-law controls (c27-c29) fill
          correctly with NO abstention; one renders with the conformance footer.
  beat 5  Reproduce it offline -- recompute the paper's Table 1 (gov-law silent-wrong
          /6 + a separate numeric/intent /3 line, 4 arms x 4 models) from the committed
          results files in <1s.  (alias: table1; --case <id> drills into any case)
  beat 6  Close the loop -- the audit row for the abstained draft: audit_id,
          status ESCALATED, the raw captured ask, sha256 of the stored record.
  all     beats 1-6 in sequence (the video script).

Usage:
  python -m contract_drafting.demo_offline            # no subcommand = all
  python -m contract_drafting.demo_offline all [--model gpt-5.5] [--db PATH]
  python -m contract_drafting.demo_offline beat 1
  python -m contract_drafting.demo_offline explain --field disputeForum --asked KCAB
  python -m contract_drafting.demo_offline table1 --case c02
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import textwrap
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from contract_drafting import demo_mars_beat as demo
from contract_drafting import gauntlet as g
from contract_drafting import intent_check
from contract_drafting import jurisdiction_map
from contract_drafting.compliance_draft import (
    DraftRequest,
    _escalation_sha256,
    draft_contract,
    get_audit_log,
)
from contract_drafting.gauntlet import RecordReplayCaller

_REPO = Path(__file__).resolve().parent.parent
_EVAL = _REPO / "data" / "eval"
_CTO = _REPO / "data" / "templates" / "cicero" / "nda-mutual" / "model" / "model.cto"
_PLAYBOOK = _REPO / ".claude" / "legal.local.md"
# Demo-scoped audit DB (S3): the demo must NOT write into the production audit DB
# (data/contract_drafting.db) by default. Still under data/ so the data/*.db
# gitignore entry covers it; --db overrides.
DEFAULT_DB = str(_REPO / "data" / "demo_offline.db")
_TEMPLATE = "nda-mutual"

# model name -> (provider, cassette/results file tag). Order = the paper's Table 1 columns.
MODELS = {
    "gpt-5.5": ("openai", "openai"),
    "deepseek-v4-pro": ("deepseek", "deepseek"),
    "deepseek-v4-flash": ("deepseek", "deepseek-flash"),
    "claude-sonnet-4-6": ("anthropic", "anthropic"),
}
_TABLE1_HEADERS = {  # short column labels, as in the paper
    "gpt-5.5": "gpt-5.5", "deepseek-v4-pro": "v4-pro",
    "deepseek-v4-flash": "v4-flash", "claude-sonnet-4-6": "sonnet-4.6",
}

# The paper's Table 1 denominators (Section "Decomposition"). Frozen on purpose --
# they ARE the paper's numbers -- but cross-checked against the committed suite's own
# case flags by _check_table1_denominators() (M7), which compute_table1 runs every
# time, so a suite edit that silently shifts a denominator fails LOUD instead of
# quietly changing the headline table.
ADJUDICATED = ("c01", "c02", "c03", "c04", "c06", "c08", "c15", "c16", "c17")
GOV_UNREP = ("c01", "c02", "c03", "c04", "c06", "c08")
# The headline table re-anchors on GOV_UNREP /6; the numeric/intent cases below carry
# their own /3 line (paper Table 1, "Decomposition"). NUMERIC = ADJUDICATED \ GOV_UNREP.
NUMERIC = ("c15", "c16", "c17")
CONTROLS = ("c27", "c28", "c29")
assert tuple(sorted(GOV_UNREP + NUMERIC)) == tuple(sorted(ADJUDICATED))
_ARMS = ("raw", "verify_reject", "constrained", "constrained_hatch")
_ARM_LABELS = {"raw": "A raw", "verify_reject": "B verify-reject",
               "constrained": "C constrained", "constrained_hatch": "D +abstention"}


# ---------------------------------------------------------------------------
# Terminal style (ANSI only on a tty)
# ---------------------------------------------------------------------------
def _ansi(code: str) -> str:
    return f"\033[{code}m" if sys.stdout.isatty() else ""


def _sty():
    return {"B": _ansi("1"), "DIM": _ansi("2"), "G": _ansi("32"), "R": _ansi("31"),
            "Y": _ansi("33"), "C": _ansi("36"), "0": _ansi("0")}


def _hr(title: str) -> None:
    s = _sty()
    print()
    print(s["B"] + "=" * 78 + s["0"])
    print(s["B"] + f" {title}" + s["0"])
    print(s["B"] + "=" * 78 + s["0"])


def _kv(key: str, value: str, indent: int = 1) -> None:
    print(" " * indent + f"{key:<18}: {value}")


def _wrap(text: str, indent: str = "   | ", width: int = 76) -> str:
    return "\n".join(indent + ln for ln in textwrap.wrap(text, width - len(indent)))


# ---------------------------------------------------------------------------
# Replay plumbing (reuses the Gauntlet's cassette replay -- offline, fail-closed)
# ---------------------------------------------------------------------------
_callers: dict[str, RecordReplayCaller] = {}
_results_cache: dict[str, dict] = {}
_suite_cache: dict[str, Any] = {}


def _caller(model: str) -> RecordReplayCaller:
    if model not in _callers:
        tag = MODELS[model][1]
        path = _EVAL / f"gauntlet_cassette.{tag}.hard.json"
        _callers[model] = RecordReplayCaller(None, path, mode="replay")
    return _callers[model]


def _results(model: str) -> dict:
    if model not in _results_cache:
        tag = MODELS[model][1]
        path = _EVAL / f"gauntlet_results.{tag}.hard.json"
        _results_cache[model] = json.loads(path.read_text(encoding="utf-8"))
    return _results_cache[model]


def _case(case_id: str) -> g.Case:
    if not _suite_cache:
        _suite_cache.update({c.id: c for c in g.load_hard_suite()})
    try:
        return _suite_cache[case_id]
    except KeyError:
        # RT6: friendly message; main() pre-validates --case and exits cleanly on this.
        raise KeyError(f"unknown case id {case_id!r}; valid ids: "
                       f"{', '.join(sorted(_suite_cache))}") from None


def _arm_result(case_id: str, model: str, arm: str) -> g.ArmResult:
    """Replay ONE (case, arm) through the real Gauntlet arm implementation from the
    committed cassette -- no network, fail-closed on a cache miss."""
    provider = MODELS[model][0]
    fn = {"raw": g.run_raw, "verify_reject": g.run_verify_reject,
          "constrained": g.run_constrained, "constrained_hatch": g.run_constrained_hatch}[arm]
    return fn(_case(case_id), caller=_caller(model), template_name=_TEMPLATE,
              provider=provider, model=model)


# ---------------------------------------------------------------------------
# Production draft-path plumbing
# ---------------------------------------------------------------------------
def _request_from_fields(fields: dict, *, out_tag: str, out_dir: Optional[str] = None) -> DraftRequest:
    """Map a replayed camelCase slot-fill onto the production DraftRequest (the same
    typed object the deterministic Cicero draft path consumes)."""
    # C10: demo artifacts default to data/demo_drafts/ (created on demand), never the
    # production data/drafts/ directory; --out-dir still overrides.
    out_base = Path(out_dir) if out_dir else (_REPO / "data" / "demo_drafts")
    return DraftRequest(
        doc_type="nda",
        disclosing_party=fields.get("disclosingParty", ""),
        receiving_party=fields.get("receivingParty", ""),
        disclosing_entity_type=fields.get("disclosingEntityType", "corporation"),
        receiving_entity_type=fields.get("receivingEntityType", "corporation"),
        purpose=fields.get("purpose", "exploring a potential business relationship"),
        term_months=int(fields.get("termMonths", 24)),
        notice_days=int(fields.get("noticeDays", 30)),
        survival_years=int(fields.get("survivalYears", 3)),
        governing_law=str(fields.get("governingLaw", "Washington")),
        governing_law_raw=str(fields.get("governingLawRaw", "") or ""),
        disclosing_entity_type_raw=str(fields.get("disclosingEntityTypeRaw", "") or ""),
        receiving_entity_type_raw=str(fields.get("receivingEntityTypeRaw", "") or ""),
        # C1: a model-filled dispute forum (e.g. gpt-5.5's gratuitous LCIA on c02) is
        # threaded into the request so the production path CAPTURES it (warning +
        # structured audit record), never silently drops it. Enum-valid forums and
        # OTHER_FORUM stay non-fatal; out-of-enum strings fail closed at the schema gate.
        dispute_forum=str(fields.get("disputeForum", "") or ""),
        dispute_forum_raw=str(fields.get("disputeForumRaw", "") or ""),
        effective_date=fields.get("effectiveDate", ""),
        mutual=bool(fields.get("mutual", True)),
        has_non_compete=bool(fields.get("hasNonCompete", False)),
        has_non_solicitation=bool(fields.get("hasNonSolicitation", False)),
        has_residuals_clause=bool(fields.get("hasResidualsClause", False)),
        user="demo_offline",
        playbook_path=str(_PLAYBOOK) if _PLAYBOOK.exists() else "",
        output_path=str(out_base /
                        f"demo_offline_{out_tag}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.docx"),
    )


def _gov_clause(rendered_markdown: str) -> str:
    """The rendered governing-law sentence from the Cicero markdown output."""
    for line in rendered_markdown.splitlines():
        if line.strip().startswith("This Agreement shall be governed by"):
            return line.strip()
    return "(governing-law clause not found)"


def _render_markdown(request: DraftRequest) -> str:
    """Deterministic INDEPENDENT re-render of a request through the real Cicero engine.
    The beats no longer call this (they read draft_contract's result['rendered_text'],
    P1 -- one render per draft); it remains for tests that verify byte-identity of
    rendered_text/render_sha256 against an independent render of the same request."""
    from contract_drafting import cicero_bridge
    res = cicero_bridge.draft(request, template_name=_TEMPLATE)
    return res.text if res.success else f"(render failed: {res.error})"


# ---------------------------------------------------------------------------
# Beat 1 -- the invisible failure
# ---------------------------------------------------------------------------
def beat1(ctx: dict) -> dict:
    s = _sty()
    model = ctx["model"]
    case = _case("c02")
    _hr(f"BEAT 1 | The invisible failure -- constrained decoding, no guardrail [{model}]")
    print(" operator asks (case c02):")
    print(_wrap(case.instruction, indent="   > "))
    print()

    r = _arm_result("c02", model, "constrained")
    fields = r.fields or {}
    # RT4: some models' replayed constrained fill OMITS governingLaw entirely (e.g.
    # deepseek-v4-pro). _request_from_fields then injects the DRIVER's 'Washington'
    # default -- a different silent failure. Never narrate that injected default as
    # the model's substitution.
    omitted = not fields.get("governingLaw")
    print(" [1] LLM slot-fill, constrained to the typed schema "
          "(replayed from committed cassette -- offline, no API key):")
    if omitted:
        _kv("governingLaw", s["R"] + "(omitted -- field absent from the model's fill)"
            + s["0"], indent=5)
    else:
        _kv("governingLaw", s["B"] + str(fields.get("governingLaw")) + s["0"], indent=5)

    errors = demo._validate_arm(fields, _TEMPLATE, with_abstain=False)
    verdict = (s["G"] + "VALID" + s["0"] + " -- 0 errors (well-typed by construction)"
               if not errors else s["R"] + f"INVALID ({len(errors)} errors)" + s["0"])
    print(f" [2] schema validator   : {verdict}")

    req = _request_from_fields(fields, out_tag="beat1", out_dir=ctx.get("out_dir"))
    result = draft_contract(req, engine="cicero", db_path=ctx["db"])
    gate = result.get("gate_result", "?")
    gate_s = (s["G"] if gate == "PASS" else s["Y"]) + gate + s["0"]
    print(f" [3] playbook gate      : {gate_s} (playbook v{result.get('playbook_version', '?')})")
    print(f" [4] deterministic Cicero render -> docx : {result.get('output_path')}")
    print(f" [5] audit row written  : audit_id={result.get('audit_id')}  "
          f"render_sha256={str(result.get('render_sha256', ''))[:16]}...")
    print()
    print(" rendered governing-law clause:")
    # P1: the clause comes from the draft's OWN rendered bytes (result['rendered_text'],
    # sha256 == render_sha256) -- no second render of the same request.
    clause = _gov_clause(result.get("rendered_text") or "")
    print(_wrap(clause))
    if omitted:
        print()
        print(_wrap("note: the model omitted governingLaw; the pipeline default fills "
                    "Washington -- a different silent failure with no signal (the "
                    "'Washington' above is the driver's injected default, not the "
                    "model's fill)."))
    ctx["beat1_fields"] = fields
    ctx["beat1_result"] = result
    return {"fields": fields, "result": result, "clause": clause, "schema_errors": errors,
            "omitted": omitted}


# ---------------------------------------------------------------------------
# Beat 2 -- the type-error explainer (pure static analysis of the .cto)
# ---------------------------------------------------------------------------
_FIELD_ALIASES = {"entityType": "receivingEntityType"}


def _probe_case_for_field(field: str) -> Optional[g.Case]:
    """The first committed ec*/fc* cross-field probe case with an un-representable ask
    for `field`, if one exists (C5: named textually in beat 2 for fields other than
    governingLaw, instead of fabricating a beat-1 narrative from c02's unrelated fill)."""
    if not _suite_cache:
        _suite_cache.update({c.id: c for c in g.load_hard_suite()})
    for cid in sorted(_suite_cache):
        c = _suite_cache[cid]
        if c.field == field and c.representable is False and re.fullmatch(r"(ec|fc)\d+", cid):
            return c
    return None


def explain_field(field: str, asked: str, template_name: str = _TEMPLATE) -> dict:
    """Locate `field`'s declaration and its enum's declaration in the .cto (FILE:LINE),
    decide whether `asked` is representable, and name the abstain sentinel. Pure static
    analysis -- works for governingLaw, disclosingEntityType/receivingEntityType
    (alias: entityType), and disputeForum."""
    field = _FIELD_ALIASES.get(field, field)
    lines = _CTO.read_text(encoding="utf-8").splitlines()

    field_line = ftype = None
    for i, ln in enumerate(lines, 1):
        m = re.match(r"\s*o\s+([A-Za-z_]\w*)\s+" + re.escape(field) + r"\b", ln)
        if m:
            field_line, ftype, field_decl = i, m.group(1), ln.strip()
            break
    if field_line is None:
        raise ValueError(f"field {field!r} not declared in {_CTO}")

    enum_start = enum_end = None
    members: list[tuple[str, int]] = []  # (identifier, line)
    for i, ln in enumerate(lines, 1):
        if enum_start is None:
            if re.match(r"\s*enum\s+" + re.escape(ftype) + r"\s*\{", ln):
                enum_start = i
            continue
        if ln.strip() == "}":
            enum_end = i
            break
        m = re.match(r"\s*o\s+([A-Za-z_]\w*)\s*$", ln)
        if m:
            members.append((m.group(1), i))
    if enum_start is None:
        raise ValueError(f"enum {ftype!r} not declared in {_CTO}")

    from contract_drafting.schema_validator import load_abstain_policies
    policy = load_abstain_policies(template_name).get(field) or {}
    sentinel = policy.get("sentinel")
    raw_field = policy.get("rawField")
    raw_line = None
    if raw_field:
        for i, ln in enumerate(lines, 1):
            if re.match(r"\s*o\s+String\s+" + re.escape(raw_field) + r"\b", ln):
                raw_line = i
                break

    # Representability of the asked value, normalized the same way the pipeline does.
    representable_ids = {m for m, _ in members} - ({sentinel} if sentinel else set())
    if field == "governingLaw":
        resolved = intent_check._resolve_clean(asked, template_name)
    else:
        # M3 single-source: field -> enum name derived from the generated abstain policy.
        from contract_drafting.schema_validator import enum_display_fields
        enum_name = enum_display_fields(template_name)[field]
        try:
            cand = jurisdiction_map.to_identifier_enum(enum_name, asked, template_name)
        except Exception:  # noqa: BLE001
            cand = asked
        resolved = cand if cand in representable_ids else None

    return {
        "field": field, "field_line": field_line, "field_decl": field_decl,
        "enum": ftype, "enum_start": enum_start, "enum_end": enum_end,
        "members": members, "n_members": len(members),
        "sentinel": sentinel, "raw_field": raw_field, "raw_line": raw_line,
        "asked": asked, "resolved": resolved, "representable": resolved is not None,
        "cto": str(_CTO.relative_to(_REPO)),
    }


def beat2(ctx: dict, field: str = "governingLaw", asked: str = "laws of Scotland") -> dict:
    s = _sty()
    model = ctx["model"]
    info = explain_field(field, asked)
    _hr(f"BEAT 2 | Reveal the silence -- the type-error explainer (static .cto analysis)")
    cto = info["cto"]
    short = Path(cto).name  # short form after the full path has been shown once
    _kv("field", f"{info['field']}   ({cto}:{info['field_line']})")
    print(f"      {short}:{info['field_line']:<4} {info['field_decl']}")
    _kv("declared type", f"enum {info['enum']}   ({short}:{info['enum_start']}-{info['enum_end']}, "
                         f"{info['n_members']} members incl. the {info['sentinel']} sentinel)")
    # show a small window of the enum: first two members, the sentinel, and (if known) the substitute
    member_lines = {m: ln for m, ln in info["members"]}
    show = [m for m, _ in info["members"][:2]]
    if info["sentinel"] in member_lines:
        show.append(info["sentinel"])
    for m in show:
        print(f"      {short}:{member_lines[m]:<4}   o {m}")
    print(f"      {s['DIM']}... ({info['n_members']} members total){s['0']}")
    print()
    _kv("asked value", repr(info["asked"]))
    stripped = intent_check._PREFIX_RE.sub("", info["asked"]).strip()
    if info["representable"]:
        print(f"   -> normalizes to {info['resolved']!r} -- representable "
              f"({short}:{member_lines.get(info['resolved'], '?')}).")
    else:
        print(f"   -> normalizes to {stripped!r} -- {s['R']}NOT a member of "
              f"{info['enum']}{s['0']}: the asked value is un-representable in this type.")

    # what the constrained decoder actually did on this input in beat 1 (replayed).
    # C5: beat 1's replayed evidence is case c02, whose field-under-test is
    # governingLaw -- for ANY other field, c02's fill is unrelated, so reading it
    # would fabricate a substitution narrative. For those fields the static .cto
    # analysis stands alone; a matching ec*/fc* probe case is named textually
    # WITHOUT claiming beat-1 evidence.
    subst = None
    if not info["representable"] and info["field"] == "governingLaw":
        fields = ctx.get("beat1_fields") or (_arm_result("c02", model, "constrained").fields or {})
        subst = fields.get(info["field"])
        if subst is not None:
            line = member_lines.get(str(subst), "?")
            print()
            print(f" what the constrained decoder substituted in beat 1 (case c02, {model}):")
            print(f"      {info['field']} = {s['B']}{subst}{s['0']}   ({short}:{line}) -- a valid "
                  f"enum member the operator never asked for. Nothing on screen warned.")
    elif not info["representable"]:
        probe = _probe_case_for_field(info["field"])
        print()
        if probe is not None:
            print(_wrap(f"beat 1's replayed evidence covers governingLaw (case c02) only; "
                        f"for {info['field']}, see the committed cross-field probe case "
                        f"{probe.id} ({probe.defect_class}) -- drill into it with "
                        f"`table1 --case {probe.id}`.", indent=" "))
        else:
            print(_wrap(f"beat 1's replayed evidence covers governingLaw (case c02) only; "
                        f"no committed probe case exercises {info['field']}.", indent=" "))
    if info["sentinel"]:
        print()
        print(f" the typed escape hatch: {info['sentinel']} ({short}:{member_lines.get(info['sentinel'], '?')})"
              f" + {info['raw_field']} ({short}:{info['raw_line']}) -- see beat 3.")
    return {**info, "substituted": subst}


# ---------------------------------------------------------------------------
# Beat 3 -- flip on the guardrail (hatch arm + fail-closed escalation + intent gate)
# ---------------------------------------------------------------------------
def _escalate_c02(ctx: dict) -> tuple[dict, dict]:
    """Replay c02's HATCH fill and push it through the PRODUCTION draft path.
    Returns (hatch_fields, draft_result) -- result is ESCALATED with an audit row."""
    r = _arm_result("c02", ctx["model"], "constrained_hatch")
    fields = r.fields or {}
    req = _request_from_fields(fields, out_tag="beat3", out_dir=ctx.get("out_dir"))
    result = draft_contract(req, engine="cicero", db_path=ctx["db"])
    if result.get("gate_result") == "ERROR":
        # C12: the production path now returns a CLEAN fail-closed ERROR (e.g. the
        # audit DB is unwritable) instead of raising. For the demo that is a
        # prerequisite failure -- there is no escalation/audit row to narrate.
        raise RuntimeError(result.get("error")
                           or "production draft path returned gate_result=ERROR")
    ctx["beat3_audit_id"] = result.get("audit_id")
    return fields, result


def beat3(ctx: dict) -> dict:
    s = _sty()
    model = ctx["model"]
    case = _case("c02")
    _hr(f"BEAT 3 | Flip on the guardrail -- same input, typed abstention [{model}]")
    print(" same operator ask (case c02), arm: constrained + abstention "
          "(the OTHER sentinel + raw capture + abstain instruction)")
    print()
    fields, result = _escalate_c02(ctx)
    print(" [1] LLM slot-fill (replayed from committed cassette):")
    _kv("governingLaw", s["B"] + str(fields.get("governingLaw")) + s["0"]
        + "   (the typed abstain sentinel -- not a jurisdiction)", indent=5)
    _kv("governingLawRaw", repr(fields.get("governingLawRaw")), indent=5)
    print()
    gate = result.get("gate_result")
    print(f" [2] production draft path: {s['Y']}{gate}{s['0']} -- FAIL CLOSED "
          f"(no signable contract rendered; output_path={result.get('output_path')})")
    print(_wrap(result.get("error", ""), indent="     ! "))
    _kv("audit_id", str(result.get("audit_id")), indent=5)
    _kv("escalation sha256", str(result.get("escalation_sha256", ""))[:32] + "...", indent=5)
    print()
    print(" [3] intent-consistency gate, retroactively applied to BEAT 1's draft")
    print("     (deterministic offline extractor -- no LLM). Offline, the gate cannot")
    print("     resolve the asked law to any supported jurisdiction, so it FLAGS the")
    print("     draft for human review (fail-closed) rather than proving the swap;")
    print("     the asked-vs-filled values below are the evidence:")
    beat1_fields = ctx.get("beat1_fields") or (_arm_result("c02", model, "constrained").fields or {})
    asked = intent_check.extract_jurisdiction(case.instruction)
    warnings = intent_check.verify_intent(case.instruction, beat1_fields,
                                          template_name=_TEMPLATE, allow_llm_fallback=False)
    # C13: print 'Scotland', never the Python list repr (['Scotland']).
    _asked_str = ", ".join(asked) if isinstance(asked, (list, tuple)) else str(asked)
    _kv("asked", f"{_asked_str} -- un-representable (not in the Jurisdiction enum)",
        indent=5)
    _kv("filled (beat 1)", str(beat1_fields.get("governingLaw")), indent=5)
    verdict = ((s["R"] + "FLAGGED" + s["0"] + " -- human review required")
               if warnings else (s["G"] + "consistent" + s["0"]))
    _kv("gate verdict", verdict, indent=5)
    for w in warnings:
        print(_wrap(w, indent="     ! "))
    return {"hatch_fields": fields, "result": result, "asked": asked, "warnings": warnings}


# ---------------------------------------------------------------------------
# Beat 4 -- the calibration race (supported-law controls fill, no abstention)
# ---------------------------------------------------------------------------
# M6: the displayed "asked" span is DERIVED from the loaded suite case's instruction
# (tolerant "governed by ... laws of X" extraction), never hardcoded per case id.
_ASKED_LAW_RE = re.compile(r"governed by[^.;]*?\b(laws of [^.;]+?)\s*(?=[.;]|$)",
                           re.IGNORECASE)


def _control_ask(case: g.Case) -> str:
    """The displayed asked-law span for a supported-law control, extracted from the
    case instruction; falls back to the adjudicated expected_correct display name
    (via the jurisdiction map) if the instruction phrasing defeats the regex."""
    m = _ASKED_LAW_RE.search(case.instruction)
    if m:
        return m.group(1).strip()
    try:
        return "laws of " + jurisdiction_map.to_display(str(case.expected_correct))
    except ValueError:
        return str(case.expected_correct)


def _print_footer(result: dict) -> None:
    """RT5: the LEGAL_DISCLAIMER footer is PASS-specific ('PASS certifies...'), so it
    may only print when the draft actually carries it (gate==PASS). On any other gate
    print the gate-neutral NOT_LEGAL_ADVICE line instead (mirrors main.py's
    gate-checked print) -- never a footer that contradicts the gate."""
    if result.get("disclaimer") or result.get("gate_result") == "PASS":
        print(" footer (rendered on every PASS draft):")
        print(_wrap(result.get("disclaimer", demo.LEGAL_DISCLAIMER), indent="   | "))
    else:
        print(" footer (gate-neutral -- this draft did not PASS):")
        print(_wrap(demo.NOT_LEGAL_ADVICE, indent="   | "))


def beat4(ctx: dict) -> dict:
    s = _sty()
    model = ctx["model"]
    _hr(f"BEAT 4 | The calibration race -- supported laws fill, never abstain [{model}]")
    print(" the three supported-law controls (hatch arm, same guardrail as beat 3):")
    print()
    fills = {}
    over_abstain = 0
    for cid in CONTROLS:
        r = _arm_result(cid, model, "constrained_hatch")
        f = r.fields or {}
        gov = str(f.get("governingLaw"))
        fills[cid] = f
        abstained = gov == "OTHER"
        over_abstain += int(abstained)
        mark = (s["R"] + "ABSTAINED (false abstention)" + s["0"] if abstained
                else s["G"] + "filled correctly -- no abstention" + s["0"])
        asked = _control_ask(_case(cid))
        print(f"   {cid}  asked {asked:<33} -> governingLaw = {gov:<22} {mark}")
    print()
    print(f" over-abstention: {s['B']}{over_abstain}/3{s['0']} "
          "(the guardrail abstains only on un-representable asks)")
    print()
    print(" rendering c28 (Republic of Singapore) through the production path:")
    req = _request_from_fields(fills["c28"], out_tag="beat4", out_dir=ctx.get("out_dir"))
    result = draft_contract(req, engine="cicero", db_path=ctx["db"])
    gate = result.get("gate_result")
    gate_s = (s["G"] if gate == "PASS" else s["Y"]) + str(gate) + s["0"]
    _kv("playbook gate", gate_s, indent=3)
    _kv("audit_id", str(result.get("audit_id")), indent=3)
    # P1: clause from the draft's own rendered bytes -- no second render.
    print(_wrap(_gov_clause(result.get("rendered_text") or "")))
    print()
    _print_footer(result)
    return {"fills": fills, "over_abstain": over_abstain, "result": result}


# ---------------------------------------------------------------------------
# Beat 5 -- reproduce Table 1 offline from the committed results
# ---------------------------------------------------------------------------
def derive_table1_sets() -> dict[str, tuple[str, ...]]:
    """Derive the paper's Table 1 denominator sets from the committed hard suite's OWN
    case flags (M7):

      adjudicated -- headline substitution-adjudicated cases: expect_constrained_substitution
                     is set on the case (author-adjudicated correctness exists);
      gov_unrep   -- un-representable governing-law asks: field == governingLaw and
                     representable is False;
      controls    -- supported-law controls: defect_class == "supported-law-control".

    Residual (documented): the suite carries no explicit "headline Table 1" flag; the
    headline scope (the original governing-law + numeric suite, vs the ec*/fc* cross-field
    replication cases, which are also substitution-adjudicated but reported separately)
    is encoded in the case-id namespace -- ids matching c\\d+ are the headline suite.
    That id-namespace scoping is the one non-flag assumption in this derivation.
    """
    if not _suite_cache:
        _suite_cache.update({c.id: c for c in g.load_hard_suite()})
    headline = [c for c in _suite_cache.values() if re.fullmatch(r"c\d+", c.id)]
    return {
        "adjudicated": tuple(c.id for c in headline if c.expect_constrained_substitution),
        "gov_unrep": tuple(c.id for c in headline
                           if c.field == "governingLaw" and c.representable is False),
        "controls": tuple(c.id for c in headline
                          if c.defect_class == "supported-law-control"),
    }


def _check_table1_denominators() -> None:
    """LOUD cross-check (M7): the frozen ADJUDICATED/GOV_UNREP/CONTROLS tuples (the
    paper's denominators) must equal the sets derived from the suite's case flags.
    Raises RuntimeError on any drift -- never silently recompute over a shifted set."""
    derived = derive_table1_sets()
    for name, frozen, got in (("ADJUDICATED", ADJUDICATED, derived["adjudicated"]),
                              ("GOV_UNREP", GOV_UNREP, derived["gov_unrep"]),
                              ("CONTROLS", CONTROLS, derived["controls"])):
        if tuple(sorted(frozen)) != tuple(sorted(got)):
            raise RuntimeError(
                f"Table 1 denominator drift: frozen {name}={tuple(sorted(frozen))} but the "
                f"committed hard suite derives {tuple(sorted(got))}. The frozen tuples are "
                f"the PAPER's denominators -- reconcile the suite flags (defect_class / "
                f"representable / expect_constrained_substitution) or the paper numbers "
                f"before trusting this table.")


def compute_table1() -> dict:
    """Recompute the paper's Table 1 from the four committed gauntlet_results.*.hard.json
    files, for all 4 arms x 4 models. The headline re-anchors on the six un-representable
    governing-law asks (silent-wrong = wrong_sub + omit, /6); the three numeric/intent
    cases (c15-c17) carry a SEPARATE /3 line rather than diluting the gov-law denominator
    (paper Table 1, "Decomposition"). Also the 6/6 abstain + 0/3 over-abstain line.

    The full 9-case ADJUDICATED set is still required present (existence check) so a
    dropped numeric row fails LOUD; only the headline SUM re-anchors to /6."""
    _check_table1_denominators()  # M7: fail LOUD if the suite no longer derives them
    SW = ("wrong_sub", "omit")
    table: dict[str, dict[str, int]] = {arm: {} for arm in _ARMS}
    numeric: dict[str, dict[str, int]] = {arm: {} for arm in _ARMS}
    abstain: dict[str, tuple[int, int]] = {}
    over: dict[str, tuple[int, int]] = {}
    for model in MODELS:
        cases = _results(model)["cases"]
        for arm in _ARMS:
            by_id = {c["case_id"]: c for c in cases
                     if c["arm"] == arm and c["case_id"] in ADJUDICATED}
            # Explicit raise (not assert) so the integrity check survives python -O.
            if len(by_id) != len(ADJUDICATED):
                raise RuntimeError(
                    f"committed results malformed: expected {len(ADJUDICATED)} "
                    f"adjudicated rows for model={model} arm={arm}, got {len(by_id)}")
            table[arm][model] = sum(1 for cid in GOV_UNREP if by_id[cid]["outcome"] in SW)
            numeric[arm][model] = sum(1 for cid in NUMERIC if by_id[cid]["outcome"] in SW)
        hatch = [c for c in cases if c["arm"] == "constrained_hatch"]
        abstain[model] = (sum(1 for c in hatch if c["case_id"] in GOV_UNREP
                              and c["outcome"] == "abstained"), len(GOV_UNREP))
        over[model] = (sum(1 for c in hatch if c["case_id"] in CONTROLS
                           and c["outcome"] == "over_abstain"), len(CONTROLS))
    return {"silent_wrong": table, "n": len(GOV_UNREP),
            "numeric": numeric, "n_numeric": len(NUMERIC),
            "abstain": abstain, "over_abstain": over}


def beat5(ctx: dict, case_id: Optional[str] = None) -> dict:
    s = _sty()
    t0 = time.time()
    t = compute_table1()
    _hr("BEAT 5 | Reproduce it offline -- Table 1 from committed results, no API key")
    cols = list(MODELS)

    def _grid(table: dict) -> None:
        header = ["Arm"] + [_TABLE1_HEADERS[m] for m in cols]
        rows = [header, ["-" * 16] + ["-" * len(_TABLE1_HEADERS[m]) for m in cols]]
        for arm in _ARMS:
            rows.append([_ARM_LABELS[arm]] + [str(table[arm][m]) for m in cols])
        w = [max(len(r[i]) for r in rows) for i in range(len(header))]
        for r in rows:
            print("   " + r[0].ljust(w[0]) + "   "
                  + "   ".join(c.center(w[i + 1]) for i, c in enumerate(r[1:])))

    print(f" silent-wrong (= wrong_sub + omit) / {t['n']} un-representable governing-law asks")
    print(f" {s['DIM']}({', '.join(GOV_UNREP)}){s['0']}")
    print()
    _grid(t["silent_wrong"])
    print()
    print(f" numeric/intent cases, reported separately / {t['n_numeric']} "
          f"(NOT pooled into the gov-law /{t['n']})")
    print(f" {s['DIM']}({', '.join(NUMERIC)}: c16 un-representable + outside the categorical "
          f"guardrail's scope;")
    print(f"  c15 a v4-pro raw/verify error constraining corrects; c17 always correct){s['0']}")
    print()
    _grid(t["numeric"])
    print()
    ab = {m: t["abstain"][m] for m in cols}
    ov = {m: t["over_abstain"][m] for m in cols}
    ab_str = "/".join(f"{ab[m][0]}" for m in cols)
    ov_str = "/".join(f"{ov[m][0]}" for m in cols)
    if all(ab[m] == (6, 6) for m in cols) and all(ov[m] == (0, 3) for m in cols):
        # C13: name the actual six case ids -- "(c01-c08)" would imply 8 cases.
        print(f" Arm D abstains {s['B']}6/6{s['0']} on the un-representable governing-law asks "
              f"({', '.join(GOV_UNREP)}) and over-abstains {s['B']}0/3{s['0']}")
        print(f" on the supported-law controls ({', '.join(CONTROLS)}) -- all four models, "
              f"replayed offline.")
    else:  # pragma: no cover -- committed results changed
        print(f" Arm D abstain per model: {ab_str} (/6); over-abstain: {ov_str} (/3)")
    print()
    print(f" {s['DIM']}(recomputed from the 4 committed gauntlet_results.*.hard.json "
          f"files in {time.time() - t0:.2f}s){s['0']}")
    if case_id:
        _case_drill(ctx, case_id)
    return t


def _case_drill(ctx: dict, case_id: str) -> None:
    s = _sty()
    model = ctx["model"]
    case = _case(case_id)
    print()
    print(s["B"] + f" --- drill-down: case {case_id} ({case.field}, {case.defect_class}) ---" + s["0"])
    print(" prompt:")
    print(_wrap(case.instruction, indent="   > "))
    _kv("representable", str(case.representable), indent=1)
    _kv("expected_correct", str(case.expected_correct), indent=1)
    print()
    print(" outcome per arm x model (from committed results):")
    cols = list(MODELS)
    header = ["arm"] + [_TABLE1_HEADERS[m] for m in cols]
    rows = [header]
    for arm in _ARMS:
        cells = []
        for m in cols:
            rec = next((c for c in _results(m)["cases"]
                        if c["arm"] == arm and c["case_id"] == case_id), None)
            cells.append(f"{rec['outcome'] or '-'}" if rec else "?")
        rows.append([_ARM_LABELS[arm]] + cells)
    w = [max(len(r[i]) for r in rows) for i in range(len(header))]
    for r in rows:
        print("   " + "   ".join(c.ljust(w[i]) for i, c in enumerate(r)))
    print()
    print(f" filled values per arm ({model}, replayed from the committed cassette):")
    for arm in _ARMS:
        r = _arm_result(case_id, model, arm)
        f = r.fields or {}
        val = f.get(case.field)
        raw_field = f"{case.field}Raw"
        extra = f"  {raw_field}={f.get(raw_field)!r}" if f.get(raw_field) else ""
        print(f"   {_ARM_LABELS[arm]:<16} {case.field}={val!r}{extra}   [outcome: {r.outcome or '-'}]")


# ---------------------------------------------------------------------------
# Beat 6 -- close the loop: the audit row for the abstained draft
# ---------------------------------------------------------------------------
def _latest_abstention_row(db: str) -> Optional[dict]:
    for row in get_audit_log(db, limit=100):
        try:
            slot = json.loads(row.get("slot_values") or "{}")
        except json.JSONDecodeError:
            continue
        if row.get("gate_result") == "ESCALATED" and slot.get("reason") == "typed-abstention":
            return row
    return None


def beat6(ctx: dict) -> dict:
    s = _sty()
    db = ctx["db"]
    _hr("BEAT 6 | Close the loop -- the audit row for the abstained draft")
    row = None
    if ctx.get("beat3_audit_id"):
        rows = get_audit_log(db, doc_id=ctx["beat3_audit_id"])
        row = rows[0] if rows else None
    if row is None:
        row = _latest_abstention_row(db)
    if row is None:  # standalone beat 6 with a fresh DB: create the escalation first
        _escalate_c02(ctx)
        row = get_audit_log(db, doc_id=ctx["beat3_audit_id"])[0]

    slot = json.loads(row["slot_values"])
    stored_sha = slot.get("escalation_sha256", "")
    record = {k: v for k, v in slot.items() if k != "escalation_sha256"}
    recomputed = _escalation_sha256(record)
    match = recomputed == stored_sha

    print(f" queried from the real audit DB: {db}")
    print()
    _kv("audit_id", str(row["id"]))
    _kv("timestamp", str(row["timestamp"]))
    _kv("status", s["Y"] + str(row["gate_result"]) + s["0"])
    for a in slot.get("abstentions", []):
        _kv("abstained field", f"{a['field']}  (sentinel {a['sentinel']})")
        _kv("raw captured ask", repr(a.get("raw")))
    _kv("output_path", f"{row.get('output_path')}  (fail-closed: no signable contract)")
    _kv("record sha256", stored_sha)
    verdict = s["G"] + "MATCH" + s["0"] if match else s["R"] + "MISMATCH" + s["0"]
    _kv("sha256 verify", f"recomputed over the stored canonical record -> {verdict}")
    print()
    print(" the escalation is durable: the human reviewer sees the verbatim ask the type")
    print(" could not represent, and the sha256 pins the stored record byte-for-byte.")
    return {"row": row, "slot": slot, "sha_match": match}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
_BEATS = {1: beat1, 3: beat3, 4: beat4, 6: beat6}


def run_all(ctx: dict) -> None:
    beat1(ctx)
    beat2(ctx)
    beat3(ctx)
    beat4(ctx)
    beat5(ctx)
    beat6(ctx)
    s = _sty()
    print()
    print(s["DIM"] + " (demo complete -- every LLM response replayed from committed cassettes; "
          "no API key, no network)" + s["0"])


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m contract_drafting.demo_offline",
        description="Six-beat OFFLINE demo: silent substitution -> typed abstention -> "
                    "escalation -> audit. Replays committed cassettes; no API key needed.")
    # A-M4: the subcommand is OPTIONAL and defaults to 'all', so the paper's verbatim
    # command `python -m contract_drafting.demo_offline` runs the full six-beat demo
    # (it used to exit 2 on the missing positional). All subcommands are unchanged.
    p.add_argument("command", nargs="?", default="all",
                   choices=["beat", "explain", "table1", "all"],
                   help="beat N | explain | table1 | all (default: all)")
    p.add_argument("number", nargs="?", type=int, choices=range(1, 7),
                   help="beat number (1-6), required with 'beat'")
    p.add_argument("--model", default="gpt-5.5", choices=list(MODELS),
                   help="which model's committed cassette/results to replay (default gpt-5.5)")
    p.add_argument("--db", default=DEFAULT_DB,
                   help=f"audit DB path (default: the demo-scoped DB, {DEFAULT_DB})")
    p.add_argument("--out-dir", default=None,
                   help="directory for rendered .docx output (default: data/demo_drafts/, "
                        "created on demand -- never the production data/drafts/)")
    p.add_argument("--field", default="governingLaw",
                   help="explain: the .cto field (governingLaw | entityType | "
                        "disclosingEntityType | receivingEntityType | disputeForum)")
    p.add_argument("--asked", default="laws of Scotland",
                   help="explain: the asked value to check for representability")
    p.add_argument("--case", dest="case_id", default=None,
                   help="table1/beat 5: drill into one case id (e.g. c02)")
    args = p.parse_args(argv)

    ctx = {"model": args.model, "db": args.db, "out_dir": args.out_dir}

    # RT6: validate --case up front -> one friendly line, nonzero rc, no traceback.
    if args.case_id is not None:
        try:
            _case(args.case_id)
        except KeyError as e:
            print(f"ERROR: {e.args[0]}")
            return 2

    # C6: validate --field up front for the explain paths (explain / beat 2) -> one
    # friendly line listing the explainable fields, rc 2, no traceback.
    if args.command == "explain" or (args.command == "beat" and args.number == 2):
        from contract_drafting.schema_validator import load_abstain_policies
        _valid_fields = set(load_abstain_policies(_TEMPLATE)) | set(_FIELD_ALIASES)
        if args.field not in _valid_fields:
            print(f"ERROR: unknown --field {args.field!r}; explainable fields: "
                  f"{', '.join(sorted(_valid_fields))}")
            return 2

    try:
        if args.command == "all":
            run_all(ctx)
        elif args.command == "table1":
            beat5(ctx, case_id=args.case_id)
        elif args.command == "explain":
            beat2(ctx, field=args.field, asked=args.asked)
        else:  # beat N
            if args.number is None:
                p.error("'beat' requires a number 1-6 (e.g. beat 1)")
            if args.number == 2:
                beat2(ctx, field=args.field, asked=args.asked)
            elif args.number == 5:
                beat5(ctx, case_id=args.case_id)
            else:
                _BEATS[args.number](ctx)
    except demo.GauntletCacheMiss as e:
        print(f"REPLAY FAILED (offline cassette miss): {e}")
        return 2
    except (sqlite3.Error, OSError, json.JSONDecodeError, RuntimeError, KeyError) as e:
        # RT3/C6: missing/corrupt committed artifacts, an unwritable audit DB,
        # Table 1 denominator drift (RuntimeError), or a malformed results file
        # (KeyError) are demo PREREQUISITE failures -- one line, distinct rc, no raw
        # traceback. (GauntletCacheMiss subclasses RuntimeError but is caught above.)
        print(f"DEMO PREREQ FAILED: {e}")
        return 3
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
