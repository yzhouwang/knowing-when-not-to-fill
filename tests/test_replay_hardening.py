"""
Replay-integrity hardening tests (audit fix plan P1.2 + P1.3).

P1.3(b) results-equality: the committed gauntlet_results.*.hard.json must EQUAL a
fresh offline replay of the committed cassettes. Beat 5 / demo_offline's table1 read
the RESULTS files, not the cassettes, so without this test the results files could
drift from what the cassettes actually replay to and nothing would notice.

P1.3(a) poisoned cassette: the cassette carries NO integrity signature or checksum.
Replay fails CLOSED on a MISSING entry (GauntletCacheMiss), but a TAMPERED entry --
forged response bytes under a valid key -- is trusted verbatim and silently changes
the replayed outcome. The test below documents that honestly (asserting the forged
value flows straight through) rather than pretending a guard exists.

P1.2: --record must never SILENTLY serve a cached entry (mixed-vintage cassettes),
and the grading comparators' normalization fallbacks must warn loudly, never
silently re-grade.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from contract_drafting import demo_mars_beat as demo
from contract_drafting import gauntlet as g
from contract_drafting.gauntlet import RecordReplayCaller

_EVAL = Path(__file__).resolve().parent.parent / "data" / "eval"

# (cassette, committed results, provider, model) for the four hard-suite recordings.
_HARD = [
    ("gauntlet_cassette.openai.hard.json", "gauntlet_results.openai.hard.json",
     "openai", "gpt-5.5"),
    ("gauntlet_cassette.deepseek.hard.json", "gauntlet_results.deepseek.hard.json",
     "deepseek", "deepseek-v4-pro"),
    ("gauntlet_cassette.deepseek-flash.hard.json", "gauntlet_results.deepseek-flash.hard.json",
     "deepseek", "deepseek-v4-flash"),
    ("gauntlet_cassette.anthropic.hard.json", "gauntlet_results.anthropic.hard.json",
     "anthropic", "claude-sonnet-4-6"),
]


# ---------------------------------------------------------------------------
# P1.3(b): committed results == fresh replay (per-case outcomes and all)
# ---------------------------------------------------------------------------
class TestCommittedResultsEqualFreshReplay:
    @pytest.mark.parametrize("cas,res,provider,model", _HARD, ids=[h[0] for h in _HARD])
    def test_full_report_equals_committed_results_file(self, cas, res, provider, model, capsys):
        """Replay the committed cassette through the FULL harness (four arms + arm E
        intent-guard + the within-guardrail ablation, i.e. the exact condition the
        committed file was written under) and assert the report -- summary, every
        per-case outcome row, intent_guard, ablation -- equals the committed
        gauntlet_results file. This is the missing link the audit flagged: table1
        reads these files, so they must be provably regenerable from the cassettes."""
        cas_path, res_path = _EVAL / cas, _EVAL / res
        assert cas_path.exists(), f"required cassette missing: {cas}"
        assert res_path.exists(), f"required results file missing: {res}"
        caller = RecordReplayCaller(None, cas_path, mode="replay")
        results = g.run_gauntlet(caller, provider=provider, model=model, mode="hard")
        suite = g.build_suite("nda-mutual", mode="hard")
        intent_guard = g.run_intent_guard(suite, caller=caller, provider=provider, model=model)
        ablation = g.run_ablation(suite, caller=caller, provider=provider, model=model)
        fresh = g.to_report(results, intent_guard, ablation)
        committed = json.loads(res_path.read_text(encoding="utf-8"))
        # per-case outcome rows first (the sharpest failure message on drift) ...
        assert fresh["cases"] == committed["cases"]
        # ... then everything (summary, intent_guard, ablation)
        assert fresh == committed
        # and the comparators' loud-fallback path was never exercised on committed data:
        # classifications came from real normalization, not the degraded string equality.
        assert "WARNING: _values_match" not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# P1.3(a): a poisoned cassette is NOT detected -- the forged value flows through
# ---------------------------------------------------------------------------
class TestPoisonedCassette:
    def test_tampered_response_silently_changes_replayed_outcome(self, tmp_path):
        """Tamper one recorded response (arm C, case c01) in a COPY of a committed
        cassette and replay it. There is NO signature: replay serves the forged bytes,
        the grader grades them, and the result row silently changes. Fail-closed covers
        cache MISSES only. If an integrity check is ever added, this test should flip
        to asserting the guard fires."""
        src = _EVAL / "gauntlet_cassette.openai.hard.json"
        assert src.exists(), "required cassette missing: gauntlet_cassette.openai.hard.json"
        suite = g.build_suite("nda-mutual", mode="hard")
        case = next(c for c in suite if c.id == "c01")

        # Locate c01's arm-C (constrained, no hatch) entry by recomputing its replay key.
        q, ctx, schema = demo.build_prompt(case.instruction, "nda-mutual", with_abstain=False)
        key = RecordReplayCaller._key("structured", q, ctx, schema,
                                      "openai", "gpt-5.5", demo._SYSTEM)
        cassette = json.loads(src.read_text(encoding="utf-8"))
        assert key in cassette, "replay key for c01 arm C not found -- prompt bytes drifted?"

        genuine = cassette[key]["response"]["governingLaw"]
        forged = "Hawaii" if genuine != "Hawaii" else "Alaska"  # valid in-enum, but forged
        cassette[key]["response"]["governingLaw"] = forged
        poisoned = tmp_path / "poisoned.json"
        poisoned.write_text(json.dumps(cassette), encoding="utf-8")

        r = g.run_constrained(case, caller=RecordReplayCaller(None, poisoned, mode="replay"),
                              provider="openai", model="gpt-5.5")
        assert r.chosen_value == forged != genuine   # the forged value shipped, no guard fired
        assert r.outcome == "wrong_sub"              # graded as if it were real model output


# ---------------------------------------------------------------------------
# P1.2: --record never SILENTLY serves a cached entry
# ---------------------------------------------------------------------------
class _Fake(demo.LLMCaller):
    def text(self, question, context, *, provider, model, system_prompt):
        return '{"governingLaw": "Washington"}'

    def structured(self, question, context, json_schema, *, provider, model, system_prompt):
        return {"governingLaw": "Washington"}


class _MustNotBeCalled(demo.LLMCaller):
    def text(self, *a, **k):
        raise AssertionError("inner caller must not be hit on a cassette cache hit")

    def structured(self, *a, **k):
        raise AssertionError("inner caller must not be hit on a cassette cache hit")


class TestRecordModeCacheHitWarning:
    def test_record_mode_warns_loudly_when_serving_cached_entry(self, tmp_path, capsys):
        path = tmp_path / "cassette.json"
        rec = RecordReplayCaller(_Fake(), path, mode="record")
        rec.text("q", "c", provider="p", model="m", system_prompt="s")
        capsys.readouterr()  # drain
        # A SECOND --record run over the same request: served from cache, but LOUDLY.
        rec2 = RecordReplayCaller(_MustNotBeCalled(), path, mode="record")
        out = rec2.text("q", "c", provider="p", model="m", system_prompt="s")
        assert out == '{"governingLaw": "Washington"}'  # response unchanged (no re-record)
        err = capsys.readouterr().err
        assert "WARNING" in err and "CACHED" in err and "record" in err.lower()

    def test_record_mode_fresh_recording_has_no_warning(self, tmp_path, capsys):
        rec = RecordReplayCaller(_Fake(), tmp_path / "fresh.json", mode="record")
        rec.text("q", "c", provider="p", model="m", system_prompt="s")
        assert "WARNING" not in capsys.readouterr().err

    def test_replay_mode_cache_hit_stays_silent(self, tmp_path, capsys):
        """The warning is record-mode only: replay serving from cache is the designed
        behavior (the whole point of the cassette), not a mixed-vintage hazard."""
        path = tmp_path / "cassette.json"
        RecordReplayCaller(_Fake(), path, mode="record").text(
            "q", "c", provider="p", model="m", system_prompt="s")
        capsys.readouterr()
        RecordReplayCaller(None, path, mode="replay").text(
            "q", "c", provider="p", model="m", system_prompt="s")
        assert "WARNING" not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# P1.2: comparator normalization fallback warns loudly, never silently re-grades
# ---------------------------------------------------------------------------
class TestValuesMatchFallbackWarns:
    def test_governinglaw_fallback_warns(self, monkeypatch, capsys):
        import contract_drafting.jurisdiction_map as jm

        def boom(*a, **k):
            raise RuntimeError("jurisdiction map unreadable")

        monkeypatch.setattr(jm, "to_identifier", boom)
        # identical strings still match under the degraded raw-equality fallback ...
        assert g._values_match("governingLaw", "New_York", "New_York") is True
        # ... but display-form no longer normalizes: the degradation is real and LOUD.
        assert g._values_match("governingLaw", "New York", "New_York") is False
        err = capsys.readouterr().err
        assert "WARNING: _values_match" in err and "falling back" in err

    def test_enum_fallback_warns(self, monkeypatch, capsys):
        import contract_drafting.jurisdiction_map as jm

        def boom(*a, **k):
            raise RuntimeError("enum map unreadable")

        monkeypatch.setattr(jm, "to_identifier_enum", boom)
        assert g._values_match("disputeForum", "SIAC", "SIAC") is True
        err = capsys.readouterr().err
        assert "WARNING: _values_match" in err and "disputeForum" in err

    def test_no_fallback_no_warning(self, capsys):
        assert g._values_match("governingLaw", "New York", "New_York") is True
        assert "WARNING" not in capsys.readouterr().err
