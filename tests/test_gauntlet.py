"""
Tests for the Gauntlet eval harness (contract_drafting/gauntlet.py).

Hermetic: a FakeCaller scripts deterministic "model" responses per case, so the
whole pipeline (suite -> 3 arms -> oracle -> record/replay -> table) runs with no
network and no API keys. Record-then-replay determinism and fail-closed cache
misses are both covered.
"""
from __future__ import annotations

import json

import pytest

from contract_drafting import demo_mars_beat as demo
from contract_drafting import gauntlet as g


# A schema-valid + playbook-PASS baseline the fake "model" starts from.
_BASE = {
    "disclosingParty": "TestCo", "receivingParty": "AcmeCorp",
    "effectiveDate": "2026-01-15", "governingLaw": "Washington",
    "disclosingEntityType": "corporation", "receivingEntityType": "corporation",
    "disputeForum": "SIAC",
    "purpose": "exploring a potential deal", "termMonths": 24,
    "noticeDays": 30, "survivalYears": 3, "mutual": True,
    "hasNonCompete": False, "hasNonSolicitation": False, "hasResidualsClause": False,
}


class FakeCaller(demo.LLMCaller):
    """Deterministic stand-in for a real model, scripted from the suite.

    - free/raw text: emits the case's violation on attempt 1; self-corrects on a
      retry (detected by the verify-reject feedback in the prompt).
    - structured: always schema-valid (the constraint prevents schema violations);
      for the prohibited-clause case it returns a schema-valid-but-policy-violating
      dict (hasNonCompete=True), modeling that the type constraint does NOT cover
      playbook policy.
    """

    def __init__(self, suite):
        self.suite = suite

    def _case(self, question):
        for c in self.suite:
            if c.instruction in question:
                return c
        raise AssertionError(f"no suite case matches question: {question[:90]!r}")

    def _free_fields(self, case, retry):
        if case.defect_class == "valid-control":
            return dict(_BASE)
        if case.defect_class == "prohibited-clause":
            return {**_BASE, "hasNonCompete": True}  # schema-valid, playbook-flagged
        if retry:
            return dict(_BASE)  # corrected after the validator fed errors back
        return {**_BASE, case.field: case.violating_value}  # the schema violation

    def text(self, question, context, *, provider, model, system_prompt):
        case = self._case(question)
        retry = "previous answer was rejected" in question
        return json.dumps(self._free_fields(case, retry))

    def structured(self, question, context, json_schema, *, provider, model, system_prompt):
        case = self._case(question)
        if case.defect_class == "prohibited-clause":
            return {**_BASE, "hasNonCompete": True}  # constraint is schema-only; policy slips through
        return dict(_BASE)  # schema-valid by construction


# ---------------------------------------------------------------------------
# Suite generation (D4)
# ---------------------------------------------------------------------------
class TestSuite:
    def test_covers_defect_classes(self):
        suite = g.build_suite("nda-mutual")
        classes = {c.defect_class for c in suite}
        for expected in ("out-of-enum", "malformed-pattern", "wrong-type",
                         "missing-required", "valid-control", "prohibited-clause"):
            assert expected in classes, f"missing defect class: {expected}"

    def test_enum_and_pattern_fields_targeted(self):
        suite = g.build_suite("nda-mutual")
        by_class = {c.defect_class: c for c in suite}
        # governingLaw is among the out-of-enum probes (the model now has several enum
        # fields: governingLaw, disclosing/receivingEntityType, disputeForum).
        out_of_enum_fields = {c.field for c in suite if c.defect_class == "out-of-enum"}
        assert "governingLaw" in out_of_enum_fields
        assert by_class["malformed-pattern"].field == "effectiveDate"
        assert by_class["wrong-type"].field in {"termMonths", "noticeDays", "survivalYears",
                                                "nonCompeteMonths", "nonSolicitationMonths"}

    def test_enum_display_values_normalized_before_validation(self):
        """Codex P2: a model that names a representable value in DISPLAY form (not the
        underscore identifier) is scored as a fill, not a schema-invalid leak -- for
        entityType + disputeForum, parity with governingLaw. _validate_arm normalizes."""
        import contract_drafting.demo_mars_beat as demo
        base = {
            "disclosingParty": "A", "receivingParty": "B", "effectiveDate": "2026-01-15",
            "purpose": "x", "termMonths": 24, "noticeDays": 30, "survivalYears": 3,
            "governingLaw": "New York",  # display form
            "disclosingEntityType": "limited liability company",  # display form
            "receivingEntityType": "corporation",
            "disputeForum": "Singapore International Arbitration Centre (SIAC)",  # display form
            "mutual": True, "hasNonCompete": False, "hasNonSolicitation": False,
            "hasResidualsClause": False,
        }
        # All display forms map to representable identifiers -> schema-valid (no leak).
        assert demo._validate_arm(base, with_abstain=True) == []


# ---------------------------------------------------------------------------
# Arms + headline result (D3) via record then replay (D1)
# ---------------------------------------------------------------------------
class TestArmsAndReplay:
    def _record(self, tmp_path):
        suite = g.build_suite("nda-mutual")
        caller = g.RecordReplayCaller(FakeCaller(suite), tmp_path / "cassette.json", mode="record")
        results = g.run_gauntlet(caller)
        return results, caller

    def test_constrained_zero_schema_invalid_by_construction(self, tmp_path):
        results, _ = self._record(tmp_path)
        s = g.summarize(results)
        assert s["constrained"]["schema_invalid"] == 0  # the headline

    def test_raw_ships_schema_invalid(self, tmp_path):
        results, _ = self._record(tmp_path)
        s = g.summarize(results)
        assert s["raw"]["schema_invalid_rate"] > 0.5  # adversarial cases escape the no-gate arm

    def test_verify_reject_recovers_with_visible_compute(self, tmp_path):
        results, _ = self._record(tmp_path)
        s = g.summarize(results)
        # verify-reject reaches schema validity on the schema-defect cases ...
        assert s["verify_reject"]["schema_invalid"] == 0
        # ... but at a measured compute cost (more than one attempt on average).
        assert s["verify_reject"]["mean_attempts"] > 1.0
        assert s["verify_reject"]["mean_est_tokens"] > s["raw"]["mean_est_tokens"]

    def test_playbook_policy_not_covered_by_type_constraint(self, tmp_path):
        """The honest nuance: a constrained draft is schema-valid yet can still be
        playbook-flagged (the non-compete case). 0%-by-construction is SCHEMA only."""
        results, _ = self._record(tmp_path)
        prohibited = [r for r in results if r.defect_class == "prohibited-clause"]
        assert prohibited and all(r.schema_valid for r in prohibited)        # schema-valid
        assert all(r.gate != "PASS" for r in prohibited)                     # policy still flags it
        assert g.summarize(results)["constrained"]["playbook_flagged"] >= 1

    def test_record_then_replay_is_deterministic(self, tmp_path):
        results1, _ = self._record(tmp_path)
        # Replay with NO inner caller: must reproduce identical results from disk.
        replay = g.RecordReplayCaller(None, tmp_path / "cassette.json", mode="replay")
        results2 = g.run_gauntlet(replay)
        assert g.summarize(results1) == g.summarize(results2)

    def test_replay_cache_miss_fails_closed(self, tmp_path):
        """Replay against an empty cassette must RAISE, never silently call live."""
        empty = g.RecordReplayCaller(None, tmp_path / "absent.json", mode="replay")
        with pytest.raises(g.GauntletCacheMiss):
            g.run_gauntlet(empty)


# ---------------------------------------------------------------------------
# Table render
# ---------------------------------------------------------------------------
class TestTable:
    def test_render_has_all_arms_and_headline(self, tmp_path):
        suite = g.build_suite("nda-mutual")
        caller = g.RecordReplayCaller(FakeCaller(suite), tmp_path / "c.json", mode="record")
        results = g.run_gauntlet(caller)
        # Guard that run_gauntlet actually PRODUCES every arm, incl. constrained_hatch -- asserting
        # only the rendered label would pass vacuously since render_table iterates _ARMS regardless
        # of whether results exist (audit finding). Check the results AND the rendered labels.
        assert {r.arm for r in results} == set(g._ARMS)
        out = g.render_table(results)
        assert ("A raw" in out and "B verify-reject" in out and "C constrained" in out
                and "D constrained+hatch" in out)
        assert "silent-wrong" in out and "SILENT-WRONG" in out


# ---------------------------------------------------------------------------
# Regression: the fill_slots DI refactor must not break the demo
# ---------------------------------------------------------------------------
class TestFillSlotsDIRegression:
    def test_default_caller_is_live(self, monkeypatch):
        # With no caller passed, fill_slots must use the live path (module-level
        # call_llm), which the existing demo tests monkeypatch.
        captured = {}
        monkeypatch.setattr(demo, "call_llm", lambda *a, **k: (captured.setdefault("hit", True), json.dumps(_BASE))[1])
        out = demo.fill_slots("draft an NDA", constrained=False)
        assert captured.get("hit") and out["governingLaw"] == "Washington"

    def test_injected_caller_overrides_live(self, monkeypatch):
        monkeypatch.setattr(demo, "call_llm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("live called")))
        suite = g.build_suite("nda-mutual")
        out = demo.fill_slots(suite[0].instruction, constrained=False, caller=FakeCaller(suite))
        assert isinstance(out, dict) and "governingLaw" in out


# ---------------------------------------------------------------------------
# Hard mode (red-teamed suite) + silent-substitution metric
# ---------------------------------------------------------------------------
def _invalid_for(field):
    if field == "governingLaw":
        return "Province of Ontario"           # real jurisdiction, not in enum
    if field in ("disclosingEntityType", "receivingEntityType"):
        return "GmbH"                            # foreign legal form, not in EntityType enum
    if field == "disputeForum":
        return "ad-hoc UNCITRAL"                 # not a listed arbitral institution
    if field == "effectiveDate":
        return "June 4 2026"                     # fails the YYYY-MM-DD pattern
    if field in ("termMonths", "noticeDays", "survivalYears"):
        return 2.5                                # float, fails type:integer
    return ""                                    # empty string fails minLength


def _policy_trigger(field):
    return {
        "hasNonCompete": {"hasNonCompete": True},
        "hasResidualsClause": {"hasResidualsClause": True},
        "mutual": {"mutual": False},
        "termMonths": {"termMonths": 120},
    }.get(field, {"hasNonCompete": True})


class HardFakeCaller(demo.LLMCaller):
    """Scripts the three failure modes the hard suite is designed to expose:
    raw LEAKS an invalid value; constrained is schema-valid (so a substitution-
    flagged case counts as a silent substitution); policy cases are schema-valid
    but trip the gate."""

    def __init__(self, suite):
        self.suite = suite

    def _case(self, question):
        for c in self.suite:
            if c.instruction in question:
                return c
        raise AssertionError(f"no hard case matches: {question[:80]!r}")

    def text(self, question, context, *, provider, model, system_prompt):
        c = self._case(question)
        if "previous answer was rejected" not in question and c.expect_raw_schema_invalid:
            return json.dumps({**_BASE, c.field: _invalid_for(c.field)})  # leak
        if c.expect_policy_flag:
            return json.dumps({**_BASE, **_policy_trigger(c.field)})       # valid but policy-flagged
        return json.dumps(dict(_BASE))                                     # corrected / clean

    def structured(self, question, context, json_schema, *, provider, model, system_prompt):
        c = self._case(question)
        if c.expect_policy_flag:
            return {**_BASE, **_policy_trigger(c.field)}   # schema-valid, policy-flagged
        return dict(_BASE)                                  # schema-valid (substitution if asked-unrepresentable)


class TestHardSuite:
    def test_loads_with_expectations(self):
        suite = g.build_suite("nda-mutual", mode="hard")
        assert len(suite) >= 18
        assert all(c.id and c.field and c.instruction for c in suite)
        # at least some cases carry each expectation flag
        assert any(c.expect_constrained_substitution for c in suite)
        assert any(c.expect_policy_flag for c in suite)
        assert {c.attack_vector for c in suite} != {""}  # vectors are populated

    def test_outcome_4way_classification(self):
        mk = lambda sv, chosen, exp, es=True: g.ArmResult(
            "constrained", "x", "governingLaw", "v", sv, "PASS", [], 1, 0,
            expect_substitution=es, chosen_value=chosen, expected_correct=exp, sentinel="OTHER")
        assert mk(False, "Ontario", "NONE").outcome == "leak"        # schema-invalid -> caught
        assert mk(True, None, "NONE").outcome == "omit"              # left blank
        assert mk(True, "Delaware", "NONE").outcome == "wrong_sub"   # un-representable -> any value wrong
        assert mk(True, "New_York", "New_York").outcome == "correct"
        assert mk(True, "New York", "New_York").outcome == "correct"  # display form normalizes
        assert mk(True, "Delaware", "New_York").outcome == "wrong_sub"  # valid but not what was asked
        assert mk(True, "OTHER", "NONE").outcome == "abstained"      # PR2 hatch: honest decline, NOT silent-wrong
        assert mk(True, "x", None, es=False).outcome == ""           # non-adjudicated case
        # back-compat coarse flag still works
        assert mk(True, "Delaware", "NONE").substituted is True

    def test_summarize_4way(self):
        R = lambda sv, chosen, exp: g.ArmResult("constrained", "x", "governingLaw", "v", sv, "PASS", [], 1, 0,
                                                expect_substitution=True, chosen_value=chosen,
                                                expected_correct=exp, sentinel="OTHER")
        results = [
            R(False, "Ontario", "NONE"),       # leak
            R(True, None, "NONE"),             # omit
            R(True, "Delaware", "NONE"),       # wrong_sub (un-representable)
            R(True, "New_York", "New_York"),   # correct
            R(True, "Delaware", "New_York"),   # wrong_sub (mismatch)
            R(True, "OTHER", "NONE"),          # abstained (PR2 hatch) -- NOT silent-wrong
        ]
        s = g.summarize(results)["constrained"]
        assert s["subst_flagged"] == 6
        assert s["outcomes"] == {"leak": 1, "omit": 1, "correct": 1, "wrong_sub": 2,
                                 "abstained": 1, "over_abstain": 0}
        assert s["wrong_sub"] == 2 and s["wrong_sub_rate"] == round(2 / 6, 3)
        # silent-wrong folds omit (silently defaults to a wrong value) in with wrong_sub;
        # abstained is honest (governingLaw=OTHER) and excluded from silent-wrong.
        assert s["silent_wrong"] == 3 and s["silent_wrong_rate"] == round(3 / 6, 3)
        assert s["abstained"] == 1 and s["abstained_rate"] == round(1 / 6, 3)

    def test_governinglawraw_side_channel_cannot_game_metric(self):
        """A baseline arm that emits the hatch side-channel field governingLawRaw alongside a
        SUBSTITUTED governingLaw is still counted wrong_sub. The 4-way outcome keys on the
        governingLaw field, not governingLawRaw, so a stray side-channel note can never deflate
        silent-wrong or be mis-credited as an abstention. (This is why the no-hatch baseline
        does not need to schema-reject governingLawRaw -- it cannot game the metric.)"""
        r = g.ArmResult("constrained", "c01", "governingLaw", "out-of-enum", True, "PASS", [], 1, 0,
                        fields={"governingLaw": "Delaware", "governingLawRaw": "Ontario, Canada"},
                        expect_substitution=True, chosen_value="Delaware", expected_correct="NONE")
        assert r.outcome == "wrong_sub"   # the real law parked in governingLawRaw does NOT save it
        s = g.summarize([r])["constrained"]
        assert s["silent_wrong"] == 1 and s["abstained"] == 0

    def test_hatch_over_abstention_on_supported_law_is_counted(self):
        """A hatch arm that returns OTHER on a SUPPORTED-law control (c27-29, representable)
        is over_abstain, scored over the SEPARATE /3 control denominator -- never the /9
        silent-wrong denominator. A correctly-filled control counts in the /3 denominator
        as 'correct', not in silent-wrong."""
        over = g.ArmResult("constrained_hatch", "c27", "governingLaw", "supported-law-control", True, "PASS", [], 1, 0,
                           fields={"governingLaw": "OTHER"}, expect_substitution=False,
                           chosen_value="OTHER", expected_correct="New_York", sentinel="OTHER")
        ok = g.ArmResult("constrained_hatch", "c28", "governingLaw", "supported-law-control", True, "PASS", [], 1, 0,
                         fields={"governingLaw": "Republic_of_Singapore"}, expect_substitution=False,
                         chosen_value="Republic_of_Singapore", expected_correct="Republic_of_Singapore", sentinel="OTHER")
        assert over.outcome == "over_abstain" and ok.outcome == "correct"
        s = g.summarize([over, ok])["constrained_hatch"]
        assert s["over_abstain"] == 1 and s["over_abstain_n"] == 2   # /3-style control denominator
        assert s["subst_flagged"] == 0 and s["silent_wrong"] == 0    # controls EXCLUDED from /9
        assert s["control_correct"] == 1

    def test_supported_controls_excluded_from_silent_wrong(self):
        """A correctly-filled supported-law control must NOT dilute the silent-wrong /9: it lands
        only in the control denominator, never in subst_flagged."""
        probe = g.ArmResult("constrained", "c01", "governingLaw", "out-of-enum", True, "PASS", [], 1, 0,
                            fields={"governingLaw": "Delaware"}, expect_substitution=True,
                            chosen_value="Delaware", expected_correct="NONE")
        ctrl = g.ArmResult("constrained", "c27", "governingLaw", "supported-law-control", True, "PASS", [], 1, 0,
                           fields={"governingLaw": "New_York"}, expect_substitution=False,
                           chosen_value="New_York", expected_correct="New_York")
        s = g.summarize([probe, ctrl])["constrained"]
        assert s["subst_flagged"] == 1 and s["silent_wrong"] == 1   # only the probe
        assert s["over_abstain_n"] == 1 and s["over_abstain"] == 0  # the control, filled correctly

    def test_offfield_over_abstain_counted_on_adjudicated_rows(self):
        """An ADJUDICATED row whose own field is filled CORRECTLY (e.g. a supported-law control
        with governingLaw=New_York -> outcome 'correct') but that ALSO emits a gratuitous off-field
        sentinel (disputeForum=OTHER_FORUM, a field the case never asked about) must still be
        tallied in over_abstain_offfield. The off-field sentinel is excluded from the row's own
        over_abstain (governingLaw filled fine), so the per-field control rate stays clean while the
        out-of-band cost is no longer under-reported (Codex P2: previously only non-adjudicated rows
        were scanned, so a correctly-filled control's stray OTHER_FORUM vanished from the tally)."""
        ctrl = g.ArmResult("constrained_hatch", "c27", "governingLaw", "supported-law-control", True, "PASS", [], 1, 0,
                           fields={"governingLaw": "New_York", "disputeForum": "OTHER_FORUM"},
                           expect_substitution=False, chosen_value="New_York",
                           expected_correct="New_York", sentinel="OTHER")
        assert ctrl.outcome == "correct"          # its OWN field is right -> not over_abstain on-field
        assert ctrl.offfield_over_abstain == 1     # but the stray OTHER_FORUM is off-field
        s = g.summarize([ctrl])["constrained_hatch"]
        assert s["over_abstain"] == 0 and s["over_abstain_n"] == 1   # control denominator stays clean
        assert s["over_abstain_offfield"] == 1                        # out-of-band cost IS counted
        assert s["control_correct"] == 1 and s["silent_wrong"] == 0

    def test_offfield_over_abstain_excludes_own_field(self):
        """The off-field tally must NOT count a sentinel in the row's OWN field-under-test (that is
        the on-field abstention, already scored by `outcome`). An entityType probe that abstains on
        its own field has offfield_over_abstain == 0."""
        r = g.ArmResult("constrained_hatch", "ec01", "disclosingEntityType", "out-of-enum", True, "PASS", [], 1, 0,
                        fields={"disclosingEntityType": "OTHER_ENTITY"}, expect_substitution=False,
                        chosen_value="OTHER_ENTITY", expected_correct=None, sentinel="OTHER_ENTITY")
        assert r.outcome == "abstained"            # on-field honest abstention
        assert r.offfield_over_abstain == 0         # NOT double-counted as off-field

    def test_schema_mode_out_of_enum_other_is_abstained(self):
        """A schema-mode out-of-enum governingLaw probe (e.g. 'Atlantis') answered with OTHER is
        an HONEST abstention, NOT an over-abstention. Schema cases don't set expect_substitution,
        so un-representability is keyed off defect_class='out-of-enum' (Codex r6)."""
        r = g.ArmResult("constrained_hatch", "c06", "governingLaw", "out-of-enum", True, "PASS", [], 1, 0,
                        fields={"governingLaw": "OTHER"}, expect_substitution=False,
                        chosen_value="OTHER", expected_correct=None, sentinel="OTHER")
        assert r.outcome == "abstained"
        s = g.summarize([r])["constrained_hatch"]
        assert s["abstained"] == 1 and s["over_abstain"] == 0

    def test_end_to_end_hard_mode(self, tmp_path):
        suite = g.build_suite("nda-mutual", mode="hard")
        caller = g.RecordReplayCaller(HardFakeCaller(suite), tmp_path / "hard.json", mode="record")
        results = g.run_gauntlet(caller, mode="hard")
        s = g.summarize(results)
        flagged = sum(1 for c in suite if c.expect_constrained_substitution)
        # constrained is schema-valid on every flagged case but never matches the adjudicated
        # correct value here (the fake returns a fixed baseline) -> all wrong_sub
        assert s["constrained"]["subst_flagged"] == flagged
        assert s["constrained"]["wrong_sub"] == flagged
        assert s["constrained"]["outcomes"]["correct"] == 0
        # raw LEAKS invalid on the raw-invalid cases (a caught failure, not a silent substitution)
        assert s["raw"]["outcomes"]["leak"] >= 1 and s["raw"]["schema_invalid"] >= 1
        # policy is independent of schema validity
        assert s["constrained"]["playbook_flagged"] >= 1
        # the table renders the silent-wrong column + the 4-way breakdown
        out = g.render_table(results)
        assert "silent-wrong" in out and "WRONG=" in out

    def test_schema_mode_out_of_enum_adjudicated_symmetrically(self, tmp_path):
        # The schema-mode suite has exactly ONE adjudicated case: the out-of-enum governingLaw
        # probe (c06, 'Atlantis' -- un-representable). It is adjudicated for EVERY arm, not just
        # the hatch's OTHER, so denominators stay symmetric across arms (Codex r7). With the
        # FakeCaller: raw emits the violating value -> schema-invalid leak; the other arms emit a
        # valid law -> wrong_sub (un-representable ask -> any concrete value is wrong).
        suite = g.build_suite("nda-mutual", mode="schema")
        rec = g.RecordReplayCaller(FakeCaller(suite), tmp_path / "sm.json", mode="record")
        s = g.summarize(g.run_gauntlet(rec, mode="schema"))
        for arm in g._ARMS:
            assert s[arm]["subst_flagged"] == 1, arm  # symmetric: every arm adjudicates c06
        assert s["raw"]["outcomes"]["leak"] == 1
        assert s["constrained"]["outcomes"]["wrong_sub"] == 1


class TestHardeningFixes:
    """Regression tests for the adversarial-review P1s on the eval harness."""

    def test_unknown_provider_fails_closed(self):
        from contract_drafting import eval_providers as ep
        with pytest.raises(ValueError):
            ep.make_record_caller("deepseek-typo")  # must NOT fall through to the OpenAI path

    def test_oracle_playbook_failure_surfaces_error_not_pass(self, monkeypatch):
        from contract_drafting import compliance_playbook

        def boom(cls, path=None):
            raise RuntimeError("playbook unavailable")

        monkeypatch.setattr(compliance_playbook.Playbook, "load", classmethod(boom))
        o = g.oracle({"disclosingParty": "A", "receivingParty": "B",
                      "effectiveDate": "2026-01-01", "governingLaw": "Washington"})
        assert o["gate"] == "ERROR" and o["playbook_pass"] is False  # fail closed, not silent PASS

    def test_run_mars_beat_reraises_cache_miss(self):
        class Boom(demo.LLMCaller):
            def text(self, *a, **k):
                raise demo.GauntletCacheMiss("miss")

            def structured(self, *a, **k):
                raise demo.GauntletCacheMiss("miss")

        with pytest.raises(demo.GauntletCacheMiss):
            demo.run_mars_beat("draft an NDA", caller=Boom())  # infra error must propagate

    def test_playbook_error_surfaced_not_silent_zero(self):
        # A broken playbook (gate=ERROR) must NOT read as compliant 0%-flagged; it is
        # counted separately and rendered visibly.
        errs = [g.ArmResult("constrained", "x", "governingLaw", "v", True, "ERROR", [], 1, 0)]
        s = g.summarize(errs)["constrained"]
        assert s["playbook_flagged"] == 0 and s["playbook_errored"] == 1
        assert "ERR" in g.render_table(errs)

    def test_provider_error_aborts_not_recorded_as_arm_data(self):
        class ProviderDown(demo.LLMCaller):
            def text(self, *a, **k):
                raise RuntimeError("429 rate limited")

            def structured(self, *a, **k):
                raise RuntimeError("429 rate limited")

        case = g.build_suite("nda-mutual", mode="schema")[0]
        with pytest.raises(RuntimeError):  # infra error propagates, never becomes arm data
            g.run_raw(case, caller=ProviderDown())

    def test_unparseable_model_output_is_arm_data(self):
        class Garbage(demo.LLMCaller):
            def text(self, *a, **k):
                return "not json at all"

            def structured(self, *a, **k):
                return {}

        case = g.build_suite("nda-mutual", mode="schema")[0]
        r = g.run_raw(case, caller=Garbage())  # parse failure IS arm data
        assert r.gate == "ERROR" and not r.schema_valid

    def test_missing_playbook_surfaces_error(self, monkeypatch, tmp_path):
        monkeypatch.setattr(g, "_REPO", tmp_path)  # tmp has no .claude/legal.local.md
        o = g.oracle({"disclosingParty": "A", "receivingParty": "B",
                      "effectiveDate": "2026-01-01", "governingLaw": "Washington"})
        assert o["gate"] == "ERROR"  # missing configured playbook must not read as clean
