"""
Offline tests for the six-beat demo driver (contract_drafting/demo_offline.py) and the
beat-6 escalation semantics in the production draft path (compliance_draft.py).

EVERY test runs with all provider API keys unset (autouse fixture): the demo replays
committed cassettes via the Gauntlet's RecordReplayCaller and fails closed on any
cache miss, so a network call is structurally impossible.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

import pytest

from contract_drafting import demo_offline as do
from contract_drafting.compliance_draft import (
    DraftRequest,
    _escalation_sha256,
    draft_contract,
    get_audit_log,
)

_REPO = Path(__file__).resolve().parent.parent
_CTO = _REPO / "data" / "templates" / "cicero" / "nda-mutual" / "model" / "model.cto"


@pytest.fixture(autouse=True)
def _no_api_keys(monkeypatch):
    """The demo MUST be fully offline: unset every provider key for every test."""
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def ctx(tmp_path):
    """A demo context with an isolated audit DB and docx output dir."""
    return {"model": "gpt-5.5", "db": str(tmp_path / "audit.db"),
            "out_dir": str(tmp_path / "drafts")}


# ---------------------------------------------------------------------------
# Beat 1 -- the invisible failure (constrained arm, silent substitution)
# ---------------------------------------------------------------------------
class TestBeat1:
    def test_renders_substituted_law_with_no_warning(self, ctx, capsys):
        out = do.beat1(ctx)
        stdout = capsys.readouterr().out
        # the replayed constrained fill silently substituted England_and_Wales
        assert out["fields"]["governingLaw"] == "England_and_Wales"
        assert out["schema_errors"] == []                      # validator green
        assert out["result"]["gate_result"] == "PASS"          # playbook gate
        assert out["result"]["audit_id"] >= 1                  # audit row written
        assert "England and Wales" in out["clause"]            # the rendered clause
        # the point of the beat: NOTHING on screen warns (exclude filesystem path
        # lines -- pytest's tmp_path embeds this very test's name in the docx path)
        low = "\n".join(ln for ln in stdout.lower().splitlines() if ".docx" not in ln)
        for banned in ("warning", "flagged", "abstain", "substitut", "escalat"):
            assert banned not in low, f"beat 1 must not print {banned!r}"

    def test_audit_row_carries_render_sha256_of_exact_bytes(self, ctx):
        out = do.beat1(ctx)
        result = out["result"]
        rows = get_audit_log(ctx["db"], doc_id=result["audit_id"])
        assert len(rows) == 1
        slot = json.loads(rows[0]["slot_values"])
        assert slot["render_sha256"] == result["render_sha256"]
        assert re.fullmatch(r"[0-9a-f]{64}", slot["render_sha256"])
        # P1: the draft result carries the EXACT rendered bytes (no second render in
        # the beats); its sha must equal the audited render_sha256 ...
        assert hashlib.sha256(result["rendered_text"].encode("utf-8")).hexdigest() \
            == slot["render_sha256"]
        # ... and rendered_text must never leak into the audit row's slot_values
        assert "rendered_text" not in slot
        # the sha is over the EXACT deterministic Cicero render bytes: an INDEPENDENT
        # re-render of the same request yields byte-identical text
        req = do._request_from_fields(out["fields"], out_tag="t",
                                      out_dir=ctx["out_dir"])
        text = do._render_markdown(req)
        assert hashlib.sha256(text.encode("utf-8")).hexdigest() == slot["render_sha256"]
        assert text == result["rendered_text"]


# ---------------------------------------------------------------------------
# Beat 2 -- the type-error explainer (static .cto analysis, FILE:LINE)
# ---------------------------------------------------------------------------
class TestBeat2Explainer:
    def test_locates_governing_law_field_and_enum_lines(self):
        info = do.explain_field("governingLaw", "laws of Scotland")
        lines = _CTO.read_text().splitlines()
        assert "o Jurisdiction governingLaw" in lines[info["field_line"] - 1]
        assert re.search(r"enum\s+Jurisdiction\s*\{", lines[info["enum_start"] - 1])
        assert lines[info["enum_end"] - 1].strip() == "}"
        member_lines = dict(info["members"])
        assert lines[member_lines["OTHER"] - 1].strip() == "o OTHER"
        assert lines[member_lines["England_and_Wales"] - 1].strip() == "o England_and_Wales"
        assert info["n_members"] == 65   # 64 jurisdictions + OTHER sentinel
        assert info["sentinel"] == "OTHER"
        assert lines[info["raw_line"] - 1].strip().startswith("o String governingLawRaw")
        # Scotland is un-representable
        assert info["representable"] is False

    def test_representable_asks_resolve(self):
        assert do.explain_field("governingLaw", "laws of the State of New York")[
            "representable"] is True
        assert do.explain_field("governingLaw", "New York")["resolved"] == "New_York"

    def test_entity_type_field(self):
        info = do.explain_field("entityType", "GmbH")  # alias -> receivingEntityType
        lines = _CTO.read_text().splitlines()
        assert "o EntityType receivingEntityType" in lines[info["field_line"] - 1]
        assert info["enum"] == "EntityType"
        assert info["sentinel"] == "OTHER_ENTITY"
        assert info["representable"] is False
        assert do.explain_field("disclosingEntityType",
                                "limited liability company")["representable"] is True

    def test_dispute_forum_field(self):
        info = do.explain_field("disputeForum", "KCAB")
        lines = _CTO.read_text().splitlines()
        assert "o DisputeForum disputeForum" in lines[info["field_line"] - 1]
        assert info["sentinel"] == "OTHER_FORUM"
        assert info["representable"] is False
        assert do.explain_field("disputeForum", "SIAC")["representable"] is True

    def test_unknown_field_fails_loudly(self):
        with pytest.raises(ValueError):
            do.explain_field("noSuchField", "x")

    def test_beat2_shows_beat1_substitution(self, ctx, capsys):
        do.beat2(ctx)
        stdout = capsys.readouterr().out
        assert "England_and_Wales" in stdout
        assert "model.cto" in stdout
        assert "un-representable" in stdout


# ---------------------------------------------------------------------------
# Beat 3 -- hatch arm + fail-closed escalation + retroactive intent gate
# ---------------------------------------------------------------------------
class TestBeat3:
    def test_hatch_abstains_and_escalates_with_audit_row(self, ctx):
        out = do.beat3(ctx)
        assert out["hatch_fields"]["governingLaw"] == "OTHER"
        assert out["hatch_fields"]["governingLawRaw"] == "laws of Scotland"
        res = out["result"]
        assert res["gate_result"] == "ESCALATED"      # human review, not BLOCKED
        assert res["abstained"] is True
        assert res["output_path"] is None             # fail-closed: nothing rendered
        assert res["abstained_fields"] == ["governingLaw"]
        rows = get_audit_log(ctx["db"], doc_id=res["audit_id"])
        slot = json.loads(rows[0]["slot_values"])
        assert rows[0]["gate_result"] == "ESCALATED"
        assert slot["abstentions"][0]["raw"] == "laws of Scotland"
        assert re.fullmatch(r"[0-9a-f]{64}", slot["escalation_sha256"])

    def test_intent_gate_flags_beat1_draft(self, ctx):
        out = do.beat3(ctx)
        assert out["asked"] == ["Scotland"]
        assert out["warnings"], "the offline intent gate must flag beat 1's substitution"


# ---------------------------------------------------------------------------
# Beat 4 -- the calibration race (supported-law controls, no over-abstention)
# ---------------------------------------------------------------------------
class TestBeat4:
    def test_controls_fill_correctly_no_abstention(self, ctx, capsys):
        out = do.beat4(ctx)
        stdout = capsys.readouterr().out
        assert out["over_abstain"] == 0
        assert out["fills"]["c27"]["governingLaw"] == "New_York"
        assert out["fills"]["c28"]["governingLaw"] == "Republic_of_Singapore"
        assert out["fills"]["c29"]["governingLaw"] == "England_and_Wales"
        # one control rendered through the production path, with the footer
        assert out["result"]["gate_result"] == "PASS"
        assert "not legal advice" in out["result"]["disclaimer"]
        assert "conformance only" in stdout
        assert "0/3" in stdout


# ---------------------------------------------------------------------------
# M6: beat 4's displayed asks are DERIVED from the suite, not hardcoded
# ---------------------------------------------------------------------------
class TestBeat4DerivedAsks:
    def test_control_asks_extracted_from_instructions(self):
        """The derived asks must equal the previously hardcoded display strings
        (same laws named) for the current committed suite."""
        assert do._control_ask(do._case("c27")) == "laws of the State of New York"
        assert do._control_ask(do._case("c28")) == "laws of the Republic of Singapore"
        assert do._control_ask(do._case("c29")) == "laws of England and Wales"

    def test_fallback_uses_expected_correct_display_name(self):
        from contract_drafting.gauntlet import Case
        c = Case(id="cx", field="governingLaw", defect_class="supported-law-control",
                 instruction="Apply NY law throughout.",  # defeats the regex on purpose
                 expected_correct="New_York")
        assert do._control_ask(c) == "laws of New York"

    def test_beat4_prints_derived_asks(self, ctx, capsys):
        do.beat4(ctx)
        stdout = capsys.readouterr().out
        assert "laws of the State of New York" in stdout
        assert "laws of the Republic of Singapore" in stdout
        assert "laws of England and Wales" in stdout


# ---------------------------------------------------------------------------
# M7: the frozen Table 1 denominators are cross-checked against the suite flags
# ---------------------------------------------------------------------------
class TestTable1Denominators:
    def test_frozen_tuples_match_suite_derivation(self):
        d = do.derive_table1_sets()
        assert tuple(sorted(d["adjudicated"])) == tuple(sorted(do.ADJUDICATED))
        assert tuple(sorted(d["gov_unrep"])) == tuple(sorted(do.GOV_UNREP))
        assert tuple(sorted(d["controls"])) == tuple(sorted(do.CONTROLS))

    def test_compute_table1_fails_loud_on_denominator_drift(self, monkeypatch):
        monkeypatch.setattr(do, "ADJUDICATED", ("c01", "c02"))  # simulate drift
        with pytest.raises(RuntimeError, match="denominator drift"):
            do.compute_table1()


# ---------------------------------------------------------------------------
# M3: the field->enum map is single-sourced from the generated abstain policy
# ---------------------------------------------------------------------------
class TestSingleSourcedFieldEnumMap:
    def test_derived_map_value_identical_to_legacy_literals(self):
        """The policy-derived map must equal the mapping previously hand-duplicated in
        demo_mars_beat._ENUM_DISPLAY_FIELDS / gauntlet._FIELD_ENUM / demo_offline."""
        from contract_drafting.schema_validator import enum_display_fields
        assert enum_display_fields("nda-mutual") == {
            "disclosingEntityType": "EntityType",
            "receivingEntityType": "EntityType",
            "disputeForum": "DisputeForum",
        }

    def test_template_without_abstain_policy_yields_empty_map(self):
        from contract_drafting.schema_validator import enum_display_fields
        assert enum_display_fields("joint-venture") == {}


# ---------------------------------------------------------------------------
# Beat 5 -- Table 1 recomputed from the committed results files
# ---------------------------------------------------------------------------
class TestBeat5Table1:
    # the paper's Table 1, re-anchored on the six un-representable governing-law asks
    # (silent-wrong /6), columns gpt-5.5 / v4-pro / v4-flash / sonnet-4.6
    _EXPECTED = {
        "raw":               {"gpt-5.5": 6, "deepseek-v4-pro": 6,
                              "deepseek-v4-flash": 6, "claude-sonnet-4-6": 5},
        "verify_reject":     {"gpt-5.5": 6, "deepseek-v4-pro": 6,
                              "deepseek-v4-flash": 6, "claude-sonnet-4-6": 6},
        "constrained":       {"gpt-5.5": 6, "deepseek-v4-pro": 6,
                              "deepseek-v4-flash": 6, "claude-sonnet-4-6": 6},
        "constrained_hatch": {"gpt-5.5": 0, "deepseek-v4-pro": 0,
                              "deepseek-v4-flash": 0, "claude-sonnet-4-6": 0},
    }
    # the three numeric/intent cases (c15-c17), reported separately /3 -- the difference
    # between the old pooled /9 and the re-anchored /6 headline (arithmetic-neutral).
    _NUMERIC = {
        "raw":               {"gpt-5.5": 1, "deepseek-v4-pro": 2,
                              "deepseek-v4-flash": 1, "claude-sonnet-4-6": 1},
        "verify_reject":     {"gpt-5.5": 1, "deepseek-v4-pro": 2,
                              "deepseek-v4-flash": 1, "claude-sonnet-4-6": 1},
        "constrained":       {"gpt-5.5": 1, "deepseek-v4-pro": 1,
                              "deepseek-v4-flash": 1, "claude-sonnet-4-6": 1},
        "constrained_hatch": {"gpt-5.5": 1, "deepseek-v4-pro": 0,
                              "deepseek-v4-flash": 1, "claude-sonnet-4-6": 1},
    }

    def test_numbers_match_committed_results_exactly(self):
        t = do.compute_table1()
        assert t["n"] == 6
        assert t["silent_wrong"] == self._EXPECTED
        assert t["n_numeric"] == 3
        assert t["numeric"] == self._NUMERIC
        # gov-law /6 + numeric /3 must equal the old pooled /9 (arithmetic-neutral re-anchor)
        for arm in do._ARMS:
            for m in do.MODELS:
                assert t["silent_wrong"][arm][m] + t["numeric"][arm][m] == {
                    "raw":               {"gpt-5.5": 7, "deepseek-v4-pro": 8,
                                          "deepseek-v4-flash": 7, "claude-sonnet-4-6": 6},
                    "verify_reject":     {"gpt-5.5": 7, "deepseek-v4-pro": 8,
                                          "deepseek-v4-flash": 7, "claude-sonnet-4-6": 7},
                    "constrained":       {"gpt-5.5": 7, "deepseek-v4-pro": 7,
                                          "deepseek-v4-flash": 7, "claude-sonnet-4-6": 7},
                    "constrained_hatch": {"gpt-5.5": 1, "deepseek-v4-pro": 0,
                                          "deepseek-v4-flash": 1, "claude-sonnet-4-6": 1},
                }[arm][m]
        # the 6/6 abstain + 0/3 over-abstain line, all four models
        for model in do.MODELS:
            assert t["abstain"][model] == (6, 6), model
            assert t["over_abstain"][model] == (0, 3), model

    def test_table_prints_aligned_paper_layout(self, ctx, capsys):
        t0 = __import__("time").time()
        do.beat5(ctx)
        elapsed = __import__("time").time() - t0
        # Generous CI-safe bound only. The "<1s" figure is a paper/demo claim measured
        # separately on a quiet machine; asserting it here is flaky under CI load.
        assert elapsed < 10.0, f"beat 5 must stay fast (offline recompute), took {elapsed:.2f}s"
        stdout = capsys.readouterr().out
        for col in ("gpt-5.5", "v4-pro", "v4-flash", "sonnet-4.6"):
            assert col in stdout
        # headline gov-law /6 grid
        assert re.search(r"A raw\s+6\s+6\s+6\s+5", stdout)
        assert re.search(r"B verify-reject\s+6\s+6\s+6\s+6", stdout)
        assert re.search(r"C constrained\s+6\s+6\s+6\s+6", stdout)
        assert re.search(r"D \+abstention\s+0\s+0\s+0\s+0", stdout)
        # separate numeric/intent /3 grid below the headline
        assert "un-representable governing-law asks" in stdout
        assert "numeric/intent" in stdout
        assert re.search(r"A raw\s+1\s+2\s+1\s+1", stdout)
        assert re.search(r"D \+abstention\s+1\s+0\s+1\s+1", stdout)
        assert "6/6" in stdout and "0/3" in stdout

    def test_case_drill_down(self, ctx, capsys):
        do.beat5(ctx, case_id="c02")
        stdout = capsys.readouterr().out
        assert "laws of Scotland" in stdout                   # the case's prompt
        assert "England_and_Wales" in stdout                  # constrained fill
        assert "OTHER" in stdout                              # hatch fill
        assert "abstained" in stdout and "wrong_sub" in stdout  # per-arm outcomes


# ---------------------------------------------------------------------------
# Beat 6 -- the audit row closes the loop
# ---------------------------------------------------------------------------
class TestBeat6:
    def test_prints_escalated_row_with_verified_sha(self, ctx, capsys):
        do.beat3(ctx)
        out = do.beat6(ctx)
        stdout = capsys.readouterr().out
        assert out["row"]["gate_result"] == "ESCALATED"
        assert out["slot"]["abstentions"][0]["raw"] == "laws of Scotland"
        assert out["sha_match"] is True
        assert "ESCALATED" in stdout and "laws of Scotland" in stdout
        assert re.search(r"[0-9a-f]{64}", stdout)

    def test_standalone_beat6_creates_escalation_on_fresh_db(self, ctx):
        out = do.beat6(ctx)  # no prior beat 3: must self-provision the escalation
        assert out["row"]["gate_result"] == "ESCALATED"
        assert out["sha_match"] is True


# ---------------------------------------------------------------------------
# Production draft path: abstention -> ESCALATED (+audit), violations -> BLOCKED
# ---------------------------------------------------------------------------
class TestEscalationRouting:
    def test_other_escalates_fail_closed_with_raw_in_audit(self, tmp_path):
        db = str(tmp_path / "a.db")
        res = draft_contract(DraftRequest(
            disclosing_party="A", receiving_party="B", effective_date="2026-01-15",
            governing_law="OTHER", governing_law_raw="laws of Scotland"), db_path=db)
        assert res["gate_result"] == "ESCALATED"
        assert res["abstained"] is True
        assert res["output_path"] is None
        slot = json.loads(get_audit_log(db, doc_id=res["audit_id"])[0]["slot_values"])
        assert slot["reason"] == "typed-abstention"
        assert slot["abstentions"] == [
            {"field": "governingLaw", "sentinel": "OTHER", "raw": "laws of Scotland"}]
        # the stored sha is over the canonical record (minus the sha field itself)
        record = {k: v for k, v in slot.items() if k != "escalation_sha256"}
        assert _escalation_sha256(record) == slot["escalation_sha256"]

    def test_entity_sentinel_escalates_with_raw(self, tmp_path):
        db = str(tmp_path / "a.db")
        res = draft_contract(DraftRequest(
            disclosing_party="A", receiving_party="B", effective_date="2026-01-15",
            disclosing_entity_type="OTHER_ENTITY", disclosing_entity_type_raw="GmbH"),
            db_path=db)
        assert res["gate_result"] == "ESCALATED"
        assert res["abstained_fields"] == ["disclosingEntityType"]
        slot = json.loads(get_audit_log(db, doc_id=res["audit_id"])[0]["slot_values"])
        assert slot["abstentions"][0]["raw"] == "GmbH"

    def test_multiple_abstentions_in_one_row(self, tmp_path):
        db = str(tmp_path / "a.db")
        res = draft_contract(DraftRequest(
            disclosing_party="A", receiving_party="B", effective_date="2026-01-15",
            governing_law="OTHER", governing_law_raw="laws of Scotland",
            receiving_entity_type="OTHER_ENTITY", receiving_entity_type_raw="plc"),
            db_path=db)
        assert res["gate_result"] == "ESCALATED"
        assert set(res["abstained_fields"]) == {"governingLaw", "receivingEntityType"}
        slot = json.loads(get_audit_log(db, doc_id=res["audit_id"])[0]["slot_values"])
        assert len(slot["abstentions"]) == 2

    def test_double_entity_abstention_keeps_both_raws(self, tmp_path):
        """M8 regression: when BOTH entity types abstain, each raw ask survives under
        its own per-field key; the legacy aggregate 'entity_type_raw' keeps the FIRST
        abstained entity's raw (back-compat) instead of being silently overwritten."""
        db = str(tmp_path / "a.db")
        res = draft_contract(DraftRequest(
            disclosing_party="A", receiving_party="B", effective_date="2026-01-15",
            disclosing_entity_type="OTHER_ENTITY", disclosing_entity_type_raw="GmbH",
            receiving_entity_type="OTHER_ENTITY", receiving_entity_type_raw="Anstalt"),
            db_path=db)
        assert res["gate_result"] == "ESCALATED"
        assert set(res["abstained_fields"]) == {"disclosingEntityType",
                                                "receivingEntityType"}
        assert res["disclosing_entity_type_raw"] == "GmbH"
        assert res["receiving_entity_type_raw"] == "Anstalt"
        assert res["entity_type_raw"] == "GmbH"  # first abstained entity, back-compat

    def test_genuine_playbook_violation_still_blocked(self, tmp_path):
        db = str(tmp_path / "a.db")
        res = draft_contract(DraftRequest(
            disclosing_party="A", receiving_party="B", effective_date="2026-01-15",
            governing_law="Washington", has_non_compete=True), db_path=db)
        assert res["gate_result"] == "BLOCKED"   # critical violation: unchanged
        assert res.get("abstained") is None
        assert get_audit_log(db, doc_id=res["audit_id"])[0]["gate_result"] == "BLOCKED"

    def test_pass_path_unchanged_except_render_sha(self, tmp_path):
        db = str(tmp_path / "a.db")
        out = str(tmp_path / "x.docx")
        res = draft_contract(DraftRequest(
            disclosing_party="A", receiving_party="B", effective_date="2026-01-15",
            governing_law="Washington", output_path=out), db_path=db)
        assert res["gate_result"] == "PASS"
        assert res["output_path"] == out
        assert re.fullmatch(r"[0-9a-f]{64}", res["render_sha256"])
        slot = json.loads(get_audit_log(db, doc_id=res["audit_id"])[0]["slot_values"])
        assert slot["render_sha256"] == res["render_sha256"]


# ---------------------------------------------------------------------------
# CLI end-to-end (the video script) -- offline, keys unset
# ---------------------------------------------------------------------------
class TestCLI:
    def test_all_runs_offline(self, ctx, capsys):
        rc = do.main(["all", "--db", ctx["db"], "--out-dir", ctx["out_dir"]])
        assert rc == 0
        stdout = capsys.readouterr().out
        for n in range(1, 7):
            assert f"BEAT {n}" in stdout
        assert "England and Wales" in stdout      # beat 1's silent substitution
        assert "ESCALATED" in stdout              # beat 3/6
        assert "no API key" in stdout

    def test_no_arg_invocation_runs_full_demo(self, tmp_path, monkeypatch, capsys):
        """A-M4: the paper's verbatim command `python -m contract_drafting.demo_offline`
        (NO subcommand) defaults to 'all' -- the full six beats run offline (keys unset
        by the autouse fixture) and exit 0. The audit DB and docx out-dir defaults are
        monkeypatched to tmp_path so the no-arg run never touches data/."""
        monkeypatch.setattr(do, "DEFAULT_DB", str(tmp_path / "audit.db"))
        orig = do._request_from_fields
        monkeypatch.setattr(
            do, "_request_from_fields",
            lambda fields, *, out_tag, out_dir=None: orig(
                fields, out_tag=out_tag, out_dir=out_dir or str(tmp_path / "drafts")))
        rc = do.main([])
        assert rc == 0
        stdout = capsys.readouterr().out
        for n in range(1, 7):
            assert f"BEAT {n}" in stdout
        assert "no API key" in stdout

    def test_help_exits_zero(self):
        """--help must keep working with the optional positional (argparse exits 0)."""
        with pytest.raises(SystemExit) as e:
            do.main(["--help"])
        assert e.value.code == 0

    def test_explain_alias(self, ctx, capsys):
        rc = do.main(["explain", "--field", "governingLaw",
                      "--asked", "laws of Scotland", "--db", ctx["db"]])
        assert rc == 0
        assert "un-representable" in capsys.readouterr().out

    def test_table1_alias(self, ctx, capsys):
        rc = do.main(["table1", "--db", ctx["db"]])
        assert rc == 0
        assert "silent-wrong" in capsys.readouterr().out

    def test_beat_requires_number(self, ctx):
        with pytest.raises(SystemExit):
            do.main(["beat", "--db", ctx["db"]])

    def test_nondefault_model(self, ctx, capsys):
        ctx2 = dict(ctx, model="deepseek-v4-pro")
        out = do.beat1(ctx2)
        # v4-pro's committed c02 constrained outcome is omit -> the DRIVER's
        # 'Washington' default (injected by _request_from_fields) renders, and the
        # beat narrates it as an omit, not as the model's fill (RT4)
        assert out["omitted"] is True
        assert out["result"]["gate_result"] in ("PASS", "ESCALATED")

    def test_unknown_case_id_friendly_error(self, ctx, capsys):
        """RT6: an unknown --case id exits with one friendly line listing the valid
        ids (sorted), a nonzero rc, and no traceback."""
        rc = do.main(["table1", "--case", "zzz", "--db", ctx["db"]])
        assert rc == 2
        out = capsys.readouterr().out
        assert "unknown case id 'zzz'" in out
        assert "c01" in out and "c02" in out     # lists valid ids
        assert "Traceback" not in out

    def test_prereq_failure_unopenable_db_rc3(self, ctx, tmp_path, capsys):
        """RT3: an audit DB that sqlite cannot open (a directory) is a demo
        PREREQUISITE failure -- one line, rc 3, no raw traceback."""
        rc = do.main(["beat", "3", "--db", str(tmp_path),    # a DIRECTORY, not a file
                      "--out-dir", ctx["out_dir"]])
        assert rc == 3
        out = capsys.readouterr().out
        assert "DEMO PREREQ FAILED" in out
        assert "Traceback" not in out


# ---------------------------------------------------------------------------
# Replay fail-closed (T1): an empty cassette must never fall through to a live call
# ---------------------------------------------------------------------------
class TestReplayFailClosed:
    def test_cache_miss_fails_closed_rc2(self, ctx, tmp_path, monkeypatch, capsys):
        """T1: force a replay miss (empty cassette) -> GauntletCacheMiss -> main
        exits rc 2 with the REPLAY FAILED line. Fail-closed: no silent live call."""
        from contract_drafting.gauntlet import RecordReplayCaller
        empty = tmp_path / "empty_cassette.json"
        empty.write_text("{}", encoding="utf-8")
        miss = RecordReplayCaller(None, empty, mode="replay")
        monkeypatch.setattr(do, "_callers", {m: miss for m in do.MODELS})
        rc = do.main(["beat", "1", "--db", ctx["db"], "--out-dir", ctx["out_dir"]])
        assert rc == 2
        assert "REPLAY FAILED" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Beat 1 across every committed model cassette (T7) -- incl. the omit narration
# ---------------------------------------------------------------------------
class TestBeat1AllModels:
    @pytest.mark.parametrize("model", list(do.MODELS))
    def test_beat1_completes_offline(self, model, tmp_path):
        """T7: beat 1 replays fully offline for all four MODELS keys; models whose
        replayed c02 constrained fill omits governingLaw take the omit-narration
        path (RT4) instead of presenting the driver's default as the model's fill."""
        ctx = {"model": model, "db": str(tmp_path / "audit.db"),
               "out_dir": str(tmp_path / "drafts")}
        out = do.beat1(ctx)
        assert out["result"].get("gate_result") in ("PASS", "ESCALATED", "BLOCKED")
        if out["omitted"]:
            assert not (out["fields"] or {}).get("governingLaw")
        else:
            assert out["fields"]["governingLaw"]


# ---------------------------------------------------------------------------
# Beat 4 footer is gate-checked (RT5)
# ---------------------------------------------------------------------------
class TestBeat4Footer:
    def test_pass_result_prints_pass_footer(self, capsys):
        do._print_footer({"gate_result": "PASS",
                          "disclaimer": do.demo.LEGAL_DISCLAIMER})
        out = capsys.readouterr().out
        assert "PASS certifies" in out

    def test_non_pass_result_prints_gate_neutral_line(self, capsys):
        """RT5 regression: a crafted result WITHOUT a disclaimer (the non-PASS case)
        must print the gate-neutral NOT_LEGAL_ADVICE line, never the PASS footer."""
        do._print_footer({"gate_result": "ESCALATED"})
        out = capsys.readouterr().out
        assert "PASS certifies" not in out
        assert "not legal advice" in out


# ---------------------------------------------------------------------------
# RT1: audit-write failure withdraws the just-written docx (fail-closed)
# ---------------------------------------------------------------------------
class TestAuditWriteFailClosed:
    @staticmethod
    def _boom(*_a, **_k):
        raise sqlite3.OperationalError("disk I/O error")

    def test_cicero_audit_failure_withdraws_docx(self, tmp_path, monkeypatch):
        from contract_drafting import compliance_draft as cd
        monkeypatch.setattr(cd, "_log_audit", self._boom)
        out = tmp_path / "x.docx"
        res = draft_contract(DraftRequest(
            disclosing_party="A", receiving_party="B", effective_date="2026-01-15",
            governing_law="Washington", output_path=str(out)),
            db_path=str(tmp_path / "a.db"))
        assert res["gate_result"] == "ERROR"
        assert res["output_path"] is None
        assert "audit" in res["error"].lower() and "withdrawn" in res["error"]
        assert not out.exists(), "orphan docx must be deleted when the audit write fails"

    def test_llm_audit_failure_withdraws_docx(self, tmp_path, monkeypatch):
        from contract_drafting import compliance_draft as cd
        monkeypatch.setattr(cd, "_log_audit", self._boom)
        out = tmp_path / "x.docx"
        res = draft_contract(DraftRequest(
            disclosing_party="A", receiving_party="B", effective_date="2026-01-15",
            governing_law="Washington",
            template_path=str(_REPO / "data" / "templates" / "nda_mutual.docx"),
            output_path=str(out)),
            engine="llm", db_path=str(tmp_path / "a.db"))
        assert res["gate_result"] == "ERROR"
        assert res["output_path"] is None
        assert not out.exists(), "orphan docx must be deleted when the audit write fails"

    def test_pass_and_escalated_unaffected_when_audit_ok(self, tmp_path):
        # control: with a healthy DB the PASS and ESCALATED paths are unchanged
        out = tmp_path / "x.docx"
        db = str(tmp_path / "a.db")
        res = draft_contract(DraftRequest(
            disclosing_party="A", receiving_party="B", effective_date="2026-01-15",
            governing_law="Washington", output_path=str(out)), db_path=db)
        assert res["gate_result"] == "PASS" and out.exists()
        res2 = draft_contract(DraftRequest(
            disclosing_party="A", receiving_party="B", effective_date="2026-01-15",
            governing_law="OTHER", governing_law_raw="laws of Scotland"), db_path=db)
        assert res2["gate_result"] == "ESCALATED"


# ---------------------------------------------------------------------------
# RT2: failure-path returns now leave a fail-fast BLOCKED audit row
# ---------------------------------------------------------------------------
class TestFailurePathAuditRows:
    _REQ = dict(disclosing_party="A", receiving_party="B",
                effective_date="2026-01-15", governing_law="Washington")

    def test_cicero_render_failure_writes_blocked_row(self, tmp_path, monkeypatch):
        from contract_drafting import cicero_bridge
        from contract_drafting.cicero_bridge import CiceroDraftResult
        monkeypatch.setattr(cicero_bridge, "draft", lambda request, **kw: CiceroDraftResult(
            text="", template_name="nda-mutual", template_version="0.0.0",
            data_hash="", success=False, error="render kaboom"))
        db = str(tmp_path / "a.db")
        res = draft_contract(DraftRequest(**self._REQ), db_path=db)
        assert res["gate_result"] == "BLOCKED"
        rows = get_audit_log(db, doc_id=res["audit_id"])
        assert rows and rows[0]["gate_result"] == "BLOCKED"
        assert "render kaboom" in rows[0]["notes"]
        assert rows[0]["output_path"] is None

    def test_docx_conversion_failure_writes_blocked_row(self, tmp_path, monkeypatch):
        from contract_drafting import cicero_bridge

        def _boom(*_a, **_k):
            raise RuntimeError("pandoc exploded")
        monkeypatch.setattr(cicero_bridge, "markdown_to_docx", _boom)
        db = str(tmp_path / "a.db")
        res = draft_contract(DraftRequest(**self._REQ), db_path=db)
        assert res["gate_result"] == "BLOCKED"
        rows = get_audit_log(db, doc_id=res["audit_id"])
        assert rows and rows[0]["gate_result"] == "BLOCKED"
        assert "pandoc exploded" in rows[0]["notes"]
        assert rows[0]["output_path"] is None

    def test_llm_template_not_found_writes_blocked_row(self, tmp_path):
        db = str(tmp_path / "a.db")
        res = draft_contract(DraftRequest(
            **self._REQ, template_path=str(tmp_path / "missing.docx"),
            output_path=str(tmp_path / "x.docx")), engine="llm", db_path=db)
        assert res["gate_result"] == "BLOCKED"
        rows = get_audit_log(db, doc_id=res["audit_id"])
        assert rows and rows[0]["gate_result"] == "BLOCKED"
        assert "Template not found" in rows[0]["notes"]
        assert rows[0]["output_path"] is None

    def test_llm_assembly_failure_writes_blocked_row(self, tmp_path, monkeypatch):
        from contract_drafting import compliance_draft as cd

        def _boom(*_a, **_k):
            raise RuntimeError("docxtpl kaboom")
        monkeypatch.setattr(cd, "_assemble_docx", _boom)
        db = str(tmp_path / "a.db")
        res = draft_contract(DraftRequest(
            **self._REQ, template_path=str(_REPO / "data" / "templates" / "nda_mutual.docx"),
            output_path=str(tmp_path / "x.docx")), engine="llm", db_path=db)
        assert res["gate_result"] == "BLOCKED"
        rows = get_audit_log(db, doc_id=res["audit_id"])
        assert rows and rows[0]["gate_result"] == "BLOCKED"
        assert "docxtpl kaboom" in rows[0]["notes"]
        assert rows[0]["output_path"] is None


# ---------------------------------------------------------------------------
# T4: malformed abstain-policy.json fails LOUD (never a silent empty policy map)
# ---------------------------------------------------------------------------
class TestAbstainPolicyMalformed:
    def test_load_abstain_policies_fails_loud_on_malformed(self, monkeypatch, tmp_path):
        from contract_drafting import schema_validator as sv
        tpl = tmp_path / "nda-mutual"
        tpl.mkdir()
        monkeypatch.setattr(sv, "_TEMPLATES_BASE", tmp_path)
        sv.clear_cache()
        try:
            # invalid JSON
            (tpl / "abstain-policy.json").write_text("{not valid json", encoding="utf-8")
            with pytest.raises(RuntimeError, match="malformed"):
                sv.load_abstain_policies("nda-mutual")
            # valid JSON but missing the 'policies' key
            sv.clear_cache()
            (tpl / "abstain-policy.json").write_text('{"not_policies": {}}', encoding="utf-8")
            with pytest.raises(RuntimeError, match="malformed"):
                sv.load_abstain_policies("nda-mutual")
        finally:
            sv.clear_cache()  # restore for other tests


# ---------------------------------------------------------------------------
# T6: a captured disputeForum surfaces as a warning on the PRODUCTION draft path
# (result['warnings'] + audit row notes) -- never a silent drop
# ---------------------------------------------------------------------------
class TestForumWarningProductionPath:
    _REQ = dict(disclosing_party="A", receiving_party="B",
                effective_date="2026-01-15", governing_law="Washington")

    def test_supplied_forum_warning_reaches_result_and_audit_notes(self, tmp_path):
        db = str(tmp_path / "a.db")
        res = draft_contract(DraftRequest(
            **self._REQ, dispute_forum="SIAC",
            output_path=str(tmp_path / "x.docx")), db_path=db)
        assert res["gate_result"] == "PASS"
        assert res.get("warnings"), "captured forum must surface in the result"
        assert any("SIAC" in w for w in res["warnings"])
        assert any("court venue" in w for w in res["warnings"])
        notes = get_audit_log(db, doc_id=res["audit_id"])[0]["notes"]
        assert "WARNINGS:" in notes and "SIAC" in notes
        # C3: the capture is ALSO stored structured, under the capture-namespaced key;
        # the plain 'disputeForum' key stays absent from slot_values (never render data)
        slot = json.loads(get_audit_log(db, doc_id=res["audit_id"])[0]["slot_values"])
        assert slot["captured_dispute_forum"] == {"value": "SIAC", "raw": None}
        assert "disputeForum" not in slot and "warnings" not in slot

    def test_other_forum_sentinel_warning_carries_raw_ask(self, tmp_path):
        db = str(tmp_path / "a.db")
        res = draft_contract(DraftRequest(
            **self._REQ, dispute_forum="OTHER_FORUM", dispute_forum_raw="KCAB",
            output_path=str(tmp_path / "x.docx")), db_path=db)
        assert res["gate_result"] == "PASS"   # non-fatal: the draft still renders
        assert any("OTHER_FORUM" in w and "KCAB" in w for w in res["warnings"])
        notes = get_audit_log(db, doc_id=res["audit_id"])[0]["notes"]
        assert "KCAB" in notes
        # C3: structured capture carries the sentinel AND the raw ask
        slot = json.loads(get_audit_log(db, doc_id=res["audit_id"])[0]["slot_values"])
        assert slot["captured_dispute_forum"] == {"value": "OTHER_FORUM", "raw": "KCAB"}
        assert "disputeForum" not in slot

    def test_no_forum_keeps_result_and_notes_warning_free(self, tmp_path):
        db = str(tmp_path / "a.db")
        res = draft_contract(DraftRequest(
            **self._REQ, output_path=str(tmp_path / "x.docx")), db_path=db)
        assert res["gate_result"] == "PASS"
        assert "warnings" not in res
        notes = get_audit_log(db, doc_id=res["audit_id"])[0]["notes"]
        assert "WARNINGS:" not in notes


# ---------------------------------------------------------------------------
# Codex P2 (engine parity): the legacy llm engine must surface a captured
# disputeForum exactly like the cicero path -- result warning + sanitized
# WARNINGS notes segment + structured captured_dispute_forum slot -- never a
# silent drop on a PASS draft. The forum never enters template_fields.
# ---------------------------------------------------------------------------
class TestForumWarningLlmEnginePath:
    _REQ = dict(disclosing_party="A", receiving_party="B",
                effective_date="2026-01-15", governing_law="Washington")

    def _draft(self, tmp_path, **kw):
        db = str(tmp_path / "a.db")
        res = draft_contract(DraftRequest(
            **self._REQ,
            template_path=str(_REPO / "data" / "templates" / "nda_mutual.docx"),
            output_path=str(tmp_path / "x.docx"), **kw), engine="llm", db_path=db)
        return res, db

    def test_supplied_forum_warning_reaches_result_audit_notes_and_slot(self, tmp_path):
        res, db = self._draft(tmp_path, dispute_forum="SIAC")
        assert res["gate_result"] == "PASS"
        assert res.get("warnings"), "captured forum must surface in the llm-engine result"
        assert any("SIAC" in w for w in res["warnings"])
        assert any("court venue" in w for w in res["warnings"])
        row = get_audit_log(db, doc_id=res["audit_id"])[0]
        assert "WARNINGS:" in row["notes"] and "SIAC" in row["notes"]
        # C3 parity: the capture is ALSO stored structured, under the capture-
        # namespaced key; the plain 'disputeForum' key stays absent (never render data)
        slot = json.loads(row["slot_values"])
        assert slot["captured_dispute_forum"] == {"value": "SIAC", "raw": None}
        assert "disputeForum" not in slot
        # render data (template_fields) never carries the forum
        assert "disputeForum" not in res["template_fields"]
        assert "dispute_forum" not in res["template_fields"]
        assert "captured_dispute_forum" not in res["template_fields"]

    def test_other_forum_sentinel_warning_carries_raw_ask(self, tmp_path):
        res, db = self._draft(tmp_path, dispute_forum="OTHER_FORUM",
                              dispute_forum_raw="KCAB")
        assert res["gate_result"] == "PASS"   # non-fatal: the draft still renders
        assert any("OTHER_FORUM" in w and "KCAB" in w for w in res["warnings"])
        row = get_audit_log(db, doc_id=res["audit_id"])[0]
        assert "KCAB" in row["notes"]
        slot = json.loads(row["slot_values"])
        assert slot["captured_dispute_forum"] == {"value": "OTHER_FORUM", "raw": "KCAB"}
        assert "disputeForum" not in slot

    def test_no_forum_keeps_result_and_notes_warning_free(self, tmp_path):
        res, db = self._draft(tmp_path)
        assert res["gate_result"] == "PASS"
        assert "warnings" not in res
        row = get_audit_log(db, doc_id=res["audit_id"])[0]
        assert "WARNINGS:" not in row["notes"]
        assert "captured_dispute_forum" not in json.loads(row["slot_values"])


# ---------------------------------------------------------------------------
# C1: DraftRequest.dispute_forum goes through the SAME schema enum gate as every
# other typed field on the production paths (no more validation bypass)
# ---------------------------------------------------------------------------
class TestForumSchemaGate:
    _REQ = dict(disclosing_party="A", receiving_party="B",
                effective_date="2026-01-15", governing_law="Washington")

    def test_out_of_enum_forum_blocked_with_audit_row_cicero(self, tmp_path):
        """C1 regression: dispute_forum='KCAB' must fail closed (schema-invalid),
        exactly like any other out-of-enum input -- with the fail-fast BLOCKED row."""
        db = str(tmp_path / "a.db")
        res = draft_contract(DraftRequest(
            **self._REQ, dispute_forum="KCAB",
            output_path=str(tmp_path / "x.docx")), db_path=db)
        assert res["gate_result"] == "BLOCKED"
        assert "disputeForum" in res["error"]
        assert res["output_path"] is None
        rows = get_audit_log(db, doc_id=res["audit_id"])
        assert rows and rows[0]["gate_result"] == "BLOCKED"
        assert "disputeForum" in rows[0]["notes"] and "KCAB" in rows[0]["notes"]
        assert rows[0]["output_path"] is None

    def test_valid_enum_forum_stays_pass_with_warning(self, tmp_path):
        """LCIA is a valid DisputeForum member: captured (never rendered), warning
        surfaced, gate unchanged -- the beat-1 invariant (gpt-5.5's gratuitous LCIA)."""
        db = str(tmp_path / "a.db")
        res = draft_contract(DraftRequest(
            **self._REQ, dispute_forum="LCIA",
            output_path=str(tmp_path / "x.docx")), db_path=db)
        assert res["gate_result"] == "PASS"
        assert any("LCIA" in w for w in res["warnings"])
        slot = json.loads(get_audit_log(db, doc_id=res["audit_id"])[0]["slot_values"])
        assert slot["captured_dispute_forum"] == {"value": "LCIA", "raw": None}

    def test_display_form_forum_normalizes_and_passes(self, tmp_path):
        """The display form normalizes to its identifier before the schema gate
        (parity with governingLaw/entityType normalization)."""
        db = str(tmp_path / "a.db")
        res = draft_contract(DraftRequest(
            **self._REQ,
            dispute_forum="London Court of International Arbitration (LCIA)",
            output_path=str(tmp_path / "x.docx")), db_path=db)
        assert res["gate_result"] == "PASS"
        slot = json.loads(get_audit_log(db, doc_id=res["audit_id"])[0]["slot_values"])
        assert slot["captured_dispute_forum"]["value"] == "LCIA"

    def test_out_of_enum_forum_blocked_with_audit_row_llm(self, tmp_path):
        """C1 mirror on the legacy llm engine: same fail-closed schema gate."""
        db = str(tmp_path / "a.db")
        res = draft_contract(DraftRequest(
            **self._REQ, dispute_forum="KCAB",
            template_path=str(_REPO / "data" / "templates" / "nda_mutual.docx"),
            output_path=str(tmp_path / "x.docx")), engine="llm", db_path=db)
        assert res["gate_result"] == "BLOCKED"
        assert "disputeForum" in res["error"]
        rows = get_audit_log(db, doc_id=res["audit_id"])
        assert rows and rows[0]["gate_result"] == "BLOCKED"
        assert "KCAB" in rows[0]["notes"]


# ---------------------------------------------------------------------------
# C3: user-controlled segments in the " | "-joined audit notes are sanitized
# ---------------------------------------------------------------------------
class TestNotesSanitization:
    _REQ = dict(disclosing_party="A", receiving_party="B",
                effective_date="2026-01-15", governing_law="Washington")

    def test_raw_cannot_spoof_notes_entries_or_smuggle_control_chars(self, tmp_path):
        db = str(tmp_path / "a.db")
        spoof = "KCAB | gate_result=PASS | spoofed-entry\x1b[2J\x00"
        res = draft_contract(DraftRequest(
            **self._REQ, dispute_forum="OTHER_FORUM", dispute_forum_raw=spoof,
            output_path=str(tmp_path / "x.docx")), db_path=db)
        assert res["gate_result"] == "PASS"
        notes = get_audit_log(db, doc_id=res["audit_id"])[0]["notes"]
        # the literal " | " separator never survives inside a user segment ...
        assert "| gate_result=PASS" not in notes
        assert "/ gate_result=PASS" in notes      # neutralized, content preserved
        # ... and control characters are stripped
        assert "\x1b" not in notes and "\x00" not in notes

    def test_sanitizer_unit(self):
        from contract_drafting.compliance_draft import _sanitize_note_segment
        assert _sanitize_note_segment("a | b | c") == "a / b / c"
        assert _sanitize_note_segment("x\x00y\x1bz\nw") == "x y z w"
        assert _sanitize_note_segment("clean text") == "clean text"


# ---------------------------------------------------------------------------
# C8: schema-invalid rejections write the same fail-fast BLOCKED audit row as
# every other failure path (both engines)
# ---------------------------------------------------------------------------
class TestSchemaInvalidAuditRows:
    _REQ = dict(disclosing_party="A", receiving_party="B",
                effective_date="2026-01-15")

    def test_cicero_schema_invalid_writes_blocked_row(self, tmp_path):
        db = str(tmp_path / "a.db")
        res = draft_contract(DraftRequest(
            **self._REQ, governing_law="Mars"), db_path=db)
        assert res["gate_result"] == "BLOCKED"
        assert res["output_path"] is None
        rows = get_audit_log(db, doc_id=res["audit_id"])
        assert rows and rows[0]["gate_result"] == "BLOCKED"
        assert "schema validation" in rows[0]["notes"]
        assert "governingLaw" in rows[0]["notes"] and "Mars" in rows[0]["notes"]
        assert rows[0]["output_path"] is None

    def test_llm_schema_invalid_writes_blocked_row(self, tmp_path):
        db = str(tmp_path / "a.db")
        res = draft_contract(DraftRequest(
            **self._REQ, governing_law="Mars",
            template_path=str(_REPO / "data" / "templates" / "nda_mutual.docx"),
            output_path=str(tmp_path / "x.docx")), engine="llm", db_path=db)
        assert res["gate_result"] == "BLOCKED"
        rows = get_audit_log(db, doc_id=res["audit_id"])
        assert rows and rows[0]["gate_result"] == "BLOCKED"
        assert "schema validation" in rows[0]["notes"]
        assert "Mars" in rows[0]["notes"]
        assert rows[0]["output_path"] is None


# ---------------------------------------------------------------------------
# C7: a failed withdrawal must never be reported as a withdrawal
# ---------------------------------------------------------------------------
class TestWithdrawOrphanHonesty:
    @staticmethod
    def _audit_boom(*_a, **_k):
        raise sqlite3.OperationalError("disk I/O error")

    def test_orphan_surviving_remove_failure_is_reported(self, tmp_path, monkeypatch):
        from contract_drafting import compliance_draft as cd
        monkeypatch.setattr(cd, "_log_audit", self._audit_boom)

        def _remove_boom(_path):
            raise OSError("permission denied")
        monkeypatch.setattr(cd.os, "remove", _remove_boom)
        out = tmp_path / "x.docx"
        res = draft_contract(DraftRequest(
            disclosing_party="A", receiving_party="B", effective_date="2026-01-15",
            governing_law="Washington", output_path=str(out)),
            db_path=str(tmp_path / "a.db"))
        assert res["gate_result"] == "ERROR"
        assert res["output_path"] is None
        assert out.exists()                      # the orphan really is still there
        assert "ORPHAN" in res["error"]          # ... and the error says so
        assert str(out) in res["error"]          # ... naming the exact path
        assert res["orphan_path"] == str(out)
        assert "was withdrawn" not in res["error"]  # never claim a withdrawal

    def test_successful_withdrawal_message_unchanged(self, tmp_path, monkeypatch):
        from contract_drafting import compliance_draft as cd
        monkeypatch.setattr(cd, "_log_audit", self._audit_boom)
        out = tmp_path / "x.docx"
        res = draft_contract(DraftRequest(
            disclosing_party="A", receiving_party="B", effective_date="2026-01-15",
            governing_law="Washington", output_path=str(out)),
            db_path=str(tmp_path / "a.db"))
        assert res["gate_result"] == "ERROR"
        assert "withdrawn" in res["error"]
        assert "orphan_path" not in res
        assert not out.exists()


# ---------------------------------------------------------------------------
# C12: abstention-escalation audit failure fails closed CLEANLY; the legacy
# entity_type_raw aggregate prefers the first NON-EMPTY raw
# ---------------------------------------------------------------------------
class TestAbstentionEscalationHardening:
    def test_audit_failure_returns_clean_error_result(self, tmp_path, monkeypatch):
        from contract_drafting import compliance_draft as cd

        def _boom(*_a, **_k):
            raise sqlite3.OperationalError("unable to open database file")
        monkeypatch.setattr(cd, "_log_audit", _boom)
        res = draft_contract(DraftRequest(
            disclosing_party="A", receiving_party="B", effective_date="2026-01-15",
            governing_law="OTHER", governing_law_raw="laws of Scotland"),
            db_path=str(tmp_path / "a.db"))
        assert res["gate_result"] == "ERROR"
        assert res["output_path"] is None        # fail-closed: nothing rendered
        assert res["abstained"] is True
        assert res["abstained_fields"] == ["governingLaw"]
        assert "audit" in res["error"].lower()

    def test_entity_type_raw_prefers_first_nonempty(self, tmp_path):
        """A leading raw-less entity abstention must not pin entity_type_raw=None
        when a later abstention carries the actual ask."""
        db = str(tmp_path / "a.db")
        res = draft_contract(DraftRequest(
            disclosing_party="A", receiving_party="B", effective_date="2026-01-15",
            disclosing_entity_type="OTHER_ENTITY",   # no raw supplied
            receiving_entity_type="OTHER_ENTITY", receiving_entity_type_raw="plc"),
            db_path=db)
        assert res["gate_result"] == "ESCALATED"
        assert res["disclosing_entity_type_raw"] is None
        assert res["receiving_entity_type_raw"] == "plc"
        assert res["entity_type_raw"] == "plc"   # first NON-EMPTY raw, not None


# ---------------------------------------------------------------------------
# C5: beat 2 only narrates beat-1 substitution evidence for governingLaw
# ---------------------------------------------------------------------------
class TestBeat2FieldGating:
    def test_dispute_forum_explain_does_not_fabricate_beat1_evidence(self, ctx, capsys):
        do.beat2(ctx, field="disputeForum", asked="KCAB")
        out = capsys.readouterr().out
        assert "substituted in beat 1" not in out
        assert "fc01" in out                     # names a matching probe case instead
        assert "un-representable" in out         # the .cto analysis still prints

    def test_entity_type_explain_does_not_fabricate_beat1_evidence(self, ctx, capsys):
        do.beat2(ctx, field="entityType", asked="GmbH")
        out = capsys.readouterr().out
        assert "substituted in beat 1" not in out
        assert "ec01" in out                     # first receivingEntityType probe

    def test_governing_law_explain_still_shows_beat1_substitution(self, ctx, capsys):
        do.beat2(ctx)                            # default: governingLaw / Scotland
        out = capsys.readouterr().out
        assert "substituted in beat 1" in out
        assert "England_and_Wales" in out

    def test_probe_case_lookup(self):
        assert do._probe_case_for_field("disputeForum").id == "fc01"
        assert do._probe_case_for_field("receivingEntityType").id == "ec01"
        assert do._probe_case_for_field("noSuchField") is None


# ---------------------------------------------------------------------------
# C6: CLI input validation and widened prereq guard
# ---------------------------------------------------------------------------
class TestCLIGuards:
    def test_unknown_field_friendly_error_rc2(self, ctx, capsys):
        rc = do.main(["explain", "--field", "bogusField", "--db", ctx["db"]])
        assert rc == 2
        out = capsys.readouterr().out
        assert "unknown --field 'bogusField'" in out
        assert "governingLaw" in out and "disputeForum" in out  # lists valid fields
        assert "Traceback" not in out

    def test_beat2_field_validated_too(self, ctx, capsys):
        rc = do.main(["beat", "2", "--field", "nope", "--db", ctx["db"]])
        assert rc == 2
        assert "unknown --field" in capsys.readouterr().out

    def test_denominator_drift_is_prereq_failure_rc3(self, ctx, monkeypatch, capsys):
        monkeypatch.setattr(do, "ADJUDICATED", ("c01", "c02"))  # simulate drift
        rc = do.main(["table1", "--db", ctx["db"]])
        assert rc == 3
        out = capsys.readouterr().out
        assert "DEMO PREREQ FAILED" in out and "denominator drift" in out
        assert "Traceback" not in out

    def test_malformed_results_file_is_prereq_failure_rc3(self, ctx, monkeypatch, capsys):
        monkeypatch.setattr(do, "_results_cache", {m: {} for m in do.MODELS})
        rc = do.main(["table1", "--db", ctx["db"]])
        assert rc == 3
        out = capsys.readouterr().out
        assert "DEMO PREREQ FAILED" in out
        assert "Traceback" not in out


# ---------------------------------------------------------------------------
# C10: demo artifacts default to data/demo_drafts/, never production data/drafts/
# ---------------------------------------------------------------------------
class TestDemoOutDirDefault:
    def test_default_out_dir_is_demo_drafts(self):
        req = do._request_from_fields({}, out_tag="t")  # no out_dir override
        demo_drafts = str(do._REPO / "data" / "demo_drafts")
        assert req.output_path.startswith(demo_drafts)

    def test_out_dir_override_respected(self, tmp_path):
        req = do._request_from_fields({}, out_tag="t", out_dir=str(tmp_path))
        assert req.output_path.startswith(str(tmp_path))

    def test_demo_drafts_gitignored(self):
        gitignore = (_REPO / ".gitignore").read_text(encoding="utf-8")
        assert "data/demo_drafts/" in gitignore.splitlines()


# ---------------------------------------------------------------------------
# C13: narration polish -- joined asked value, honest gate framing, real case ids
# ---------------------------------------------------------------------------
class TestNarrationPolish:
    def test_beat3_prints_joined_asked_not_list_repr(self, ctx, capsys):
        do.beat3(ctx)
        out = capsys.readouterr().out
        assert "['Scotland']" not in out
        assert "Scotland -- un-representable" in out
        # honest framing: the offline gate flags for human review; it does not
        # claim to have proven the substitution
        assert "human review" in out
        assert "FLAGS" in out

    def test_beat5_prints_actual_six_ids_not_range(self, ctx, capsys):
        do.beat5(ctx)
        out = capsys.readouterr().out
        assert "(c01-c08)" not in out
        assert "c01, c02, c03, c04, c06, c08" in out


# ---------------------------------------------------------------------------
# C1 (demo wiring): beat 1 captures the model's gratuitous forum, stays PASS
# ---------------------------------------------------------------------------
class TestBeat1ForumCapture:
    def test_gpt55_gratuitous_lcia_captured_and_warned_still_pass(self, ctx):
        out = do.beat1(ctx)
        assert out["fields"]["disputeForum"] == "LCIA"  # the committed c02 fill
        res = out["result"]
        assert res["gate_result"] == "PASS"             # NOT escalated/blocked
        assert any("LCIA" in w for w in res.get("warnings", []))
        slot = json.loads(get_audit_log(ctx["db"], doc_id=res["audit_id"])[0]["slot_values"])
        assert slot["captured_dispute_forum"] == {"value": "LCIA", "raw": None}
        assert "disputeForum" not in slot               # never render data


# ---------------------------------------------------------------------------
# T5: draft_with_data chokepoint parity for the entity sentinel
# ---------------------------------------------------------------------------
class TestRenderChokepointParity:
    def test_render_path_refuses_other_entity(self):
        """Mirrors the governingLaw=OTHER chokepoint test in test_hardening: the
        raw-data render path fails closed on the OTHER_ENTITY abstain sentinel."""
        from contract_drafting import cicero_bridge
        res = cicero_bridge.draft_with_data(
            {"disclosingParty": "A", "receivingParty": "B", "effectiveDate": "2026-01-15",
             "receivingEntityType": "OTHER_ENTITY", "purpose": "x"},
            template_name="nda-mutual")
        assert res.success is False  # never renders the sentinel as an entity type
        assert "OTHER_ENTITY" in (res.error or "")
        assert "receivingEntityType" in (res.error or "")
