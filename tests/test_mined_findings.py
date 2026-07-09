"""
CI guard for the mined-findings artifacts (novelty plan N-C, 2026-07-04) -- the same
fail-closed pattern as tests/test_raw_fidelity.py.

data/eval/{baseline_leak,forced_fill,directionality,mined_misc}.json and
data/eval/raw_fidelity_extended.json are recomputed offline from the committed
hard-suite cassettes through the gauntlet's own replay path (no API key, no network)
and FAIL on any byte drift against the committed artifacts, so a schema/prompt/
cassette change that shifts any paper-cited mined number breaks CI loudly instead of
silently. Also pins the pre-registered volunteered_flag rule (N-D) and the
harness-vs-product validate_semantics wiring fact.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from contract_drafting import mined_findings as mf
from contract_drafting import raw_fidelity as rf

_EVAL = Path(__file__).resolve().parent.parent / "data" / "eval"


@pytest.fixture(autouse=True)
def _no_api_keys(monkeypatch):
    """Fully offline: a cassette miss must raise GauntletCacheMiss, never call out."""
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------------------
# Byte-identity: every artifact regenerates byte-identically from the cassettes.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(mf._ARTIFACTS))
def test_artifact_replays_byte_identical(name, tmp_path):
    committed = mf._ARTIFACTS[name][1]
    assert committed.exists(), f"required artifact missing: {committed}"
    out = tmp_path / f"{name}.json"
    mf.write_artifact(name, out)
    assert out.read_bytes() == committed.read_bytes(), (
        f"{committed.name} drifted from what the committed cassettes replay; "
        f"regenerate with `python -m contract_drafting.mined_findings --only {name}` "
        f"and review the diff")


def test_extended_fidelity_replays_byte_identical(tmp_path):
    committed = rf.OUT_PATH_EXTENDED
    assert committed.exists(), "required artifact missing: data/eval/raw_fidelity_extended.json"
    out = tmp_path / "raw_fidelity_extended.json"
    rf.write_extended_report(out)
    assert out.read_bytes() == committed.read_bytes(), (
        "raw_fidelity_extended.json drifted; regenerate with "
        "`python -m contract_drafting.raw_fidelity --extended` and review the diff")


# ---------------------------------------------------------------------------
# baseline_leak.json -- the paper's citable leak/counterfactual numbers.
# ---------------------------------------------------------------------------
def test_baseline_leak_headline_numbers():
    d = json.loads(mf.BASELINE_LEAK_PATH.read_text(encoding="utf-8"))
    flags = {m: {a: d["models"][m][a]["flagged"] for a in d["arms"]} for m in d["models"]}
    assert flags["gpt-5.5"] == {"raw": 5, "verify_reject": 5, "constrained": 0}
    assert flags["deepseek-v4-pro"] == {"raw": 3, "verify_reject": 3, "constrained": 5}
    assert flags["deepseek-v4-flash"] == {"raw": 5, "verify_reject": 5, "constrained": 4}
    assert flags["claude-sonnet-4-6"] == {"raw": 6, "verify_reject": 6, "constrained": 0}
    p = d["pooled"]
    assert (p["raw"]["silent_wrong"], p["raw"]["counterfactual_silent_wrong"]) == (23, 5)
    assert (p["verify_reject"]["silent_wrong"],
            p["verify_reject"]["counterfactual_silent_wrong"]) == (24, 5)
    assert (p["constrained"]["silent_wrong"],
            p["constrained"]["counterfactual_silent_wrong"]) == (24, 15)
    assert all(p[a]["n"] == 24 for a in d["arms"])


def test_baseline_leak_controls_discriminativeness():
    """gpt-5.5 and sonnet emit ZERO raw-fills on the supported-law controls (their
    raws discriminate un-representable asks); the DeepSeek models echo on some."""
    d = json.loads(mf.BASELINE_LEAK_PATH.read_text(encoding="utf-8"))
    ctrl = {m: {a: d["models"][m][a]["controls"]["raw_fill_nonempty"]
                for a in d["arms"]} for m in d["models"]}
    assert ctrl["gpt-5.5"] == {"raw": 0, "verify_reject": 0, "constrained": 0}
    assert ctrl["claude-sonnet-4-6"] == {"raw": 0, "verify_reject": 0, "constrained": 0}
    assert ctrl["deepseek-v4-flash"] == {"raw": 2, "verify_reject": 2, "constrained": 2}
    assert ctrl["deepseek-v4-pro"] == {"raw": 0, "verify_reject": 0, "constrained": 1}


def test_baseline_leak_internal_consistency():
    d = json.loads(mf.BASELINE_LEAK_PATH.read_text(encoding="utf-8"))
    for arm in d["arms"]:
        for key in ("flagged", "silent_wrong", "counterfactual_silent_wrong", "n"):
            assert d["pooled"][arm][key] == sum(
                d["models"][m][arm][key if key != "n" else "n"] for m in d["models"])
        for m in d["models"]:
            rows = d["models"][m][arm]["rows"]
            assert d["models"][m][arm]["flagged"] == sum(
                r["flags_asked_jurisdiction"] for r in rows)
            assert d["models"][m][arm]["counterfactual_silent_wrong"] == sum(
                1 for r in rows if r["silent_wrong"] and not r["flags_asked_jurisdiction"])


# ---------------------------------------------------------------------------
# forced_fill.json -- the sentinel-as-confabulation-sink decomposition.
# ---------------------------------------------------------------------------
def test_forced_fill_gpt_strict_decomposition():
    d = json.loads(mf.FORCED_FILL_PATH.read_text(encoding="utf-8"))
    gpt_c = d["models"]["gpt-5.5"]["constrained"]
    assert (gpt_c["filled"], gpt_c["concrete"], gpt_c["absent"]) == (41, 41, 0)
    assert gpt_c["concrete_value_distribution"] == {
        "AAA_ICDR": 20, "LCIA": 7, "SIAC": 7, "JAMS": 3, "DIAC": 2, "ICC": 1, "HKIAC": 1}
    gpt_d = d["models"]["gpt-5.5"]["constrained_hatch"]
    assert (gpt_d["filled"], gpt_d["sentinel"], gpt_d["concrete"], gpt_d["absent"]) == (41, 32, 9, 0)
    link = d["gpt_arm_d_siac_singapore_linkage"]
    assert (link["concrete_total"], link["siac_on_singapore_governing_law_cases"]) == (9, 7)


def test_forced_fill_pooled_and_per_model():
    d = json.loads(mf.FORCED_FILL_PATH.read_text(encoding="utf-8"))
    pc = d["pooled_unrequested_concrete"]
    assert pc == {"denominator": 164, "raw": 39, "constrained": 67, "constrained_hatch": 13}
    concrete = {m: {a: d["models"][m][a]["concrete"] for a in d["arms"]} for m in d["models"]}
    assert concrete["gpt-5.5"] == {"raw": 3, "constrained": 41, "constrained_hatch": 9}
    assert concrete["deepseek-v4-pro"] == {"raw": 8, "constrained": 3, "constrained_hatch": 1}
    assert concrete["deepseek-v4-flash"] == {"raw": 10, "constrained": 12, "constrained_hatch": 3}
    assert concrete["claude-sonnet-4-6"] == {"raw": 18, "constrained": 11, "constrained_hatch": 0}
    # sonnet's 18 raw-arm inventions are voluntary (no strict grammar forces them);
    # the hatch takes them to 0.
    filled_d = {m: d["models"][m]["constrained_hatch"]["filled"] for m in d["models"]}
    assert filled_d == {"gpt-5.5": 41, "deepseek-v4-pro": 2,
                        "deepseek-v4-flash": 8, "claude-sonnet-4-6": 2}


def test_forced_fill_internal_consistency():
    d = json.loads(mf.FORCED_FILL_PATH.read_text(encoding="utf-8"))
    assert d["n_cases"] == 41
    for m in d["models"]:
        for a in d["arms"]:
            cell = d["models"][m][a]
            assert cell["filled"] == cell["sentinel"] + cell["concrete"] + cell["non_enum"]
            assert cell["filled"] + cell["absent"] == 41
            assert cell["concrete"] == len(cell["concrete_rows"])
            assert cell["concrete"] == sum(cell["concrete_value_distribution"].values())


# ---------------------------------------------------------------------------
# directionality.json -- nearest-representable coercion + the entity folk matrix.
# ---------------------------------------------------------------------------
def test_directionality_headline():
    d = json.loads(mf.DIRECTIONALITY_PATH.read_text(encoding="utf-8"))
    s = d["summary"]
    assert s["filled_wrong_total"] == 46  # 44 on a relative + c01's 2 divergent fills
    assert s["filled_on_in_enum_relative"] == 44
    assert s["filled_wrong_where_relative_exists"] == 44
    cells = {cid: d["cases"][cid]["cells"] for cid in d["cases"]}
    assert cells["c03"] == {"parent": 12}                       # DIFC -> UAE 12/12
    assert cells["c08"] == {"omit": 6, "parent": 6}             # DIFC -> UAE 6/6 filled
    assert cells["c06"] == {"constituent": 12}                  # US federal -> New_York
    assert d["cases"]["c06"]["filled_values"] == {"New_York": 12}
    assert cells["c02"] == {"omit": 6, "sibling": 6}            # Scotland -> E&W 6/6
    assert cells["c04"] == {"omit": 4, "parent": 7, "sibling": 1}  # Macao -> PRC/HK
    assert d["cases"]["c04"]["filled_values"] == {
        "Peoples_Republic_of_China": 7, "Hong_Kong_SAR": 1}
    # Ontario: NO in-enum relative -> scatters (9 omit + 3 divergent cells)
    assert cells["c01"] == {"omit": 9, "unrelated": 2, "verbatim_leak": 1}


def test_directionality_c04_sibling_is_sonnet():
    d = json.loads(mf.DIRECTIONALITY_PATH.read_text(encoding="utf-8"))
    sib = [r for r in d["cases"]["c04"]["rows"] if r["relation"] == "sibling"]
    assert len(sib) == 1 and sib[0]["model"] == "claude-sonnet-4-6"


def test_entity_folk_matrix():
    d = json.loads(mf.DIRECTIONALITY_PATH.read_text(encoding="utf-8"))
    fm = d["entity_folk_matrix"]
    assert (fm["n_probes"], fm["probes_with_3of4_agreement"]) == (12, 10)
    probes = fm["probes"]
    # GmbH -> limited_liability_company 4/4; cooperative -> general_partnership 4/4;
    # KK / SA / plc / Pte / ULC -> corporation 4/4.
    assert (probes["ec01"]["top_form"], probes["ec01"]["models_agreeing"]) == (
        "limited_liability_company", 4)
    assert (probes["ec11"]["top_form"], probes["ec11"]["models_agreeing"]) == (
        "general_partnership", 4)
    for cid in ("ec02", "ec03", "ec06", "ec07", "ec09"):
        assert (probes[cid]["top_form"], probes[cid]["models_agreeing"]) == (
            "corporation", 4), cid


# ---------------------------------------------------------------------------
# mined_misc.json -- the typed-surface pair, dates, and the invented sentinel.
# ---------------------------------------------------------------------------
def test_typed_surface_pair():
    d = json.loads(mf.MINED_MISC_PATH.read_text(encoding="utf-8"))
    tp = d["typed_surface_pair"]
    assert (tp["c20"]["blocked"], tp["c20"]["n"]) == (16, 16)
    assert (tp["c21"]["pass_with_no_structured_flag"], tp["c21"]["n"]) == (16, 16)
    # provenance: the suite's own why_hard text predicted the pair
    assert "playbook" in tp["c20"]["why_hard"]
    assert "gate=PASS" in tp["c21"]["why_hard"] or "gate PASS" in tp["c21"]["why_hard"]


def test_impossible_dates_and_silenced_objection():
    d = json.loads(mf.MINED_MISC_PATH.read_text(encoding="utf-8"))
    dd = d["impossible_dates"]
    assert dd["cells_shipping_impossible_date_schema_valid_pass"] == 24
    assert dd["n_cells_gpt_pro_flash"] == 24
    son = dd["sonnet"]
    assert son["c13"]["objection_quote_verbatim"] == (
        "February 30 does not exist on any calendar.")
    assert son["c14"]["objection_quote_verbatim"] == (
        "February 31, 2026 does not exist.")
    assert son["c13"]["constrained_shipped_effectiveDate"] == "2026-02-28"
    assert son["c14"]["constrained_shipped_effectiveDate"] == "2026-04-30"
    # the raw arm's (unlandable) fenced JSON proposed 2026-02-28 for BOTH cases --
    # sonnet's two decoding regimes disagree about the c14 correction itself.
    assert son["c13"]["raw_proposed_effectiveDate"] == "2026-02-28"
    assert son["c14"]["raw_proposed_effectiveDate"] == "2026-02-28"


def test_production_semantics_wiring():
    """The wiring fact stated in the artifact must hold in the LIVE code: production
    validate_semantics rejects Feb-30/Feb-31, and both draft paths call it."""
    from contract_drafting.schema_validator import validate_semantics
    for bad in ("2026-02-30", "2026-02-31"):
        errs = validate_semantics({"effectiveDate": bad}, template_name="nda-mutual")
        assert errs and "not a real calendar date" in errs[0]
    assert not validate_semantics({"effectiveDate": "2026-02-28"},
                                  template_name="nda-mutual")
    sites = mf._semantics_call_sites()
    assert sites == ["contract_drafting/compliance_draft.py::_draft_cicero",
                     "contract_drafting/compliance_draft.py::_draft_llm"]


def test_c11_confabulation_and_invented_sentinel():
    d = json.loads(mf.MINED_MISC_PATH.read_text(encoding="utf-8"))
    c11 = d["c11_event_relative_date"]
    assert (c11["filled_concrete"], c11["n"]) == (16, 16)
    assert c11["before_recording_window"] == 12
    assert c11["distinct_values"] == 10
    inv = d["invented_sentinel"]
    assert inv["effectiveDate"] == "<UNKNOWN>" and inv["schema_valid"] is False


# ---------------------------------------------------------------------------
# raw_fidelity_extended.json -- the cross-field raw-capture fidelity split.
# ---------------------------------------------------------------------------
def test_extended_fidelity_headline_numbers():
    d = json.loads(rf.OUT_PATH_EXTENDED.read_text(encoding="utf-8"))
    forum = d["groups"]["disputeForumRaw"]["summary"]
    entity = d["groups"]["entityTypeRaw"]["summary"]
    assert (forum["faithful"], forum["n_total"], forum["excluded_not_abstained"]) == (48, 48, 0)
    assert (entity["faithful"], entity["n_total"], entity["excluded_not_abstained"]) == (38, 38, 10)
    c = d["combined_with_committed_law"]
    assert (c["law_faithful"], c["law_n"]) == (24, 24)
    assert (c["combined_faithful"], c["combined_n"]) == (110, 110)


def test_extended_fidelity_internal_consistency():
    d = json.loads(rf.OUT_PATH_EXTENDED.read_text(encoding="utf-8"))
    for gr in d["groups"].values():
        for metric in ("faithful", "verbatim", "ci_substring"):
            assert gr["summary"][metric] == sum(m[metric] for m in gr["models"].values())
            for m in gr["models"].values():
                assert m[metric] == sum(bool(r[metric]) for r in m["rows"])
        assert all(r["raw"] for m in gr["models"].values() for r in m["rows"])


def test_extended_roster_guard(monkeypatch):
    """A roster/suite drift must raise BEFORE any number is produced (same guard
    discipline as the committed law grader)."""
    monkeypatch.setitem(rf._EXT_KEY_NAMES, "fc99", ("x",))
    with pytest.raises(RuntimeError, match="_EXT_KEY_NAMES"):
        rf.compute_extended()


# ---------------------------------------------------------------------------
# volunteered_flag -- the PRE-REGISTERED clean-prompt grader (N-D, 2026-07-04).
# Pinned here BEFORE the cleanbase recording so the rule cannot drift silently.
# ---------------------------------------------------------------------------
def test_volunteered_flag_asked_outside_chosen_value():
    v = rf.volunteered_flag("c02", {"governingLaw": "England_and_Wales",
                                    "governingLawRaw": "Scotland"})
    assert v["flagged"] and v["asked_outside_governing_law"] == ["governingLawRaw"]
    # the asked value INSIDE the chosen governingLaw slot is the fill itself, not a flag
    v = rf.volunteered_flag("c02", {"governingLaw": "Scotland"})
    assert not v["flagged"]
    # any other surface counts, including non-string values and invented keys
    v = rf.volunteered_flag("c03", {"governingLaw": "United_Arab_Emirates",
                                    "notes": {"asked": "DIFC"}})
    assert v["flagged"] and v["asked_outside_governing_law"] == ["notes"]


def test_volunteered_flag_caveat_markers():
    v = rf.volunteered_flag("c01", {"governingLaw": "Washington",
                                    "purpose": "closest supported jurisdiction used"})
    assert v["flagged"] and v["caveat_marker_fields"] == ["purpose"]
    # whole words only: 'noted'/'cannotate' must not trip the 'note'/'cannot' markers
    v = rf.volunteered_flag("c01", {"governingLaw": "Washington",
                                    "purpose": "as noted previously, denoted terms"})
    assert not v["flagged"]


def test_volunteered_flag_on_committed_gpt_c02_raw_fill():
    """The committed gpt-5.5 arm-A c02 fill (which omits governingLaw but carries
    governingLawRaw='Scotland' via the definitions-block leak) counts as a
    volunteered flag under the pre-registered rule -- the leak-era anchor case."""
    d = json.loads(mf.BASELINE_LEAK_PATH.read_text(encoding="utf-8"))
    row = next(r for r in d["models"]["gpt-5.5"]["raw"]["rows"] if r["case"] == "c02")
    assert row["governingLawRaw"] == "Scotland" and row["outcome"] == "omit"
    v = rf.volunteered_flag("c02", {"governingLawRaw": row["governingLawRaw"]})
    assert v["flagged"]
