"""
CI guard for the Gauntlet eval's reproducibility (the audit's P1-a).

The documented "replay offline, no API key" path silently broke at HEAD when the PR2
schema change altered schema.json and every cassette key changed. These tests REPLAY
each committed cassette through the full harness and fail on any cache-miss -- so a
future .cto/schema edit that rots the cassettes fails CI loudly instead of silently.

They also assert the headline result is reproducible from committed artifacts (the audit's
P1-b): the constrained+hatch arm abstains where the constrained arm silently substitutes.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from contract_drafting import gauntlet as g
from contract_drafting.gauntlet import RecordReplayCaller

_EVAL = Path(__file__).resolve().parent.parent / "data" / "eval"

# (cassette filename, suite, provider, model) for every committed cassette.
_CASSETTES = [
    ("gauntlet_cassette.openai.hard.json", "hard", "openai", "gpt-5.5"),
    ("gauntlet_cassette.deepseek.hard.json", "hard", "deepseek", "deepseek-v4-pro"),
    ("gauntlet_cassette.deepseek-flash.hard.json", "hard", "deepseek", "deepseek-v4-flash"),
    ("gauntlet_cassette.anthropic.hard.json", "hard", "anthropic", "claude-sonnet-4-6"),
    ("gauntlet_cassette.openai.json", "schema", "openai", "gpt-5.5"),
    ("gauntlet_cassette.deepseek.json", "schema", "deepseek", "deepseek-v4-pro"),
    ("gauntlet_cassette.deepseek-flash.json", "schema", "deepseek", "deepseek-v4-flash"),
]

# hard-suite cassettes only (the 4 models with the full red-teamed suite + the 3 supported controls)
_HARD = [(c, p, m) for (c, s, p, m) in _CASSETTES if s == "hard"]


@pytest.mark.parametrize("cas,suite,provider,model", _CASSETTES,
                         ids=[c[0] for c in _CASSETTES])
def test_cassette_replays_at_head(cas, suite, provider, model):
    """Replay the full gauntlet (all arms) from a committed cassette with NO API. Any
    cache-miss raises GauntletCacheMiss and fails this test -- the guard that was missing
    when PR2 silently invalidated every cassette."""
    path = _EVAL / cas
    # required manifest: a missing cassette FAILS (the guard must not pass without replaying it)
    assert path.exists(), f"required cassette missing from the repo: {cas}"
    caller = RecordReplayCaller(None, path, mode="replay")
    results = g.run_gauntlet(caller, provider=provider, model=model, mode=suite)
    assert results, "replay produced no results"
    # all four arms present
    arms = {r.arm for r in results}
    assert {"raw", "verify_reject", "constrained", "constrained_hatch"} <= arms


@pytest.mark.parametrize("cas,provider,model", _HARD, ids=[c[0] for c in _HARD])
def test_hatch_arm_reproducible_from_cassette(cas, provider, model):
    """Reproducible headline (audit P1-b), now across ALL FOUR models: the constrained+hatch arm
    ABSTAINS where the plain constrained arm silently substitutes, AND ZERO over-abstention on the
    11 supported-value controls (3 law + 4 entity-form + 4 dispute-forum) replays OFFLINE -- the
    cross-field over-abstention safety guard (M2/D5)."""
    path = _EVAL / cas
    assert path.exists(), f"required cassette missing: {cas}"
    caller = RecordReplayCaller(None, path, mode="replay")
    s = g.summarize(g.run_gauntlet(caller, provider=provider, model=model, mode="hard"))
    assert s["constrained_hatch"]["abstained"] > 0
    assert s["constrained_hatch"]["silent_wrong"] < s["constrained"]["silent_wrong"]
    # the supported-value controls are filled (or mis-filled), NEVER over-abstained -- across all
    # three typed fields (governingLaw, entityType, disputeForum); replayed offline.
    assert s["constrained_hatch"]["over_abstain"] == 0
    assert s["constrained_hatch"]["over_abstain_n"] == 11
    assert s["constrained_hatch"]["control_correct"] >= 3


@pytest.mark.parametrize("cas,provider,model", _HARD, ids=[c[0] for c in _HARD])
def test_intent_guard_arm_reproducible(cas, provider, model):
    """Arm E (intent-guard over arm C) replays offline from each committed cassette: the
    deterministic gate catches every governing-law substitution (catch 6/6) and false-flags
    none of the supported-law controls (0/3), on all four models -- the gate's contribution
    is now measured, not narrated (D7/D9)."""
    path = _EVAL / cas
    assert path.exists(), f"required cassette missing: {cas}"
    caller = RecordReplayCaller(None, path, mode="replay")
    suite = g.build_suite("nda-mutual", mode="hard")
    ig = g.run_intent_guard(suite, caller=caller, provider=provider, model=model)
    assert ig["catch"] == ig["catch_n"] == 6      # flags all 6 governing-law substitutions
    assert ig["false_flag"] == 0 and ig["false_flag_n"] == 3


def test_gate_catches_baseline_substitutions():
    """Reproducible gate metric (audit P1-b): replay the no-hatch baseline arm and assert
    the intent gate flags every governing-law silent substitution it produced -- from the
    committed cassette, no API. (Offline gate: the hard-suite instructions name the law
    explicitly, so an un-representable ask fails closed.)"""
    import json
    from contract_drafting import intent_check as ic
    path = _EVAL / "gauntlet_cassette.openai.hard.json"
    assert path.exists(), "required cassette missing: gauntlet_cassette.openai.hard.json"
    hard = {c["id"]: c for c in json.loads((_EVAL / "hard_suite.json").read_text())}
    caller = RecordReplayCaller(None, path, mode="replay")
    results = g.run_gauntlet(caller, provider="openai", model="gpt-5.5", mode="hard")
    gov_silent = [r for r in results if r.arm == "constrained" and r.field == "governingLaw"
                  and r.outcome in ("wrong_sub", "omit")]
    assert gov_silent, "expected the baseline arm to produce governing-law silent substitutions"
    for r in gov_silent:
        instr = hard[r.case_id]["instruction"]
        w = ic.verify_intent(instr, {"governingLaw": r.chosen_value}, allow_llm_fallback=False)
        assert w, f"gate failed to flag {r.case_id} (filled {r.chosen_value!r})"
