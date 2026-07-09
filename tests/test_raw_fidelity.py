"""
CI guard for the raw-fidelity artifact (A-S3) -- same fail-closed pattern as
tests/test_gauntlet_replay.py.

data/eval/raw_fidelity.json grades how faithfully arm D's governingLawRaw captures
the asked jurisdiction on the six un-representable governing-law cases x four
models. These tests recompute it from the committed cassettes through the gauntlet's
own replay path (no API key, no network) and FAIL on any byte drift against the
committed artifact, so a schema/prompt/cassette change that shifts the paper's
fidelity numbers breaks CI loudly instead of silently.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from contract_drafting import gauntlet as g
from contract_drafting import raw_fidelity as rf
from contract_drafting.gauntlet import RecordReplayCaller

_EVAL = Path(__file__).resolve().parent.parent / "data" / "eval"


@pytest.fixture(autouse=True)
def _no_api_keys(monkeypatch):
    """Fully offline: a cassette miss must raise GauntletCacheMiss, never call out."""
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(k, raising=False)


def test_committed_artifact_replays_byte_identical(tmp_path):
    """Recompute data/eval/raw_fidelity.json from the committed cassettes and assert
    BYTE equality with the committed artifact (fails closed on drift)."""
    committed = rf.OUT_PATH
    assert committed.exists(), "required artifact missing: data/eval/raw_fidelity.json"
    out = tmp_path / "raw_fidelity.json"
    rf.write_report(out)
    assert out.read_bytes() == committed.read_bytes(), (
        "raw_fidelity.json drifted from what the committed cassettes replay; "
        "regenerate with `python -m contract_drafting.raw_fidelity` and review the diff")


def test_headline_fidelity_numbers():
    """The paper's citable numbers: 24/24 faithful, 22/24 verbatim, 23/24
    case-insensitive; zero missing raws; the only verbatim misses are sonnet's
    c03 (clause reorder) and c06 (capitalization; recovered case-insensitively)."""
    report = rf.compute()
    s = report["summary"]
    assert s["n_total"] == 24 and s["missing_raw"] == 0
    assert s["faithful"] == 24
    assert s["verbatim"] == 22
    assert s["ci_substring"] == 23
    assert s["verbatim_misses"] == ["claude-sonnet-4-6/c03", "claude-sonnet-4-6/c06"]
    assert s["ci_substring_misses"] == ["claude-sonnet-4-6/c03"]
    for model, m in report["models"].items():
        assert m["faithful"] == m["n"] == 6, f"{model}: every abstention names the ask"


def test_none_sighting_lives_in_instr_only_ablation_not_arm_d():
    """Resolve the reviewer's governingLawRaw=None sighting DEFINITIVELY: it is the
    INSTRUCTION-ONLY ablation (claude-sonnet-4-6, c01/c04/c06), where with_abstain=False
    strips the hatch fields from the schema -- the bare OTHER there is schema-INVALID
    (outcome 'leak'), not a silent raw-less abstention. Arm D itself carries a raw on
    all 24 abstentions (asserted by the tests above / compute()'s own raise)."""
    from contract_drafting import demo_mars_beat as demo
    suite = {c.id: c for c in g.load_hard_suite()}
    caller = RecordReplayCaller(
        None, _EVAL / "gauntlet_cassette.anthropic.hard.json", mode="replay")
    none_cases = []
    for cid in rf.GOV_UNREP:
        r = g._run_single(suite[cid], constrained=True, caller=caller,
                          template_name="nda-mutual",
                          provider="anthropic", model="claude-sonnet-4-6",
                          arm_name="ablation_instr_only_governingLaw",
                          system_prompt=demo._abstain_system("governingLaw"),
                          with_abstain=False)
        f = r.fields or {}
        if str(f.get("governingLaw")) == "OTHER" and not f.get("governingLawRaw"):
            none_cases.append(cid)
            assert not r.schema_valid, (
                f"{cid}: a bare OTHER without the hatch schema must be schema-INVALID")
            assert r.outcome == "leak"
    assert none_cases == ["c01", "c04", "c06"]


def test_artifact_internal_consistency():
    """The committed JSON's summary equals the sum of its own rows (no hand edits)."""
    d = json.loads(rf.OUT_PATH.read_text(encoding="utf-8"))
    assert d["arm"] == "constrained_hatch"
    assert d["cases"] == ["c01", "c02", "c03", "c04", "c06", "c08"]
    assert sorted(d["models"]) == sorted(
        ["gpt-5.5", "deepseek-v4-pro", "deepseek-v4-flash", "claude-sonnet-4-6"])
    for metric in ("faithful", "verbatim", "ci_substring"):
        assert d["summary"][metric] == sum(m[metric] for m in d["models"].values())
        for m in d["models"].values():
            assert m[metric] == sum(bool(r[metric]) for r in m["rows"])
    assert all(r["raw"] for m in d["models"].values() for r in m["rows"])


def test_compute_revalidates_denominator_against_live_suite(monkeypatch):
    """compute() must re-cross-check GOV_UNREP vs the live suite and the key-name
    roster on every regeneration (Codex adv: a frozen tuple could otherwise
    regenerate a stale 24 after a suite edit). Both guards are real raises, not
    asserts, so they survive `python -O`."""
    # Roster drift -> RuntimeError before any number is produced.
    monkeypatch.setitem(rf._KEY_NAMES, "c99", ("x",))
    with pytest.raises(RuntimeError, match="_KEY_NAMES"):
        rf.compute()


def test_compute_invokes_suite_denominator_crosscheck(monkeypatch):
    """A suite-vs-frozen-tuple mismatch (M7 cross-check) propagates out of compute()."""
    def _boom():
        raise RuntimeError("Table 1 denominator drift")
    monkeypatch.setattr(rf, "_check_table1_denominators", _boom)
    with pytest.raises(RuntimeError, match="denominator drift"):
        rf.compute()
