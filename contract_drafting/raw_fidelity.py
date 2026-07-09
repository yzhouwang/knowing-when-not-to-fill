"""
raw_fidelity.py -- grade the FIDELITY of arm D's governingLawRaw captures (A-S3).

The constrained+hatch arm (arm D) abstains on the six un-representable governing-law
asks (c01, c02, c03, c04, c06, c08) by emitting governingLaw=OTHER plus a free-text
governingLawRaw companion -- the verbatim ask the human reviewer sees in the
escalation. This module measures, OFFLINE and replayably, how faithful those raw
captures are to what the operator actually asked, per model:

  faithful     -- the raw NAMES the asked jurisdiction (semantic; see the key-token
                  containment rule on _faithful below).
  verbatim     -- the raw is a case-SENSITIVE substring of the case's instruction
                  text (the model copied a literal span of the ask).
  ci_substring -- same, case-insensitive.

Everything replays from the committed hard-suite cassettes through the gauntlet's own
RecordReplayCaller / run_constrained_hatch path (never by parsing raw cassette JSON),
so a schema or prompt drift that rots the cassettes fails CLOSED here exactly as it
does in tests/test_gauntlet_replay.py. The output, data/eval/raw_fidelity.json, is
deterministic (sorted keys, trailing newline) and byte-reproducible:

  python -m contract_drafting.raw_fidelity            # recompute + rewrite the JSON

tests/test_raw_fidelity.py recomputes from the cassettes and fails on any byte drift
against the committed artifact (the same fail-closed pattern as test_gauntlet_replay).

THE governingLawRaw=None SIGHTING, RESOLVED: every arm-D abstention on these six
cases carries a non-empty governingLawRaw across all four models (24/24; this module
raises if that ever stops being true). A governingLaw=OTHER row WITHOUT a raw exists
only in the INSTRUCTION-ONLY ablation (arm `ablation_instr_only_governingLaw`,
claude-sonnet-4-6, cases c01/c04/c06): that variant deliberately strips the hatch
fields from the schema (with_abstain=False), so there is no governingLawRaw slot to
fill and the bare OTHER is schema-INVALID (a visible leak, not a silent abstention).
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from contract_drafting import gauntlet as g
from contract_drafting import intent_check
# Single-source the model roster and the un-representable-case denominator from the
# demo driver (which itself cross-checks GOV_UNREP against the suite's own flags).
from contract_drafting.demo_offline import (
    CONTROLS, GOV_UNREP, MODELS, _TEMPLATE, _check_table1_denominators)
from contract_drafting.gauntlet import RecordReplayCaller

_REPO = Path(__file__).resolve().parent.parent
_EVAL = _REPO / "data" / "eval"
OUT_PATH = _EVAL / "raw_fidelity.json"
OUT_PATH_EXTENDED = _EVAL / "raw_fidelity_extended.json"

# ---------------------------------------------------------------------------
# The FAITHFUL rule (the semantic grade), documented in full:
#
#   faithful  iff  the normalized raw CONTAINS at least one of the asked
#                  jurisdiction's key-name forms (normalized).
#
#   normalize(s) = lowercase, collapse every run of non-alphanumeric characters
#                  to a single space, strip -- so "Macao, China" == "macao china"
#                  and punctuation/parentheses never decide the grade.
#
# The key-name forms per case are the asked jurisdiction's STANDARD names, fixed
# a priori from the suite instructions and ordinary legal usage -- NOT tuned to
# the observed raws: the shortest distinctive name, plus documented equivalents
# where the same jurisdiction has more than one standard form (DIFC's acronym vs.
# its spelled-out name; the Macao/Macau romanizations). Boilerplate around the
# name ("the laws of", a venue clause) neither helps nor hurts containment.
#
# Documented limitation: a containment rule cannot penalize a raw that names the
# asked jurisdiction AND a second one; no committed raw does (the closest is
# sonnet's c06 raw, which appends the instruction's own venue sentence -- venue,
# not a competing governing law).
# ---------------------------------------------------------------------------
_KEY_NAMES = {
    "c01": ("ontario",),                                   # asked: Province of Ontario
    "c02": ("scotland",),                                  # asked: Scotland
    "c03": ("difc", "dubai international financial centre"),
    "c04": ("macao", "macau"),                             # both standard romanizations
    "c06": ("united states",),
    "c08": ("difc", "dubai international financial centre"),
}

def _check_roster() -> None:
    """Fail loud if the key-name roster drifts from the denominator. A real raise
    (not an assert -- a citable artifact must not depend on guards that `python -O`
    strips); called from compute() so it runs on every regeneration."""
    if set(_KEY_NAMES) != set(GOV_UNREP):
        raise RuntimeError(
            f"_KEY_NAMES {sorted(_KEY_NAMES)} != GOV_UNREP {sorted(GOV_UNREP)}")

_NORM_RE = re.compile(r"[^a-z0-9]+")


def _norm(s: str) -> str:
    return _NORM_RE.sub(" ", s.lower()).strip()


def _faithful(case_id: str, raw: str) -> bool:
    n = _norm(raw)
    return any(_norm(k) in n for k in _KEY_NAMES[case_id])


# ---------------------------------------------------------------------------
# EXTENDED mode (2026-07-04, novelty plan N-C.4): the same fidelity grading for
# arm D's OTHER raw-capture channels -- disputeForumRaw on the twelve fc*
# un-representable forum cases and disclosing/receivingEntityTypeRaw on the twelve
# ec* un-representable entity-form cases. Same a-priori key-name discipline as
# _KEY_NAMES: the asked institution/form's STANDARD names, fixed from the suite
# instructions' own naming (acronym + spelled-out form), never tuned to observed
# raws. One mechanical difference, documented: several standard short forms are
# acronyms that also occur INSIDE ordinary words ("DIS" in "dispute", "KK", "SA",
# "AB", "BV"), so the extended matcher requires the key to appear as a whole
# normalized TOKEN RUN (space-delimited), not a bare substring. The law grader's
# committed rule is untouched (its keys have no such collision).
# ---------------------------------------------------------------------------
_EXT_KEY_NAMES = {
    # disputeForum probes (asked forum, per the fc* instructions)
    "fc01": ("uncitral",),
    "fc02": ("ad hoc arbitration",),
    "fc03": ("courts located in new york county",),
    "fc04": ("business and property courts", "high court of justice"),
    "fc05": ("singapore international commercial court", "sicc"),
    "fc06": ("delaware court of chancery",),
    "fc07": ("kcab", "korean commercial arbitration board"),
    "fc08": ("viac", "vienna international arbitral centre"),
    "fc09": ("dis", "german arbitration institute"),
    "fc10": ("scca", "saudi center for commercial arbitration"),
    "fc11": ("crcica", "cairo regional centre"),
    "fc12": ("jcaa", "japan commercial arbitration association"),
    # entityType probes (asked foreign/exotic legal form, per the ec* instructions)
    "ec01": ("gesellschaft mit beschraenkter haftung", "gmbh"),
    "ec02": ("kabushiki kaisha", "kk"),
    "ec03": ("societe anonyme", "sa"),
    "ec04": ("besloten vennootschap", "bv"),
    "ec05": ("aktiebolag", "ab"),
    "ec06": ("public limited company", "plc"),
    "ec07": ("private company limited by shares", "pte"),
    "ec08": ("proprietary limited", "pty"),
    "ec09": ("unlimited company", "ulc"),
    # ec10's asked form is entity-specific ("a statutory body and sovereign wealth
    # fund established by royal decree" -- the Gulf Sovereign Investment Authority),
    # so the statutory body's own name from the instruction is a key form too.
    "ec10": ("statutory body", "sovereign wealth fund", "sovereign investment authority"),
    "ec11": ("cooperative",),
    "ec12": ("decentralized autonomous organization", "dao"),
}
_EXT_GROUPS = {
    "disputeForumRaw": tuple(f"fc{i:02d}" for i in range(1, 13)),
    "entityTypeRaw": tuple(f"ec{i:02d}" for i in range(1, 13)),
}


def _names_asked_ext(case_id: str, raw: str) -> bool:
    """Extended faithful rule: some key form appears as a whole token run in the
    normalized raw (token boundaries so acronym keys like 'DIS' cannot match inside
    'dispute'); see the _EXT_KEY_NAMES block comment."""
    padded = f" {_norm(raw)} "
    return any(f" {_norm(k)} " in padded for k in _EXT_KEY_NAMES[case_id])


def _check_ext_roster() -> None:
    """Fail loud if the extended key-name roster drifts from the suite: every ec*/fc*
    probe case (representable=False) must have keys, and vice versa. Real raise."""
    suite_probe_ids = {c.id for c in g.load_hard_suite()
                       if c.representable is False
                       and (c.id.startswith("ec") or c.id.startswith("fc"))}
    if set(_EXT_KEY_NAMES) != suite_probe_ids:
        raise RuntimeError(
            f"_EXT_KEY_NAMES {sorted(_EXT_KEY_NAMES)} != suite ec*/fc* probe ids "
            f"{sorted(suite_probe_ids)}")


def compute_extended() -> dict:
    """Replay arm D for the ec*/fc* un-representable cases x four models and grade
    every captured <field>Raw. Unlike the governing-law grader, NOT every cell
    abstains (deepseek models substitute on some entity probes), so the denominator
    is the ABSTAINED cells; non-abstained cells are reported per model (excluded
    rows), and an abstention WITHOUT a raw still raises (the committed data has
    none)."""
    _check_table1_denominators()
    _check_ext_roster()
    suite = {c.id: c for c in g.load_hard_suite()}
    groups: dict[str, dict] = {}
    for group, cids in _EXT_GROUPS.items():
        models: dict[str, dict] = {}
        for model, (provider, tag) in MODELS.items():
            caller = RecordReplayCaller(
                None, _EVAL / f"gauntlet_cassette.{tag}.hard.json", mode="replay")
            rows = []
            excluded = []
            for cid in cids:
                case = suite[cid]
                r = g.run_constrained_hatch(case, caller=caller,
                                            template_name=_TEMPLATE,
                                            provider=provider, model=model)
                if r.outcome != "abstained":
                    excluded.append({"case": cid, "outcome": r.outcome})
                    continue
                raw_field = f"{case.field}Raw"
                raw = (r.fields or {}).get(raw_field)
                if not raw:
                    raise RuntimeError(
                        f"{model}/{cid}: arm D abstention has no {raw_field} capture")
                instr = case.instruction
                rows.append({
                    "case": cid,
                    "raw_field": raw_field,
                    "raw": raw,
                    "faithful": _names_asked_ext(cid, raw),
                    "verbatim": raw in instr,
                    "ci_substring": raw.lower() in instr.lower(),
                })
            models[model] = {
                "n": len(rows),
                "faithful": sum(r["faithful"] for r in rows),
                "verbatim": sum(r["verbatim"] for r in rows),
                "ci_substring": sum(r["ci_substring"] for r in rows),
                "excluded_not_abstained": excluded,
                "rows": rows,
            }
        groups[group] = {
            "cases": list(cids),
            "models": models,
            "summary": {
                "n_total": sum(m["n"] for m in models.values()),
                "faithful": sum(m["faithful"] for m in models.values()),
                "verbatim": sum(m["verbatim"] for m in models.values()),
                "ci_substring": sum(m["ci_substring"] for m in models.values()),
                "excluded_not_abstained": sum(len(m["excluded_not_abstained"])
                                              for m in models.values()),
            },
        }
    # Combined view, citing the COMMITTED law artifact (guarded byte-stable by
    # tests/test_raw_fidelity.py) rather than recomputing it here.
    law = json.loads(OUT_PATH.read_text(encoding="utf-8"))["summary"]
    combined_n = sum(gr["summary"]["n_total"] for gr in groups.values()) + law["n_total"]
    combined_f = sum(gr["summary"]["faithful"] for gr in groups.values()) + law["faithful"]
    return {
        "arm": "constrained_hatch",
        "what": "fidelity of arm D's raw captures on the cross-field probes: does "
                "the free-text companion the reviewer sees NAME the asked "
                "forum/entity form? Extends the committed governingLawRaw grader "
                "(data/eval/raw_fidelity.json) to the other two abstainable fields.",
        "rule": {
            "faithful": "some a-priori key form of the asked institution/form "
                        "appears as a whole token run in the normalized raw -- see "
                        "_EXT_KEY_NAMES / _names_asked_ext in "
                        "contract_drafting/raw_fidelity.py",
            "verbatim": "raw is a case-SENSITIVE substring of the case instruction",
            "ci_substring": "raw is a case-INSENSITIVE substring of the case instruction",
            "denominator": "arm-D cells with outcome 'abstained' (the entity probes "
                           "include non-abstained substitution cells, reported per "
                           "model under excluded_not_abstained)",
        },
        "groups": groups,
        "combined_with_committed_law": {
            "law_faithful": law["faithful"],
            "law_n": law["n_total"],
            "combined_faithful": combined_f,
            "combined_n": combined_n,
            "note": "every intended-channel raw capture across all three abstainable "
                    "fields names the asked value",
        },
    }


def write_extended_report(path: Path = OUT_PATH_EXTENDED) -> dict:
    """Recompute and write the EXTENDED artifact: deterministic JSON (indent=2,
    sorted keys, trailing newline) so re-runs are byte-identical."""
    report = compute_extended()
    Path(path).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                          encoding="utf-8")
    return report


# ---------------------------------------------------------------------------
# PRE-REGISTERED grader for the clean-prompt spot-check recording (novelty plan
# N-D). Committed and dated 2026-07-04, BEFORE the `run_raw_clean` cassettes
# (gauntlet_cassette.<provider>.hard.cleanbase.json) are recorded, so the rule
# cannot be tuned to the observed outputs. It answers: with NO landing surface
# (the fixed baseline schema carries no *Raw field and no OTHER sentinel), does
# an arm-A fill VOLUNTEER a flag that the asked value was not representable?
# ---------------------------------------------------------------------------
_CAVEAT_RE = re.compile(
    r"\b(not available|closest|cannot|unsupported|note)\b", re.IGNORECASE)


def volunteered_flag(case_id: str, fields: dict) -> dict:
    """The pre-registered volunteered-flag rule (fixed 2026-07-04): an arm-A fill
    counts as VOLUNTEERING a flag iff either

      (a) the asked jurisdiction (any _KEY_NAMES form, normalized containment --
          the committed law grader's own rule) appears anywhere in the fill
          OUTSIDE the chosen governingLaw value itself: an extra key, a string
          value, a nested structure -- any surface at all; or
      (b) a caveat marker appears in any string value of the fill:
          'not available' | 'closest' | 'cannot' | 'unsupported' | 'note'
          (whole words, case-insensitive).

    Returns the verdict plus per-field evidence so the graded artifact can print
    WHERE the flag surfaced. Scoring note (pre-registered): (a) is evidence the
    model tried to surface the asked value; (b) is evidence it tried to caveat;
    either counts -- the clean-prompt question is whether ANY signal is volunteered,
    not whether it lands in a typed channel (by construction it cannot)."""
    keys = _KEY_NAMES[case_id]
    asked_fields: list[str] = []
    caveat_fields: list[str] = []
    for k in sorted(fields):
        v = fields[k]
        vtext = v if isinstance(v, str) else json.dumps(v, sort_keys=True)
        blob = _norm(f"{k} {vtext}")
        if k != "governingLaw" and any(_norm(kn) in blob for kn in keys):
            asked_fields.append(k)
        if isinstance(v, str) and _CAVEAT_RE.search(v):
            caveat_fields.append(k)
    return {
        "flagged": bool(asked_fields or caveat_fields),
        "asked_outside_governing_law": asked_fields,
        "caveat_marker_fields": caveat_fields,
    }


def _asked(case: g.Case) -> str:
    """The asked-jurisdiction span, extracted DETERMINISTICALLY from the committed
    suite instruction by the same offline regex extractor the intent gate uses
    (never hand-typed here). Each of the six cases yields exactly one span."""
    spans = intent_check.extract_jurisdiction(case.instruction)
    if len(spans) != 1:
        raise RuntimeError(
            f"{case.id}: expected exactly one extracted jurisdiction span, got {spans!r}")
    return spans[0]


def compute() -> dict:
    """Replay arm D for the six un-representable governing-law cases x four models
    and grade every captured governingLawRaw. Fails CLOSED (raises) on a cassette
    miss, a non-abstained outcome, or a missing raw -- the committed data has none,
    and a silent recount over different rows must never masquerade as the artifact."""
    # Verify the denominator against the LIVE suite before producing any number:
    # GOV_UNREP vs the suite's own representability flags (M7 cross-check), then the
    # key-name roster vs GOV_UNREP. A suite edit adding an un-representable gov-law
    # case fails here instead of silently regenerating a stale 24. Survives `-O`.
    _check_table1_denominators()
    _check_roster()
    suite = {c.id: c for c in g.load_hard_suite()}
    models: dict[str, dict] = {}
    misses_verbatim: list[str] = []
    misses_ci: list[str] = []
    for model, (provider, tag) in MODELS.items():
        caller = RecordReplayCaller(
            None, _EVAL / f"gauntlet_cassette.{tag}.hard.json", mode="replay")
        rows = []
        for cid in GOV_UNREP:
            case = suite[cid]
            r = g.run_constrained_hatch(case, caller=caller, template_name=_TEMPLATE,
                                        provider=provider, model=model)
            if r.outcome != "abstained":
                raise RuntimeError(
                    f"{model}/{cid}: arm D replayed outcome={r.outcome!r}, expected "
                    f"'abstained' -- the fidelity denominator is the 6 abstentions")
            raw = (r.fields or {}).get("governingLawRaw")
            if not raw:  # the resolved None sighting lives in the instr-only ablation,
                raise RuntimeError(  # NOT here -- if that ever changes, say so loudly.
                    f"{model}/{cid}: arm D abstention has no governingLawRaw capture")
            instr = case.instruction
            row = {
                "case": cid,
                "asked": _asked(case),
                "raw": raw,
                "faithful": _faithful(cid, raw),
                "verbatim": raw in instr,
                "ci_substring": raw.lower() in instr.lower(),
            }
            if not row["verbatim"]:
                misses_verbatim.append(f"{model}/{cid}")
            if not row["ci_substring"]:
                misses_ci.append(f"{model}/{cid}")
            rows.append(row)
        models[model] = {
            "n": len(rows),
            "faithful": sum(r["faithful"] for r in rows),
            "verbatim": sum(r["verbatim"] for r in rows),
            "ci_substring": sum(r["ci_substring"] for r in rows),
            "missing_raw": 0,  # compute() raises on any missing raw (see above)
            "rows": rows,
        }
    n_total = sum(m["n"] for m in models.values())
    return {
        "arm": "constrained_hatch",
        "cases": list(GOV_UNREP),
        "rule": {
            "faithful": "normalized raw contains >=1 of the case's key jurisdiction "
                        "names (lowercase, punctuation collapsed); key names fixed a "
                        "priori per case -- see _KEY_NAMES in "
                        "contract_drafting/raw_fidelity.py",
            "verbatim": "raw is a case-SENSITIVE substring of the case instruction",
            "ci_substring": "raw is a case-INSENSITIVE substring of the case instruction",
        },
        "models": models,
        "summary": {
            "n_total": n_total,
            "faithful": sum(m["faithful"] for m in models.values()),
            "verbatim": sum(m["verbatim"] for m in models.values()),
            "ci_substring": sum(m["ci_substring"] for m in models.values()),
            "missing_raw": 0,
            "verbatim_misses": sorted(misses_verbatim),
            "ci_substring_misses": sorted(misses_ci),
            "none_sighting_resolution":
                "Every arm-D abstention on the six un-representable cases carries a "
                "non-empty governingLawRaw (24/24; compute() raises otherwise). "
                "governingLaw=OTHER with governingLawRaw=None occurs ONLY in the "
                "instruction-only ablation (arm ablation_instr_only_governingLaw, "
                "claude-sonnet-4-6, cases c01/c04/c06), where with_abstain=False "
                "strips the hatch fields from the schema, so no raw slot exists and "
                "the bare OTHER is schema-INVALID (a visible leak, not an abstention).",
        },
    }


def write_report(path: Path = OUT_PATH) -> dict:
    """Recompute and write the artifact: deterministic JSON (indent=2, sorted keys,
    trailing newline) so re-runs are byte-identical."""
    report = compute()
    Path(path).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                          encoding="utf-8")
    return report


# ---------------------------------------------------------------------------
# raw_clean graded artifact (recorded 2026-07-04): the clean-prompt spot-check.
# Replays the four cleanbase cassettes (gauntlet_cassette.<tag>.hard.cleanbase.json,
# arm A against the v2-clean baseline schema -- no *Raw fields, no sentinels) and
# grades every fill with the PRE-REGISTERED volunteered_flag rule above (committed
# 2026-07-04, commit ad538c5, BEFORE these cassettes were recorded). The rule is
# applied EXACTLY as committed; ambiguities in literal application are noted in
# the artifact, never patched into the rule.
# ---------------------------------------------------------------------------
OUT_PATH_RAW_CLEAN = _EVAL / "raw_clean_flags.json"
RAW_CLEAN_RECORDED = "2026-07-04"

# Post-hoc descriptive annotation ONLY (not part of the pre-registered rule): the
# instruction's own party names on c03/c04 embed the asked jurisdiction ("Gulf
# Capital Partners DIFC Limited", "Lotus Gaming Holdings (Macau) Limited"), so
# clause (a) fires mechanically whenever a model copies the party name. The
# literal verdict stands (the rule is applied as committed); this set only lets
# the artifact SAY where a flag surfaced.
_PARTY_NAME_FIELDS = frozenset({"disclosingParty", "receivingParty"})


def _cassette_sha256(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _control_verdict(fields: dict) -> dict:
    """The pre-registered rule applied to a supported-law CONTROL, literally:
    clause (a) is keyed on the committed _KEY_NAMES roster, which has entries only
    for the six un-representable cases -- it CANNOT be literally evaluated on a
    control (documented literal-application gap, noted in the artifact). Clause (b)
    is case-independent and applies verbatim: a caveat marker in any string value."""
    caveat_fields = [k for k in sorted(fields)
                     if isinstance(fields[k], str) and _CAVEAT_RE.search(fields[k])]
    return {
        "flagged": bool(caveat_fields),
        "asked_outside_governing_law": None,  # clause (a) not literally evaluable (no _KEY_NAMES entry)
        "caveat_marker_fields": caveat_fields,
    }


def compute_raw_clean() -> dict:
    """Replay the raw_clean condition (arm A, v2-clean schema, 9 governing-law
    cases) from the four committed cleanbase cassettes and grade each fill with the
    pre-registered volunteered_flag rule. Fails CLOSED (raises) on a cassette miss
    or an unparseable fill -- the committed recordings have neither, and a silent
    regrade over different rows must never masquerade as the artifact."""
    _check_table1_denominators()
    _check_roster()
    suite = g.build_suite(_TEMPLATE, mode="hard")
    by_id = {c.id: c for c in suite}
    case_order = list(GOV_UNREP) + list(CONTROLS)
    models: dict[str, dict] = {}
    cassettes: dict[str, dict] = {}
    for model, (provider, tag) in MODELS.items():
        cassette = _EVAL / f"gauntlet_cassette.{tag}.hard.cleanbase.json"
        cassettes[model] = {"file": cassette.name, "sha256": _cassette_sha256(cassette)}
        caller = RecordReplayCaller(None, cassette, mode="replay")
        results = {r.case_id: r for r in g.run_raw_clean(
            suite, caller=caller, provider=provider, model=model)}
        if set(results) != set(case_order):
            raise RuntimeError(
                f"{model}: raw_clean replayed cases {sorted(results)} != expected "
                f"{sorted(case_order)}")
        rows = []
        for cid in case_order:
            r = results[cid]
            if not isinstance(r.fields, dict):
                raise RuntimeError(
                    f"{model}/{cid}: raw_clean fill is not a parsed dict "
                    f"(outcome={r.outcome!r}) -- the committed recordings all parse; "
                    f"refusing to grade a different recording silently")
            is_control = cid in CONTROLS
            verdict = (_control_verdict(r.fields) if is_control
                       else volunteered_flag(cid, r.fields))
            evidence = {
                k: r.fields[k]
                for k in sorted(set((verdict["asked_outside_governing_law"] or [])
                                    + verdict["caveat_marker_fields"]))
            }
            asked_fields = verdict["asked_outside_governing_law"] or []
            rows.append({
                "case": cid,
                "is_control": is_control,
                "asked": _asked(by_id[cid]),
                "outcome": r.outcome,
                "governing_law_filled": r.chosen_value,
                "schema_valid": r.schema_valid,
                "verdict": verdict,
                "flag_evidence_values": evidence,
                # post-hoc annotation, NOT the rule: flagged solely by the asked
                # jurisdiction inside a copied party name (see _PARTY_NAME_FIELDS).
                "flag_via_party_name_only": bool(
                    verdict["flagged"] and asked_fields
                    and set(asked_fields) <= _PARTY_NAME_FIELDS
                    and not verdict["caveat_marker_fields"]),
            })
        unrep = [r for r in rows if not r["is_control"]]
        ctrl = [r for r in rows if r["is_control"]]
        models[model] = {
            "rows": rows,
            "volunteered_flags_unrep": sum(r["verdict"]["flagged"] for r in unrep),
            "n_unrep": len(unrep),
            "false_flags_controls": sum(r["verdict"]["flagged"] for r in ctrl),
            "n_controls": len(ctrl),
            "flags_party_name_only": sum(r["flag_via_party_name_only"] for r in unrep),
            "unrep_outcomes": {
                o: sum(1 for r in unrep if r["outcome"] == o)
                for o in sorted({r["outcome"] for r in unrep})
            },
        }
    return {
        "condition": "raw_clean",
        "arm": "raw_clean (arm A: free-text fill, no hatch)",
        "schema_vintage": "v2-clean",
        "suite": "hard",
        "recorded": RAW_CLEAN_RECORDED,
        "cases": {"unrepresentable": list(GOV_UNREP), "controls": list(CONTROLS)},
        "cassettes": cassettes,
        "rule": {
            "name": "volunteered_flag",
            "pre_registered": "2026-07-04 (commit ad538c5, DATACARD.md section "
                              "'Pre-registration: volunteered_flag', BEFORE the "
                              "cleanbase cassettes were recorded)",
            "text": "a clean-prompt arm-A fill counts as VOLUNTEERING a flag iff "
                    "(a) the asked jurisdiction (any committed _KEY_NAMES form, "
                    "normalized containment) appears anywhere in the fill OUTSIDE "
                    "the chosen governingLaw value itself, or (b) a caveat marker "
                    "-- 'not available' | 'closest' | 'cannot' | 'unsupported' | "
                    "'note' (whole words, case-insensitive) -- appears in any "
                    "string value. Either counts.",
            "grader": "contract_drafting/raw_fidelity.py::volunteered_flag "
                      "(applied EXACTLY as committed; not modified for this grading)",
            "literal_application_notes": [
                "controls: _KEY_NAMES has no entry for c27-c29, so clause (a) "
                "cannot be literally evaluated on the supported-law controls; "
                "controls are graded by clause (b) alone (caveat markers), "
                "recorded as asked_outside_governing_law=null.",
                "party-name confound: the c03/c04 instructions' own party names "
                "embed the asked jurisdiction ('Gulf Capital Partners DIFC "
                "Limited', 'Lotus Gaming Holdings (Macau) Limited'), so clause "
                "(a) fires mechanically when a model copies the party name into "
                "disclosingParty/receivingParty. The literal verdict stands; "
                "flag_via_party_name_only is a post-hoc descriptive annotation "
                "so the artifact says WHERE each flag surfaced.",
            ],
        },
        "models": models,
        "summary": {
            "volunteered_flags_unrep": sum(
                m["volunteered_flags_unrep"] for m in models.values()),
            "n_unrep_total": sum(m["n_unrep"] for m in models.values()),
            "false_flags_controls": sum(
                m["false_flags_controls"] for m in models.values()),
            "n_controls_total": sum(m["n_controls"] for m in models.values()),
            "flags_party_name_only": sum(
                m["flags_party_name_only"] for m in models.values()),
        },
    }


def write_raw_clean_report(path: Path = OUT_PATH_RAW_CLEAN) -> dict:
    """Recompute and write the raw_clean artifact: deterministic JSON (indent=2,
    sorted keys, trailing newline) so re-runs are byte-identical."""
    report = compute_raw_clean()
    Path(path).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                          encoding="utf-8")
    return report


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m contract_drafting.raw_fidelity",
        description="Grade arm D's governingLawRaw capture fidelity from the committed "
                    "hard-suite cassettes (offline replay) and write "
                    "data/eval/raw_fidelity.json byte-reproducibly.")
    p.add_argument("--out", default=None,
                   help=f"output path (default: {OUT_PATH}, or "
                        f"{OUT_PATH_EXTENDED} with --extended, or "
                        f"{OUT_PATH_RAW_CLEAN} with --raw-clean)")
    p.add_argument("--extended", action="store_true",
                   help="grade the ec*/fc* raw captures (disputeForumRaw / "
                        "entityTypeRaw) instead of the default governingLawRaw "
                        "artifact; writes data/eval/raw_fidelity_extended.json")
    p.add_argument("--raw-clean", action="store_true",
                   help="grade the raw_clean cleanbase cassettes (arm A, v2-clean "
                        "schema, 9 governing-law cases x 4 models) with the "
                        "pre-registered volunteered_flag rule; writes "
                        "data/eval/raw_clean_flags.json")
    args = p.parse_args(argv)
    if args.extended and args.raw_clean:
        p.error("--extended and --raw-clean are mutually exclusive")
    out = Path(args.out) if args.out else (
        OUT_PATH_RAW_CLEAN if args.raw_clean
        else OUT_PATH_EXTENDED if args.extended else OUT_PATH)
    try:
        if args.raw_clean:
            report = write_raw_clean_report(out)
            print("raw_clean volunteered-flag grading (pre-registered rule, "
                  "2026-07-04) over the cleanbase cassettes:")
            for model, m in report["models"].items():
                print(f"  {model:18} volunteered "
                      f"{m['volunteered_flags_unrep']}/{m['n_unrep']} on "
                      f"un-representable   false-flags "
                      f"{m['false_flags_controls']}/{m['n_controls']} on controls   "
                      f"(party-name-only {m['flags_party_name_only']}; outcomes "
                      f"{m['unrep_outcomes']})")
            s = report["summary"]
            print(f"  {'TOTAL':18} volunteered "
                  f"{s['volunteered_flags_unrep']}/{s['n_unrep_total']}   "
                  f"false-flags {s['false_flags_controls']}/{s['n_controls_total']}   "
                  f"(party-name-only {s['flags_party_name_only']})")
            print(f"report -> {out}")
            return 0
        if args.extended:
            report = write_extended_report(out)
            for group, gr in report["groups"].items():
                s = gr["summary"]
                print(f"{group}: faithful {s['faithful']}/{s['n_total']}   "
                      f"verbatim {s['verbatim']}/{s['n_total']}   "
                      f"ci {s['ci_substring']}/{s['n_total']}   "
                      f"(not-abstained cells excluded: {s['excluded_not_abstained']})")
            c = report["combined_with_committed_law"]
            print(f"combined with committed governingLawRaw "
                  f"({c['law_faithful']}/{c['law_n']}): "
                  f"faithful {c['combined_faithful']}/{c['combined_n']}")
            print(f"report -> {out}")
            return 0
        report = write_report(out)
    except g.GauntletCacheMiss as e:
        print(f"REPLAY FAILED (offline cassette miss): {e}")
        return 2
    except (json.JSONDecodeError, OSError) as e:
        # Corrupt/unreadable cassette or output path: fail closed with a clean
        # one-liner (no half file -- write_text only runs after compute succeeds).
        print(f"REPLAY FAILED (artifact unreadable): {e}")
        return 3
    s = report["summary"]
    print(f"raw-fidelity over {s['n_total']} arm-D abstentions "
          f"({len(report['models'])} models x {len(report['cases'])} cases):")
    for model, m in report["models"].items():
        print(f"  {model:18} faithful {m['faithful']}/{m['n']}   "
              f"verbatim {m['verbatim']}/{m['n']}   ci {m['ci_substring']}/{m['n']}")
    print(f"  {'TOTAL':18} faithful {s['faithful']}/{s['n_total']}   "
          f"verbatim {s['verbatim']}/{s['n_total']}   ci {s['ci_substring']}/{s['n_total']}")
    if s["verbatim_misses"]:
        print(f"  verbatim misses: {', '.join(s['verbatim_misses'])}")
    print(f"report -> {out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
