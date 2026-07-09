"""
Regression tests for the intent-gate fail-open (AUDIT_FIX_PLAN_2026-07-02, P0.3).

The defect: verify_intent short-circuited on the _MIGHT_NAME_LAW regex pre-gate
BEFORE the allow_llm_fallback branch, so an evasive governing-law phrasing outside
the regex vocabulary ("Apply Ontario legislation.") returned [] without the LLM ever
being consulted -- a fail-open on the exact path the paper claims fails closed.

The fix under test: (1) with allow_llm_fallback=True the LLM extractor is consulted
UNCONDITIONALLY on any non-empty instruction (no regex pre-gate in front of it);
(2) the pre-gate survives only as the deterministic offline path's scope limiter and
was widened (legislation | statutes? | legal system | construed), together with the
strong fail-closed signal, so the offline path now catches the statutes/legislation
phrasings too. The third phrasing ("under Ontario rules") remains an offline residual
band -- see TestOfflineWidenedRegex for the documentation of that band.
"""
from __future__ import annotations

import pytest

from contract_drafting import intent_check as ic

# The three evasive phrasings from the audit, verbatim. All name Ontario (not in the
# nda-mutual governingLaw enum) without using the old trigger vocabulary
# (govern/laws of/jurisdiction/law).
_EVASIVE_STATUTES = "construed per the statutes of Ontario"
_EVASIVE_LEGISLATION = "Apply Ontario legislation."
_EVASIVE_RULES = "Interpret this agreement under Ontario rules."
_EVASIVE = (_EVASIVE_STATUTES, _EVASIVE_LEGISLATION, _EVASIVE_RULES)


def _mock_llm_recording(monkeypatch, answer):
    """Canned LLM extraction that also records each call's context, so tests can
    assert the LLM WAS consulted (the old fail-open never reached it)."""
    from contract_drafting import llm
    calls: list[str] = []

    def fake(*a, **k):
        calls.append(k.get("context", ""))
        return answer

    monkeypatch.setattr(llm, "call_llm", fake)
    return calls


def _llm_must_not_run(monkeypatch):
    from contract_drafting import llm
    monkeypatch.setattr(llm, "call_llm",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("LLM should not be called")))


class TestLLMConsultedUnconditionally:
    """allow_llm_fallback=True: the LLM must be consulted even when the instruction
    uses none of the deterministic extractor's vocabulary (the audited fail-open)."""

    @pytest.mark.parametrize("instr", _EVASIVE)
    def test_evasive_phrasing_reaches_llm_and_flags(self, monkeypatch, instr):
        calls = _mock_llm_recording(monkeypatch, "Ontario")
        w = ic.verify_intent(instr, {"governingLaw": "Washington"})
        assert calls, f"LLM extractor was never consulted for: {instr!r}"
        assert calls[0] == instr  # the full instruction is what gets extracted from
        assert w and "not a supported jurisdiction" in w[0]

    def test_no_law_named_llm_still_consulted_no_flag(self, monkeypatch):
        # Unconditional means unconditional: even a law-free instruction goes to the
        # LLM (which answers NONE) -- and a NONE extraction must not false-flag.
        calls = _mock_llm_recording(monkeypatch, "NONE")
        assert ic.verify_intent("Draft an NDA between TestCo and AcmeCorp.",
                                {"governingLaw": "Washington"}) == []
        assert calls, "LLM extractor must be consulted unconditionally when enabled"

    def test_empty_instruction_no_llm(self, monkeypatch):
        # The one exception: an empty instruction asked for nothing -- no LLM call.
        _llm_must_not_run(monkeypatch)
        assert ic.verify_intent("", {"governingLaw": "Washington"}) == []


class TestOfflineWidenedRegex:
    """allow_llm_fallback=False (deterministic offline path, the Arm E / demo config):
    the widened _MIGHT_NAME_LAW + _GOVERNING_LAW_SIGNAL vocabulary (legislation |
    statutes? | legal system | construed) now fails closed on the statutes/legislation
    evasions the audit demonstrated.

    RESIDUAL BAND (documented, by design): offline coverage is bounded by the regex
    vocabulary. "Interpret this agreement under Ontario rules." uses none of the
    widened tokens, so the offline path still cannot see it and returns [] -- only the
    (unconditional) LLM path catches it. The offline gate is a scope-limited
    best-effort, not a completeness claim.
    """

    @pytest.mark.parametrize("instr", [_EVASIVE_STATUTES, _EVASIVE_LEGISLATION])
    def test_widened_vocabulary_fails_closed(self, monkeypatch, instr):
        _llm_must_not_run(monkeypatch)
        w = ic.verify_intent(instr, {"governingLaw": "Washington"},
                             allow_llm_fallback=False)
        assert w and "review required" in w[0], instr

    def test_residual_band_under_x_rules_offline(self, monkeypatch):
        # See class docstring: out-of-vocabulary -> unverifiable offline -> [] (the
        # residual band). This test DOCUMENTS the band; if the vocabulary is ever
        # widened to cover "rules", update this test and the class docstring together.
        _llm_must_not_run(monkeypatch)
        assert ic.verify_intent(_EVASIVE_RULES, {"governingLaw": "Washington"},
                                allow_llm_fallback=False) == []

    def test_statute_of_limitations_not_false_blocked(self, monkeypatch):
        # The widened signal must not fail closed on the common claims-clause phrase
        # "statute of limitations" (not a choice-of-law ask).
        _llm_must_not_run(monkeypatch)
        assert ic.verify_intent(
            "Draft an NDA; claims are subject to the applicable statute of limitations.",
            {"governingLaw": "Washington"}, allow_llm_fallback=False) == []

    def test_venue_only_still_not_false_blocked(self, monkeypatch):
        # The widening must not disturb the deliberate venue/forum carve-out.
        _llm_must_not_run(monkeypatch)
        assert ic.verify_intent(
            "the parties submit to the exclusive jurisdiction in King County courts",
            {"governingLaw": "Washington"}, allow_llm_fallback=False) == []
