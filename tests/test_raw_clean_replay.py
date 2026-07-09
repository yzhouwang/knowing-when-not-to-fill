"""
CI guard for the raw_clean (clean-prompt spot-check) recording -- the same
fail-closed pattern as tests/test_gauntlet_replay.py / test_mined_findings.py.

The four cleanbase cassettes (gauntlet_cassette.<tag>.hard.cleanbase.json,
recorded 2026-07-04: arm A against the v2-clean baseline schema, the 9
governing-law hard-suite cases) must replay OFFLINE through the harness's own
run_raw_clean path, and data/eval/raw_clean_flags.json -- graded with the
PRE-REGISTERED volunteered_flag rule (DATACARD.md, commit ad538c5, committed
BEFORE recording) -- must regenerate byte-identically from them. Any schema/
prompt/cassette drift that shifts a graded number breaks CI loudly.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from contract_drafting import gauntlet as g
from contract_drafting import raw_fidelity as rf
from contract_drafting.demo_offline import CONTROLS, GOV_UNREP, MODELS
from contract_drafting.gauntlet import RecordReplayCaller

_EVAL = Path(__file__).resolve().parent.parent / "data" / "eval"

# (cassette filename, provider, model) for every committed cleanbase cassette,
# derived from the single-source model roster so the two cannot drift.
_CLEANBASE = [(f"gauntlet_cassette.{tag}.hard.cleanbase.json", provider, model)
              for model, (provider, tag) in MODELS.items()]


@pytest.fixture(autouse=True)
def _no_api_keys(monkeypatch):
    """Fully offline: a cassette miss must raise GauntletCacheMiss, never call out."""
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(k, raising=False)


@pytest.mark.parametrize("cas,provider,model", _CLEANBASE,
                         ids=[c[0] for c in _CLEANBASE])
def test_cleanbase_cassette_replays_at_head(cas, provider, model):
    """Replay the raw_clean condition from a committed cleanbase cassette with NO
    API. Any cache-miss raises GauntletCacheMiss and fails this test -- exactly the
    guard test_gauntlet_replay.py provides for the 2026-06 hard-suite cassettes."""
    path = _EVAL / cas
    assert path.exists(), f"required cassette missing from the repo: {cas}"
    caller = RecordReplayCaller(None, path, mode="replay")
    suite = g.build_suite("nda-mutual", mode="hard")
    results = g.run_raw_clean(suite, caller=caller, provider=provider, model=model)
    assert {r.case_id for r in results} == set(GOV_UNREP) | set(CONTROLS)
    assert all(r.arm == "raw_clean" for r in results)
    # every committed recording parsed into a fields dict (compute_raw_clean
    # fails closed on anything else)
    assert all(isinstance(r.fields, dict) for r in results)


def test_raw_clean_flags_replays_byte_identical(tmp_path):
    """The graded artifact regenerates byte-identically from the committed
    cleanbase cassettes (offline replay + the pre-registered volunteered_flag
    rule): same fail-closed byte-drift guard as test_mined_findings.py."""
    committed = rf.OUT_PATH_RAW_CLEAN
    assert committed.exists(), "required artifact missing: data/eval/raw_clean_flags.json"
    out = tmp_path / "raw_clean_flags.json"
    rf.write_raw_clean_report(out)
    assert out.read_bytes() == committed.read_bytes(), (
        "raw_clean_flags.json drifted from what the committed cleanbase cassettes "
        "replay; regenerate with `python -m contract_drafting.raw_fidelity "
        "--raw-clean` and review the diff")


def test_raw_clean_headline_numbers():
    """Pin the graded headline: volunteered flags on the 6 un-representable cases
    and false flags on the 3 supported-law controls, per model, as recorded
    2026-07-04 and graded by the pre-registered rule."""
    import json
    d = json.loads(rf.OUT_PATH_RAW_CLEAN.read_text(encoding="utf-8"))
    flags = {m: (d["models"][m]["volunteered_flags_unrep"],
                 d["models"][m]["false_flags_controls"]) for m in d["models"]}
    assert flags == {
        "gpt-5.5": (3, 0),
        "deepseek-v4-pro": (2, 0),
        "deepseek-v4-flash": (2, 0),
        "claude-sonnet-4-6": (2, 0),
    }
    # 8 of the 9 flags surface ONLY inside a copied party name (the c03/c04
    # confound documented in the artifact's literal_application_notes); the one
    # non-mechanical flag is gpt-5.5's volunteered governingLawText on c02.
    assert d["summary"]["flags_party_name_only"] == 8
    gpt_c02 = next(r for r in d["models"]["gpt-5.5"]["rows"] if r["case"] == "c02")
    assert gpt_c02["verdict"]["asked_outside_governing_law"] == ["governingLawText"]
    # denominators + rule provenance
    assert d["summary"]["n_unrep_total"] == 24
    assert d["summary"]["n_controls_total"] == 12
    assert "ad538c5" in d["rule"]["pre_registered"]
