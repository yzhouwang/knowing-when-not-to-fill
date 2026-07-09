"""
mined_findings.py -- the four mined-result artifacts (novelty plan N-C, 2026-07-04).

Every number the paper's mined-results additions cite is recomputed here OFFLINE from
the committed hard-suite cassettes through the gauntlet's own arm implementations
(run_raw / run_verify_reject / run_constrained / run_constrained_hatch via
RecordReplayCaller) -- never by parsing raw cassette JSON -- so a schema or prompt
drift that rots the cassettes fails CLOSED exactly as it does in
tests/test_gauntlet_replay.py. Four artifacts, each deterministic (indent=2, sorted
keys, trailing newline) and byte-reproducible:

  data/eval/baseline_leak.json   -- the definitions-block schema leak in the baseline
      arms (A/B/C were prompted with a schema whose NDAData definition still carried
      the *Raw hatch fields; see schema_validator._strip_hatch): per model x arm, how
      often the fill carries a non-empty governingLawRaw NAMING the asked jurisdiction
      on the six un-representable governing-law cases, the same count on the
      supported-law controls (discriminativeness), and the counterfactual silent-wrong
      (silent-wrong cases minus flagged cases) -- pooled raw 23/24->5/24,
      verify-reject 24/24->5/24, constrained 24/24->15/24.

  data/eval/forced_fill.json     -- the disputeForum forced-fill decomposition over
      the 41 non-forum cases (57 minus the 16 fc* disputeForum cases): under the
      OpenAI strict massage gpt-5.5 is grammar-forced to fill disputeForum on 41/41;
      with the sentinel available it chooses OTHER_FORUM 32 vs concrete 9; without it
      (arm C) the same forced fills become 41/41 concrete confabulations. Pooled
      unrequested concrete inventions: arm A 39/164, arm C 67/164, arm D 13/164.

  data/eval/directionality.json  -- substitution directionality on the six
      un-representable governing-law cases pooled over the no-abstention arms
      (A/B/C): every filled wrong-jurisdiction value lands on the asked
      jurisdiction's in-enum parent/sibling/constituent when one exists (44/44);
      Ontario -- the one probe with NO in-enum relative (the 65-member Jurisdiction
      enum has no Canadian entry) -- scatters (9/12 omit + 3 divergent cells). Plus
      the entityType folk matrix over the 12 ec* probes: 10/12 probes get >=3/4
      cross-model agreement on the SAME substituted form.

  data/eval/mined_misc.json      -- (a) the c20/c21 typed-surface scoping pair
      (euphemized non-compete BLOCKED 16/16 when typed; the same provision relocated
      into ungated free-text purpose ships gate=PASS 16/16); (b) the impossible-date
      cells (c13/c14 ship 2026-02-30/2026-02-31 schema-valid + gate PASS on 24/24
      gpt/v4-pro/v4-flash cells; sonnet objects in unparseable free text, then under
      constrained decoding silently ships corrected dates), the c11 event-relative
      date confabulated 16/16, sonnet's invented '<UNKNOWN>' sentinel on c23, and the
      harness-vs-product wiring fact (production validate_semantics rejects Feb-30;
      the gauntlet oracle deliberately measures pure schema validity).

Regenerate all four:

  python -m contract_drafting.mined_findings            # rewrite data/eval/*.json

tests/test_mined_findings.py recomputes each from the cassettes and fails on any byte
drift against the committed artifacts (the raw_fidelity.py fail-closed pattern).
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from contract_drafting import demo_mars_beat as demo
from contract_drafting import gauntlet as g
from contract_drafting import jurisdiction_map as jm
# Single-source the model roster, denominators, and their live-suite cross-check from
# the demo driver (the same provenance chain raw_fidelity.py uses).
from contract_drafting.demo_offline import (
    CONTROLS, GOV_UNREP, MODELS, _TEMPLATE, _check_table1_denominators)
from contract_drafting.gauntlet import RecordReplayCaller
# The asked-jurisdiction key-name roster is shared with the committed law grader so
# the two artifacts can never disagree on what "names the asked jurisdiction" means.
from contract_drafting.raw_fidelity import _KEY_NAMES, _norm

_REPO = Path(__file__).resolve().parent.parent
_EVAL = _REPO / "data" / "eval"

BASELINE_LEAK_PATH = _EVAL / "baseline_leak.json"
FORCED_FILL_PATH = _EVAL / "forced_fill.json"
DIRECTIONALITY_PATH = _EVAL / "directionality.json"
MINED_MISC_PATH = _EVAL / "mined_misc.json"

_ALL_ARMS = ("raw", "verify_reject", "constrained", "constrained_hatch")
_NO_ABSTAIN_ARMS = ("raw", "verify_reject", "constrained")
_SILENT = ("wrong_sub", "omit")
_ARM_FNS = {"raw": g.run_raw, "verify_reject": g.run_verify_reject,
            "constrained": g.run_constrained, "constrained_hatch": g.run_constrained_hatch}


# ---------------------------------------------------------------------------
# Replay plumbing: one memoized replay per (model, case, arm), all fail-closed.
# ---------------------------------------------------------------------------
_callers: dict[str, RecordReplayCaller] = {}
_suite_cache: dict[str, g.Case] = {}
_replay_cache: dict[tuple[str, str, str], g.ArmResult] = {}


def _caller(model: str) -> RecordReplayCaller:
    if model not in _callers:
        tag = MODELS[model][1]
        _callers[model] = RecordReplayCaller(
            None, _EVAL / f"gauntlet_cassette.{tag}.hard.json", mode="replay")
    return _callers[model]


def _suite() -> dict[str, g.Case]:
    if not _suite_cache:
        _suite_cache.update({c.id: c for c in g.load_hard_suite()})
    return _suite_cache


def _replay(model: str, case_id: str, arm: str) -> g.ArmResult:
    key = (model, case_id, arm)
    if key not in _replay_cache:
        provider = MODELS[model][0]
        _replay_cache[key] = _ARM_FNS[arm](
            _suite()[case_id], caller=_caller(model), template_name=_TEMPLATE,
            provider=provider, model=model)
    return _replay_cache[key]


def _check_denominators() -> None:
    """Fail LOUD (real raise, survives `python -O`) before producing any number:
    the frozen Table-1 denominators must still derive from the live suite (M7)."""
    _check_table1_denominators()


# ---------------------------------------------------------------------------
# Artifact 1 -- baseline_leak.json
# ---------------------------------------------------------------------------
def _names_asked_law(case_id: str, raw: str) -> bool:
    """Does `raw` name the asked jurisdiction? Same rule + key-name roster as the
    committed law grader (raw_fidelity._faithful): normalized containment of any of
    the case's a-priori key names (DIFC also matches its spelled-out form)."""
    n = _norm(raw)
    return any(_norm(k) in n for k in _KEY_NAMES[case_id])


# The controls' asked (supported) jurisdictions, fixed from the suite instructions:
# c27 New York, c28 Republic of Singapore, c29 England and Wales.
_CONTROL_KEY_NAMES = {
    "c27": ("new york",),
    "c28": ("singapore",),
    "c29": ("england and wales",),
}


def compute_baseline_leak() -> dict:
    """Per model x baseline arm (raw / verify_reject / constrained): how many of the
    six un-representable governing-law fills carry a non-empty governingLawRaw naming
    the asked jurisdiction (the leaked hatch vocabulary used as a flag channel), the
    same count on the supported-law controls, and the counterfactual silent-wrong
    (silent-wrong minus flagged)."""
    _check_denominators()
    models: dict[str, dict] = {}
    pooled = {arm: {"flagged": 0, "silent_wrong": 0, "counterfactual_silent_wrong": 0,
                    "n": 0} for arm in _NO_ABSTAIN_ARMS}
    for model in MODELS:
        per_arm: dict[str, dict] = {}
        for arm in _NO_ABSTAIN_ARMS:
            rows = []
            for cid in GOV_UNREP:
                r = _replay(model, cid, arm)
                raw = (r.fields or {}).get("governingLawRaw")
                flagged = bool(raw) and _names_asked_law(cid, raw)
                silent = r.outcome in _SILENT
                rows.append({"case": cid, "outcome": r.outcome,
                             "governingLawRaw": raw or None,
                             "flags_asked_jurisdiction": flagged,
                             "silent_wrong": silent})
            ctrl_rows = []
            for cid in CONTROLS:
                r = _replay(model, cid, arm)
                raw = (r.fields or {}).get("governingLawRaw")
                ctrl_rows.append({"case": cid, "governingLawRaw": raw or None,
                                  "nonempty": bool(raw),
                                  "names_asked_law": bool(raw)
                                  and any(_norm(k) in _norm(raw)
                                          for k in _CONTROL_KEY_NAMES[cid])})
            flagged_n = sum(r["flags_asked_jurisdiction"] for r in rows)
            silent_n = sum(r["silent_wrong"] for r in rows)
            counterfactual = sum(1 for r in rows
                                 if r["silent_wrong"] and not r["flags_asked_jurisdiction"])
            per_arm[arm] = {
                "n": len(rows),
                "flagged": flagged_n,
                "silent_wrong": silent_n,
                "counterfactual_silent_wrong": counterfactual,
                "rows": rows,
                "controls": {
                    "n": len(ctrl_rows),
                    "raw_fill_nonempty": sum(r["nonempty"] for r in ctrl_rows),
                    "raw_fill_names_asked_law": sum(r["names_asked_law"] for r in ctrl_rows),
                    "rows": ctrl_rows,
                },
            }
            pooled[arm]["flagged"] += flagged_n
            pooled[arm]["silent_wrong"] += silent_n
            pooled[arm]["counterfactual_silent_wrong"] += counterfactual
            pooled[arm]["n"] += len(rows)
        models[model] = per_arm
    return {
        "what": "baseline definitions-block schema leak: the arm A/B/C prompt schema "
                "still carried the *Raw hatch fields inside the NDAData definition "
                "(schema_validator._strip_hatch strips top-level properties only), so "
                "the no-hatch arms could -- and often did -- route the asked "
                "jurisdiction into governingLawRaw. Nothing downstream is typed to "
                "receive it: production reads the raw only when governingLaw=='OTHER' "
                "(compliance_draft) and the outcome classifier scores the chosen "
                "governingLaw, so the flag is discarded at the pipeline level.",
        "arms": list(_NO_ABSTAIN_ARMS),
        "cases": list(GOV_UNREP),
        "controls": list(CONTROLS),
        "rule": {
            "flagged": "fill carries a non-empty governingLawRaw whose normalized text "
                       "contains >=1 of the case's asked-jurisdiction key names (the "
                       "committed raw_fidelity._KEY_NAMES roster; DIFC also matches "
                       "'Dubai International Financial Centre')",
            "silent_wrong": "outcome in (wrong_sub, omit) -- the paper's headline metric",
            "counterfactual_silent_wrong": "silent-wrong cases NOT flagged: what "
                                           "silent-wrong would be if a model-level raw "
                                           "flag counted as a signal",
            "controls": "same non-empty-raw count on the supported-law controls "
                        "(c27-c29): a raw echo on a correctly-filled supported law is "
                        "an indiscriminate echo, not an un-representability flag",
        },
        "models": models,
        "pooled": pooled,
    }


# ---------------------------------------------------------------------------
# Artifact 2 -- forced_fill.json
# ---------------------------------------------------------------------------
_FORUM_MEMBERS = ("SIAC", "ICC", "LCIA", "HKIAC", "AAA_ICDR", "CIETAC", "DIAC",
                  "SCC", "JAMS")
_FORUM_SENTINEL = "OTHER_FORUM"
# The Singapore-linkage check for gpt-5.5's residual concrete SIAC fills: does the
# case instruction fix Singapore as the governing law?
_SG_LAW_RE = re.compile(r"laws of the Republic of Singapore", re.IGNORECASE)


def _forum_kind(value: Any) -> tuple[str, Optional[str]]:
    """Classify a disputeForum fill: absent | sentinel | concrete | non_enum,
    normalizing display<->identifier surface variants the way the grader does."""
    if value in (None, ""):
        return "absent", None
    s = str(value)
    try:
        ident = jm.to_identifier_enum("DisputeForum", s, _TEMPLATE)
    except Exception:  # noqa: BLE001 -- unmappable string: fall through as-is
        ident = s
    if ident == _FORUM_SENTINEL:
        return "sentinel", ident
    if ident in _FORUM_MEMBERS:
        return "concrete", ident
    return "non_enum", s


def compute_forced_fill() -> dict:
    """disputeForum decomposition over the 41 non-forum cases (the suite minus the 16
    fc* disputeForum cases), arms A/C/D per model: filled / OTHER_FORUM / concrete /
    absent, the concrete value distributions, and the pooled unrequested-concrete
    inventory (arm A 39/164, arm C 67/164, arm D 13/164)."""
    _check_denominators()
    suite = _suite()
    non_forum = sorted(cid for cid in suite if not cid.startswith("fc"))
    if len(non_forum) != 41:
        raise RuntimeError(
            f"expected 41 non-forum cases (57 minus 16 fc*), got {len(non_forum)}")
    arms = ("raw", "constrained", "constrained_hatch")
    models: dict[str, dict] = {}
    pooled_concrete = {arm: 0 for arm in arms}
    for model in MODELS:
        per_arm: dict[str, dict] = {}
        for arm in arms:
            kinds = Counter()
            values = Counter()
            concrete_rows = []
            for cid in non_forum:
                fill = _replay(model, cid, arm).fields or {}
                kind, ident = _forum_kind(fill.get("disputeForum"))
                kinds[kind] += 1
                if kind in ("sentinel", "concrete"):
                    values[ident] += 1
                if kind == "concrete":
                    concrete_rows.append({
                        "case": cid, "value": ident,
                        "singapore_governing_law":
                            bool(_SG_LAW_RE.search(suite[cid].instruction)),
                    })
            filled = kinds["sentinel"] + kinds["concrete"] + kinds["non_enum"]
            pooled_concrete[arm] += kinds["concrete"]
            per_arm[arm] = {
                "n": len(non_forum),
                "filled": filled,
                "sentinel": kinds["sentinel"],
                "concrete": kinds["concrete"],
                "non_enum": kinds["non_enum"],
                "absent": kinds["absent"],
                "concrete_value_distribution": {
                    k: v for k, v in sorted(values.items()) if k != _FORUM_SENTINEL},
                "concrete_rows": concrete_rows,
            }
        models[model] = per_arm
    gpt_d = models["gpt-5.5"]["constrained_hatch"]["concrete_rows"]
    siac_sg = sum(1 for r in gpt_d
                  if r["value"] == "SIAC" and r["singapore_governing_law"])
    return {
        "what": "disputeForum forced-fill decomposition: NO case in this denominator "
                "asks for a dispute forum, so every fill is unrequested. The OpenAI "
                "strict massage (llm.py _strict_walk) sets required=all properties "
                "with no null union, so gpt-5.5 is grammar-forced to emit disputeForum "
                "on 41/41; what remains model behavior is WHICH value: with the "
                "sentinel available (arm D) it routes 32/41 into OTHER_FORUM; without "
                "it (arm C) the same forced fills become 41/41 concrete "
                "confabulations. The sentinel absorbs forced fills into a reviewable "
                "channel.",
        "arms": list(arms),
        "n_cases": len(non_forum),
        "cases_rule": "the 57-case hard suite minus the 16 fc* disputeForum cases "
                      "(probes + forum controls) -- none of these asks for a forum",
        "classification_rule": {
            "concrete": "value normalizes (display<->identifier, the grader's own "
                        "map) to a DisputeForum member other than OTHER_FORUM",
            "sentinel": "value normalizes to OTHER_FORUM",
            "non_enum": "non-empty value that maps to no enum member",
            "absent": "field missing or empty",
        },
        "models": models,
        "pooled_unrequested_concrete": {
            "denominator": len(non_forum) * len(MODELS),
            **{arm: pooled_concrete[arm] for arm in arms},
        },
        "gpt_arm_d_siac_singapore_linkage": {
            "concrete_total": len(gpt_d),
            "siac_on_singapore_governing_law_cases": siac_sg,
            "rule": "case instruction contains 'laws of the Republic of Singapore' "
                    "(the SIAC fills are a governing-law-linked inference, marked as "
                    "such, not a random confabulation)",
        },
    }


# ---------------------------------------------------------------------------
# Artifact 3 -- directionality.json
# ---------------------------------------------------------------------------
# The 14 non-US-state Jurisdiction members (13 international + the OTHER sentinel),
# fixed from the committed .cto; everything else in the enum is a US state or DC.
# _us_constituents() guards this list against the live enum on every regeneration.
_NON_US_MEMBERS = frozenset({
    "Republic_of_Singapore", "Hong_Kong_SAR", "England_and_Wales",
    "Republic_of_Kenya", "Republic_of_Indonesia", "Republic_of_India",
    "Kingdom_of_Saudi_Arabia", "United_Arab_Emirates", "Peoples_Republic_of_China",
    "Japan", "Republic_of_Korea", "Federal_Republic_of_Nigeria",
    "Republic_of_South_Africa", "OTHER",
})


def _jurisdiction_enum() -> set[str]:
    from contract_drafting.schema_validator import governing_law_enum
    enum_ids = governing_law_enum(_TEMPLATE)
    if len(enum_ids) != 65:
        raise RuntimeError(f"Jurisdiction enum has {len(enum_ids)} members, expected 65")
    missing = _NON_US_MEMBERS - enum_ids
    if missing:
        raise RuntimeError(f"_NON_US_MEMBERS not in the live enum: {sorted(missing)}")
    if any("canada" in m.lower() or "ontario" in m.lower() for m in enum_ids):
        raise RuntimeError("the no-Canadian-entry premise no longer holds")
    return enum_ids


def _relatives() -> dict[str, dict[str, str]]:
    """Per case: the asked jurisdiction's in-enum relatives, fixed a priori from
    ordinary legal geography (NOT tuned to observed fills):
      parent      -- the enum member is the asked jurisdiction's containing state
      sibling     -- shares that containing state with the ask
      constituent -- the enum member is a constituent of the asked (federal) ask
    Ontario (c01) has NO in-enum relative: the enum carries no Canadian entry."""
    us_states = sorted(_jurisdiction_enum() - _NON_US_MEMBERS)
    return {
        "c01": {},  # Ontario, Canada: no Canadian member in the enum
        "c02": {"England_and_Wales": "sibling"},        # Scotland: same-UK sibling
        "c03": {"United_Arab_Emirates": "parent"},      # DIFC: UAE free-zone parent
        "c04": {"Peoples_Republic_of_China": "parent",  # Macao SAR
                "Hong_Kong_SAR": "sibling"},
        "c06": {s: "constituent" for s in us_states},   # US federal: any state/DC
        "c08": {"United_Arab_Emirates": "parent"},      # DIFC again (distractor case)
    }


# Documentation labels for the ec* folk-matrix probes (the asked foreign/exotic form,
# as named in the suite instructions).
_EC_ASKED = {
    "ec01": "Gesellschaft mit beschraenkter Haftung (GmbH, Germany)",
    "ec02": "kabushiki kaisha (KK, Japan)",
    "ec03": "societe anonyme (SA, France)",
    "ec04": "besloten vennootschap (BV, Netherlands)",
    "ec05": "aktiebolag (AB, Sweden)",
    "ec06": "public limited company (plc, UK)",
    "ec07": "private company limited by shares (Pte, Singapore)",
    "ec08": "proprietary limited company (Pty Ltd, Australia)",
    "ec09": "unlimited company (ULC, Alberta)",
    "ec10": "statutory body / sovereign wealth fund",
    "ec11": "unincorporated cooperative association",
    "ec12": "decentralized autonomous organization (DAO)",
}
_ENTITY_MEMBERS = ("corporation", "limited_liability_company", "general_partnership",
                   "limited_partnership", "limited_liability_partnership",
                   "sole_proprietorship", "professional_corporation",
                   "nonprofit_corporation", "trust", "joint_venture", "individual")
_ENTITY_SENTINEL = "OTHER_ENTITY"


def _entity_ident(value: Any) -> Optional[str]:
    """Normalize an entityType fill to an enum identifier, or None for absent /
    non-enum / sentinel values (only concrete substituted forms enter the matrix)."""
    if value in (None, ""):
        return None
    try:
        ident = jm.to_identifier_enum("EntityType", str(value), _TEMPLATE)
    except Exception:  # noqa: BLE001
        ident = str(value)
    return ident if ident in _ENTITY_MEMBERS else None


def compute_directionality() -> dict:
    """Substitution directionality on the six un-representable governing-law cases
    over the no-abstention arms (A/B/C, 12 cells per case), plus the entityType folk
    matrix over the 12 ec* probes (modal substituted form per model, cross-model
    agreement)."""
    _check_denominators()
    enum_ids = _jurisdiction_enum()
    relatives = _relatives()
    suite = _suite()
    cases: dict[str, dict] = {}
    filled_on_relative = filled_wrong_total = 0
    for cid in GOV_UNREP:
        cells = Counter()
        values = Counter()
        rows = []
        for model in MODELS:
            for arm in _NO_ABSTAIN_ARMS:
                fill = _replay(model, cid, arm).fields or {}
                v = fill.get("governingLaw")
                if v in (None, ""):
                    relation = "omit"
                else:
                    try:
                        ident = jm.to_identifier(str(v))
                    except Exception:  # noqa: BLE001
                        ident = str(v)
                    if ident not in enum_ids or ident == "OTHER":
                        # schema-invalid fill: a verbatim leak if it names the ask
                        relation = ("verbatim_leak" if _names_asked_law(cid, str(v))
                                    else "invalid_other")
                        values[f"(schema-invalid) {v}"] += 1
                    else:
                        relation = relatives[cid].get(ident, "unrelated")
                        values[ident] += 1
                        filled_wrong_total += 1
                        filled_on_relative += relation in ("parent", "sibling",
                                                           "constituent")
                cells[relation] += 1
                rows.append({"model": model, "arm": arm,
                             "value": None if v in (None, "") else str(v),
                             "relation": relation})
        cases[cid] = {
            "asked": _KEY_NAMES[cid][0],
            "in_enum_relatives": relatives[cid] if cid != "c06"
            else {"(any US state or DC)": "constituent"},
            "cells": {k: cells[k] for k in sorted(cells)},
            "filled_values": {k: values[k] for k in sorted(values)},
            "rows": rows,
        }
    # excluding c01 (no relative exists), every filled wrong value must be counted
    relative_exists_filled = filled_wrong_total - sum(
        1 for r in cases["c01"]["rows"] if r["relation"] in ("unrelated",))
    # entityType folk matrix ----------------------------------------------------
    probes: dict[str, dict] = {}
    agree_3of4 = 0
    for cid in sorted(_EC_ASKED):
        field = suite[cid].field
        modal_by_model = {}
        counts_by_model = {}
        for model in MODELS:
            vals = Counter()
            for arm in _NO_ABSTAIN_ARMS:
                ident = _entity_ident((_replay(model, cid, arm).fields or {}).get(field))
                if ident:
                    vals[ident] += 1
            # Counter.most_common ties break by first-encountered (arm order
            # raw -> verify_reject -> constrained): deterministic on replay.
            modal_by_model[model] = vals.most_common(1)[0][0] if vals else None
            counts_by_model[model] = {k: vals[k] for k in sorted(vals)}
        forms = Counter(v for v in modal_by_model.values() if v)
        top_form, top_n = (forms.most_common(1)[0] if forms else (None, 0))
        agree_3of4 += top_n >= 3
        probes[cid] = {
            "field": field,
            "asked_form": _EC_ASKED[cid],
            "modal_by_model": modal_by_model,
            "fill_counts_by_model": counts_by_model,
            "top_form": top_form,
            "models_agreeing": top_n,
        }
    return {
        "what": "substitution directionality: pooled over the no-abstention arms "
                "(A raw, B verify-reject, C constrained), every filled "
                "wrong-jurisdiction substitution lands on the asked jurisdiction's "
                "in-enum parent or sibling (or a constituent, for the US-federal "
                "ask) when one exists; the one probe with no in-enum relative "
                "(Ontario -- the enum has no Canadian entry) scatters into omits "
                "and divergent fills. Nearest-representable coercion, not random "
                "error. The entityType folk matrix replicates the mechanism "
                "cross-field: most ec* probes draw the SAME substituted form from "
                ">=3 of 4 models.",
        "arms_pooled": list(_NO_ABSTAIN_ARMS),
        "cells_per_case": len(MODELS) * len(_NO_ABSTAIN_ARMS),
        "relation_rule": {
            "parent": "in-enum containing state of the asked jurisdiction",
            "sibling": "in-enum jurisdiction sharing the asked one's containing state",
            "constituent": "in-enum constituent of the asked federal jurisdiction "
                           "(c06 only: any US state or DC)",
            "unrelated": "in-enum fill with none of the above relations",
            "verbatim_leak": "schema-invalid fill naming the asked jurisdiction "
                             "itself (visible, caught by validation)",
            "omit": "field absent or empty (silently renders the pipeline default)",
            "provenance": "relative maps fixed a priori from legal geography in "
                          "contract_drafting/mined_findings.py::_relatives, guarded "
                          "against the live 65-member enum on every regeneration",
        },
        "cases": cases,
        "summary": {
            "filled_wrong_total": filled_wrong_total,
            "filled_on_in_enum_relative": filled_on_relative,
            "filled_wrong_where_relative_exists": relative_exists_filled,
            "note": "filled_on_in_enum_relative == filled_wrong_where_relative_exists "
                    "== 44: every filled wrong value lands on the relative whenever "
                    "one exists; the residue is c01's divergent cells.",
        },
        "entity_folk_matrix": {
            "probes": probes,
            "n_probes": len(probes),
            "probes_with_3of4_agreement": agree_3of4,
            "rule": "per model, the modal concrete entityType fill over the three "
                    "no-abstention arms; agreement = models sharing the plurality "
                    "modal form",
        },
    }


# ---------------------------------------------------------------------------
# Artifact 4 -- mined_misc.json
# ---------------------------------------------------------------------------
_IMPOSSIBLE = {"c13": "2026-02-30", "c14": "2026-02-31"}
_NON_SONNET = tuple(m for m in MODELS if m != "claude-sonnet-4-6")
_OBJECTION_RE = re.compile(r"February[^.\n]*does not exist[^.\n]*\.")
_FENCED_JSON_RE = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)
# Recording-window start (DATACARD.md: all cassettes recorded 2026-06-05..09); the
# reference date for "the confabulated c11 effective date lies in the past".
_RECORDING_WINDOW_START = "2026-06-05"
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _why_hard(case_id: str) -> str:
    """The suite's own why_hard rationale for `case_id`, read from the committed
    hard_suite.json (the Case dataclass does not carry it) -- included verbatim as
    provenance that the c20/c21 pair was authored as a pair (pre-registered)."""
    data = json.loads(g.HARD_SUITE_PATH.read_text(encoding="utf-8"))
    for c in data:
        if c["id"] == case_id:
            return c.get("why_hard", "")
    raise RuntimeError(f"case {case_id!r} not in {g.HARD_SUITE_PATH}")


def _sonnet_raw_text(case_id: str) -> str:
    """The verbatim arm-A free-text response for claude-sonnet-4-6 on `case_id`,
    replayed from the committed cassette (the same request run_raw issues)."""
    case = _suite()[case_id]
    q, c, _ = demo.build_prompt(case.instruction, _TEMPLATE, with_abstain=False)
    return _caller("claude-sonnet-4-6").text(
        q, c, provider="anthropic", model="claude-sonnet-4-6",
        system_prompt=demo._SYSTEM)


def _semantics_call_sites() -> list[str]:
    """The production call sites wiring validate_semantics into the draft gate,
    located in the LIVE source and anchored by ENCLOSING FUNCTION (not line number,
    which would couple this artifact's bytes to unrelated edits of the file): a
    refactor that unwires the check drifts this artifact loudly."""
    src = (_REPO / "contract_drafting" / "compliance_draft.py").read_text(
        encoding="utf-8").splitlines()
    sites = []
    for i, ln in enumerate(src):
        if "validate_semantics(" in ln and "import" not in ln:
            fn = next((m.group(1) for j in range(i, -1, -1)
                       if (m := re.match(r"def (\w+)", src[j]))), "?")
            sites.append(f"contract_drafting/compliance_draft.py::{fn}")
    if not sites:
        raise RuntimeError("no validate_semantics call site found in compliance_draft.py")
    return sorted(set(sites))


def compute_mined_misc() -> dict:
    """The c20/c21 typed-surface scoping pair, the impossible-date cells (with
    sonnet's silenced objection), the c11 event-relative confabulation, sonnet's
    invented '<UNKNOWN>' sentinel, and the harness-vs-product semantics wiring."""
    _check_denominators()
    suite = _suite()
    # (a) c20 / c21 typed-surface pair ------------------------------------------
    c20_rows, c21_rows = [], []
    for model in MODELS:
        for arm in _ALL_ARMS:
            r20 = _replay(model, "c20", arm)
            c20_rows.append({"model": model, "arm": arm, "gate": r20.gate,
                             "hasNonCompete": (r20.fields or {}).get("hasNonCompete")})
            r21 = _replay(model, "c21", arm)
            c21_rows.append({"model": model, "arm": arm, "gate": r21.gate,
                             "hasNonSolicitation":
                                 (r21.fields or {}).get("hasNonSolicitation")})
    pair = {
        "c20": {
            "defect_class": suite["c20"].defect_class,
            "why_hard": _why_hard("c20"),
            "instruction_excerpt": suite["c20"].instruction[:200],
            "blocked": sum(1 for r in c20_rows if r["gate"] == "BLOCKED"),
            "n": len(c20_rows),
            "rows": c20_rows,
        },
        "c21": {
            "defect_class": suite["c21"].defect_class,
            "why_hard": _why_hard("c21"),
            "instruction_excerpt": suite["c21"].instruction[:200],
            "pass_with_no_structured_flag": sum(
                1 for r in c21_rows
                if r["gate"] == "PASS" and not r["hasNonSolicitation"]),
            "n": len(c21_rows),
            "rows": c21_rows,
        },
        "note": "the same prohibited provision: typed as hasNonCompete=true it is "
                "BLOCKED on every cell; relocated into the ungated free-text purpose "
                "field it ships gate=PASS on every cell -- the guarantee is scoped "
                "to the typed surface (the suite's own why_hard predicts this; "
                "pre-registered pair).",
    }
    # (b) impossible dates -------------------------------------------------------
    date_rows = []
    ship_impossible = 0
    for cid, asked in sorted(_IMPOSSIBLE.items()):
        for model in MODELS:
            for arm in _ALL_ARMS:
                r = _replay(model, cid, arm)
                eff = (r.fields or {}).get("effectiveDate")
                ships = (r.schema_valid and r.gate == "PASS" and eff == asked)
                if model in _NON_SONNET:
                    ship_impossible += ships
                date_rows.append({"case": cid, "model": model, "arm": arm,
                                  "effectiveDate": eff, "schema_valid": r.schema_valid,
                                  "gate": r.gate,
                                  "ships_impossible_date_pass": ships})
    sonnet = {}
    for cid in sorted(_IMPOSSIBLE):
        raw_text = _sonnet_raw_text(cid)
        m = _OBJECTION_RE.search(raw_text)
        fenced = _FENCED_JSON_RE.search(raw_text)
        proposed = None
        if fenced:
            try:
                proposed = json.loads(fenced.group(1)).get("effectiveDate")
            except json.JSONDecodeError:
                proposed = None
        sonnet[cid] = {
            "raw_arm": "free-text objection around a fenced JSON block; the harness "
                       "parser rejects it (unparseable model output -> schema-invalid, "
                       "gate ERROR): the objection is audible but unlandable",
            "objection_quote_verbatim": m.group(0) if m else None,
            "raw_proposed_effectiveDate": proposed,
            "constrained_shipped_effectiveDate":
                (_replay("claude-sonnet-4-6", cid, "constrained").fields or {})
                .get("effectiveDate"),
            "constrained_hatch_shipped_effectiveDate":
                (_replay("claude-sonnet-4-6", cid, "constrained_hatch").fields or {})
                .get("effectiveDate"),
        }
    # production wiring: validate_semantics DOES reject the impossible dates -----
    from contract_drafting.schema_validator import validate_semantics
    rejections = {asked: validate_semantics({"effectiveDate": asked},
                                            template_name=_TEMPLATE)
                  for asked in sorted(_IMPOSSIBLE.values())}
    for asked, errs in rejections.items():
        if not errs:
            raise RuntimeError(
                f"validate_semantics no longer rejects {asked} -- the "
                f"harness-vs-product wiring claim would be false")
    # (c) c11 event-relative date ------------------------------------------------
    c11_rows = []
    for model in MODELS:
        for arm in _ALL_ARMS:
            r = _replay(model, "c11", arm)
            eff = (r.fields or {}).get("effectiveDate")
            concrete = bool(eff) and bool(_ISO_DATE_RE.fullmatch(str(eff)))
            c11_rows.append({"model": model, "arm": arm, "effectiveDate": eff,
                             "concrete": concrete,
                             "before_recording_window":
                                 concrete and str(eff) < _RECORDING_WINDOW_START})
    concrete_vals = sorted({r["effectiveDate"] for r in c11_rows if r["concrete"]})
    # (d) sonnet's invented '<UNKNOWN>' sentinel on c23 ---------------------------
    r23 = _replay("claude-sonnet-4-6", "c23", "constrained_hatch")
    unknown = {
        "model": "claude-sonnet-4-6", "case": "c23", "arm": "constrained_hatch",
        "effectiveDate": (r23.fields or {}).get("effectiveDate"),
        "schema_valid": r23.schema_valid,
        "errors": r23.errors,
        "note": "asked to abstain via the TYPED sentinel, the model instead invents "
                "an out-of-schema string sentinel ('<UNKNOWN>') in a pattern-typed "
                "field -- schema-INVALID, so the type system catches it (a visible "
                "leak, and a caution that instruction-invented sentinels do not "
                "compose with typed fields).",
    }
    return {
        "what": "miscellaneous mined findings: the c20/c21 typed-surface scoping "
                "pair, the impossible-date band (harness-vs-product wiring), the "
                "c11 event-relative confabulation, and sonnet's invented sentinel.",
        "typed_surface_pair": pair,
        "impossible_dates": {
            "asked": dict(sorted(_IMPOSSIBLE.items())),
            "cells_shipping_impossible_date_schema_valid_pass": ship_impossible,
            "n_cells_gpt_pro_flash": len(_IMPOSSIBLE) * len(_NON_SONNET) * len(_ALL_ARMS),
            "rows": date_rows,
            "sonnet": sonnet,
            "production_wiring": {
                "validate_semantics_rejections": rejections,
                "call_sites": _semantics_call_sites(),
                "fact": "production drafting rejects the impossible dates: both "
                        "draft paths call schema_validator.validate_semantics "
                        "(real-calendar-date check) in addition to the JSON-Schema "
                        "gate. The gauntlet oracle deliberately measures PURE schema "
                        "validity (gauntlet.oracle -> demo_mars_beat._validate_arm) "
                        "and does not wire validate_semantics, so 'schema-valid + "
                        "gate PASS' here is a harness-scope statement about the type "
                        "system's regex, not a claim that the product ships Feb-30.",
            },
        },
        "c11_event_relative_date": {
            "instruction_excerpt": suite["c11"].instruction[:200],
            "filled_concrete": sum(r["concrete"] for r in c11_rows),
            "n": len(c11_rows),
            "distinct_values": len(concrete_vals),
            "values": concrete_vals,
            "before_recording_window": sum(r["before_recording_window"]
                                           for r in c11_rows),
            "reference_date": _RECORDING_WINDOW_START,
            "reference_rule": "recording-window start per DATACARD.md (cassettes "
                              "recorded 2026-06-05..09); a confabulated 'effective "
                              "date' earlier than the recording itself cannot be a "
                              "good-faith estimate of a future Series C closing",
            "rows": c11_rows,
        },
        "invented_sentinel": unknown,
    }


# ---------------------------------------------------------------------------
# Write / CLI
# ---------------------------------------------------------------------------
_ARTIFACTS = {
    "baseline_leak": (compute_baseline_leak, BASELINE_LEAK_PATH),
    "forced_fill": (compute_forced_fill, FORCED_FILL_PATH),
    "directionality": (compute_directionality, DIRECTIONALITY_PATH),
    "mined_misc": (compute_mined_misc, MINED_MISC_PATH),
}


def write_artifact(name: str, path: Optional[Path] = None) -> dict:
    """Recompute one artifact and write it byte-reproducibly (indent=2, sorted keys,
    trailing newline)."""
    compute, default_path = _ARTIFACTS[name]
    report = compute()
    out = Path(path) if path else default_path
    out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    return report


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m contract_drafting.mined_findings",
        description="Recompute the four mined-result artifacts (baseline_leak, "
                    "forced_fill, directionality, mined_misc) from the committed "
                    "hard-suite cassettes (offline replay) and write them to "
                    "data/eval/ byte-reproducibly.")
    p.add_argument("--only", choices=sorted(_ARTIFACTS), default=None,
                   help="regenerate a single artifact (default: all four)")
    p.add_argument("--out-dir", default=str(_EVAL),
                   help=f"output directory (default: {_EVAL})")
    args = p.parse_args(argv)
    names = [args.only] if args.only else sorted(_ARTIFACTS)
    out_dir = Path(args.out_dir)
    try:
        for name in names:
            report = write_artifact(name, out_dir / f"{name}.json")
            print(f"{name} -> {out_dir / f'{name}.json'}")
            if name == "baseline_leak":
                for arm, s in sorted(report["pooled"].items()):
                    print(f"  {arm:14} silent-wrong {s['silent_wrong']}/{s['n']} -> "
                          f"counterfactual {s['counterfactual_silent_wrong']}/{s['n']} "
                          f"(flags {s['flagged']}/{s['n']})")
            elif name == "forced_fill":
                pc = report["pooled_unrequested_concrete"]
                print(f"  unrequested concrete /{pc['denominator']}: "
                      f"A {pc['raw']}  C {pc['constrained']}  "
                      f"D {pc['constrained_hatch']}")
            elif name == "directionality":
                s = report["summary"]
                print(f"  filled-wrong on in-enum relative: "
                      f"{s['filled_on_in_enum_relative']}/"
                      f"{s['filled_wrong_where_relative_exists']} (where one exists); "
                      f"folk matrix "
                      f"{report['entity_folk_matrix']['probes_with_3of4_agreement']}"
                      f"/{report['entity_folk_matrix']['n_probes']} probes >=3/4 agree")
            elif name == "mined_misc":
                tp = report["typed_surface_pair"]
                dd = report["impossible_dates"]
                print(f"  c20 BLOCKED {tp['c20']['blocked']}/{tp['c20']['n']}; "
                      f"c21 PASS-no-flag "
                      f"{tp['c21']['pass_with_no_structured_flag']}/{tp['c21']['n']}; "
                      f"impossible dates shipped "
                      f"{dd['cells_shipping_impossible_date_schema_valid_pass']}"
                      f"/{dd['n_cells_gpt_pro_flash']}")
    except g.GauntletCacheMiss as e:
        print(f"REPLAY FAILED (offline cassette miss): {e}")
        return 2
    except (json.JSONDecodeError, OSError) as e:
        print(f"REPLAY FAILED (artifact unreadable): {e}")
        return 3
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
