"""
Tests for PR1 of the silent-substitution hardening: the intent-consistency gate
(intent_check.py) + the semantic schema checks (calendar dates, integer bounds).

The gate is LLM-authoritative for ambiguous governing-law language (compound phrases,
venue vs law, casual casing) with a deterministic fast-path for clean single
jurisdictions and fail-closed-on-strong-signal when the LLM is unavailable. LLM-path
tests monkeypatch contract_drafting.llm.call_llm with a canned extraction.
"""
from __future__ import annotations

import pytest

from contract_drafting import intent_check as ic
from contract_drafting import schema_validator as sv

# A schema-valid, in-bounds nda-mutual data dict to mutate per test.
_VALID = {
    "disclosingParty": "A", "receivingParty": "B", "effectiveDate": "2026-01-15",
    "governingLaw": "Washington", "purpose": "exploring a deal",
    "termMonths": 24, "noticeDays": 30, "survivalYears": 3,
    "mutual": True, "hasNonCompete": False, "hasNonSolicitation": False,
    "hasResidualsClause": False,
}


def _mock_llm(monkeypatch, answer):
    """Make call_llm return a canned governing-law extraction (comma-separated/NONE)."""
    from contract_drafting import llm
    monkeypatch.setattr(llm, "call_llm", lambda *a, **k: answer)


def _llm_must_not_run(monkeypatch):
    from contract_drafting import llm
    monkeypatch.setattr(llm, "call_llm",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("LLM should not be called")))


def _llm_unavailable(monkeypatch):
    from contract_drafting import llm
    monkeypatch.setattr(llm, "call_llm", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no key")))


class TestExtract:
    def test_explicit_forms(self):
        assert ic.extract_jurisdiction("governed by the laws of the Province of Ontario, Canada")
        assert ic.extract_jurisdiction("under Washington law")
        assert ic.extract_jurisdiction("Governing law = DIFC")
        assert ic.extract_jurisdiction("New York law governs")

    def test_none_when_absent(self):
        assert ic.extract_jurisdiction("Draft an NDA between TestCo and AcmeCorp.") == []


class TestFastPath:
    """OFFLINE deterministic path (allow_llm_fallback=False): clean single-jurisdiction
    cases resolve without an LLM. (Online, the LLM is always authoritative -- see
    TestLLMAuthoritative -- so the fast-path is reserved for the no-LLM path.)"""

    def test_representable_match(self, monkeypatch):
        _llm_must_not_run(monkeypatch)
        assert ic.verify_intent("under Washington law", {"governingLaw": "Washington"},
                                allow_llm_fallback=False) == []

    def test_lowercase_resolves(self, monkeypatch):
        _llm_must_not_run(monkeypatch)
        assert ic.verify_intent("the laws of new york", {"governingLaw": "New_York"},
                                allow_llm_fallback=False) == []

    def test_state_of_prefix_resolves(self, monkeypatch):
        _llm_must_not_run(monkeypatch)
        assert ic.verify_intent("governed by the laws of the state of New York",
                                {"governingLaw": "New_York"}, allow_llm_fallback=False) == []

    def test_full_official_name_resolves(self, monkeypatch):
        _llm_must_not_run(monkeypatch)
        assert ic.verify_intent("the laws of the Republic of Singapore",
                                {"governingLaw": "Republic_of_Singapore"}, allow_llm_fallback=False) == []

    def test_venue_clause_truncated_cleanly(self, monkeypatch):
        _llm_must_not_run(monkeypatch)
        assert ic.verify_intent("governed by the laws of New York with venue in King County",
                                {"governingLaw": "New_York"}, allow_llm_fallback=False) == []

    def test_labeled_law_with_venue_tail(self, monkeypatch):
        _llm_must_not_run(monkeypatch)
        assert ic.verify_intent("Governing law: New York, venue in King County",
                                {"governingLaw": "New_York"}, allow_llm_fallback=False) == []

    def test_law_and_lowercase_tail(self, monkeypatch):
        # " and <lowercase>" is a clause tail, not a compound jurisdiction
        _llm_must_not_run(monkeypatch)
        assert ic.verify_intent("governed by the laws of Washington and venue in King County",
                                {"governingLaw": "Washington"}, allow_llm_fallback=False) == []

    def test_verb_before_law_representable(self, monkeypatch):
        _llm_must_not_run(monkeypatch)
        assert ic.verify_intent("Apply California law to this agreement.",
                                {"governingLaw": "California"}, allow_llm_fallback=False) == []

    def test_representable_mismatch_flagged(self, monkeypatch):
        _llm_must_not_run(monkeypatch)
        w = ic.verify_intent("the laws of New York", {"governingLaw": "Delaware"},
                             allow_llm_fallback=False)
        assert w and "does not match" in w[0]


class TestLLMAuthoritative:
    """Ambiguous language routes to the LLM, which is authoritative."""

    def test_unrepresentable_substitution_flagged(self, monkeypatch):
        _mock_llm(monkeypatch, "Ontario")
        w = ic.verify_intent("governed by the laws of the Province of Ontario, Canada",
                             {"governingLaw": "Delaware"})
        assert w and "not a supported jurisdiction" in w[0]

    def test_x_law_applies_unrepresentable(self, monkeypatch):
        _mock_llm(monkeypatch, "Ontario")
        w = ic.verify_intent("Ontario law applies to this agreement.", {"governingLaw": "Delaware"})
        assert w and "not a supported jurisdiction" in w[0]

    def test_compound_unrepresentable_not_masked(self, monkeypatch):
        # the truncation guard defers the compound; the LLM returns both -> Ontario flags
        _mock_llm(monkeypatch, "New York\nOntario")
        w = ic.verify_intent("governed by the laws of New York, Ontario", {"governingLaw": "New_York"})
        assert w and "not a supported jurisdiction" in w[0]

    def test_multiple_governing_laws_flagged(self, monkeypatch):
        # two representable governing laws cannot both fit one field -> flag
        _mock_llm(monkeypatch, "Washington\nNew York")
        w = ic.verify_intent("under Washington law and under New York law",
                             {"governingLaw": "Washington"})
        assert w and "multiple governing laws" in w[0]

    def test_venue_only_returns_none(self, monkeypatch):
        # the LLM tells venue from governing law -> NONE -> no false block
        _mock_llm(monkeypatch, "NONE")
        assert ic.verify_intent("the parties agree to exclusive jurisdiction in King County courts",
                                {"governingLaw": "Delaware"}) == []

    def test_england_and_wales(self, monkeypatch):
        _mock_llm(monkeypatch, "England and Wales")
        assert ic.verify_intent("governed by the laws of England and Wales",
                                {"governingLaw": "England_and_Wales"}) == []

    def test_compound_with_earlier_occurrence(self, monkeypatch):
        # the same words appear earlier ("New York office"); the SPAN-based tail check
        # must inspect the governing-law capture, not the first occurrence
        _mock_llm(monkeypatch, "New York\nOntario")
        w = ic.verify_intent("Open a New York office; governed by the laws of New York, Ontario",
                             {"governingLaw": "New_York"})
        assert w and "not a supported jurisdiction" in w[0]

    def test_partial_second_law_mention_not_masked(self, monkeypatch):
        # 'Ontario law also applies' defeats the verb pattern; law-mentions > spans ->
        # the fast-path defers and the LLM catches the un-representable Ontario
        _mock_llm(monkeypatch, "Washington\nOntario")
        w = ic.verify_intent("Washington law governs, and Ontario law also applies",
                             {"governingLaw": "Washington"})
        assert w and "not a supported jurisdiction" in w[0]

    def test_common_alias_resolves(self, monkeypatch):
        # the LLM extracts the common short name; the gate maps it to the official
        # enum display name instead of false-blocking ("Singapore" -> Republic of Singapore)
        _mock_llm(monkeypatch, "Singapore")
        assert ic.verify_intent("under Singapore law", {"governingLaw": "Republic_of_Singapore"}) == []


class TestAliasResolution:
    def test_common_country_aliases(self):
        assert ic._resolve_clean("Singapore", "nda-mutual") == "Republic_of_Singapore"
        assert ic._resolve_clean("India", "nda-mutual") == "Republic_of_India"
        assert ic._resolve_clean("Saudi Arabia", "nda-mutual") == "Kingdom_of_Saudi_Arabia"

    def test_unrepresentable_still_none(self):
        for bad in ("Ontario", "Scotland", "Macao"):
            assert ic._resolve_clean(bad, "nda-mutual") is None, bad

    def test_ambiguous_alias_is_none(self):
        # "Carolina" matches North + South Carolina -> ambiguous -> None (flag for review)
        assert ic._resolve_clean("Carolina", "nda-mutual") is None


class TestFailClosedAndNoOp:
    def test_fail_closed_when_unavailable_with_signal(self, monkeypatch):
        _llm_unavailable(monkeypatch)
        w = ic.verify_intent("governed by the laws of Ontario", {"governingLaw": "Delaware"})
        assert w and "could not extract or verify" in w[0]

    def test_fail_closed_when_disabled_with_signal(self):
        w = ic.verify_intent("governed by the laws of Ontario", {"governingLaw": "Delaware"},
                             allow_llm_fallback=False)
        assert w and "review required" in w[0]

    def test_no_signal_no_check_no_llm(self, monkeypatch):
        _llm_must_not_run(monkeypatch)
        assert ic.verify_intent("Draft an NDA between A and B.", {"governingLaw": "Delaware"}) == []

    def test_governance_not_false_blocked(self):
        # "corporate governance" is not a STRONG signal -> no fail-closed without LLM
        assert ic.verify_intent("Draft an NDA about corporate governance between A and B.",
                                {"governingLaw": "Delaware"}, allow_llm_fallback=False) == []

    def test_law_firm_not_false_blocked(self):
        assert ic.verify_intent("Draft an NDA for Smith Law Firm and AcmeCorp.",
                                {"governingLaw": "Delaware"}, allow_llm_fallback=False) == []

    def test_forum_only_not_false_blocked(self):
        # bare "jurisdiction" (forum/venue) is NOT a strong governing-law signal
        assert ic.verify_intent("the parties submit to the exclusive jurisdiction in King County courts",
                                {"governingLaw": "Delaware"}, allow_llm_fallback=False) == []

    def test_under_x_law_unresolvable_fails_closed(self):
        # "under Ontario law" -- a governing-law PATTERN matched but cannot resolve;
        # without the LLM the gate must fail closed, not silently pass.
        w = ic.verify_intent("under Ontario law", {"governingLaw": "Delaware"},
                             allow_llm_fallback=False)
        assert w and "review required" in w[0]

    def test_verb_before_law_unresolvable_fails_closed(self):
        # "Apply/subject to Ontario law" must also fail closed offline (was fail-open)
        for instr in ("Apply Ontario law to this agreement.", "subject to Ontario law"):
            w = ic.verify_intent(instr, {"governingLaw": "Delaware"}, allow_llm_fallback=False)
            assert w, instr

    def test_string_enum_template_gate_runs(self, monkeypatch):
        # consulting has a governingLaw ENUM (no jurisdictions.map) -- the gate MUST
        # apply (source of truth is the schema enum); a representable value passes.
        _llm_must_not_run(monkeypatch)
        assert ic.verify_intent("under California law", {"governingLaw": "California"},
                                template_name="consulting", allow_llm_fallback=False) == []

    def test_string_enum_template_substitution_flagged(self, monkeypatch):
        # an out-of-enum ask on a string-enum template must flag, not be skipped
        _mock_llm(monkeypatch, "Ontario")
        w = ic.verify_intent("governed by the laws of Ontario", {"governingLaw": "California"},
                             template_name="consulting")
        assert w and "not a supported jurisdiction" in w[0]

    def test_unknown_template_is_noop(self):
        # no governingLaw enum at all (missing schema) -> gate N/A, no false block
        assert ic.verify_intent("under California law", {"governingLaw": "Anything"},
                                template_name="does-not-exist", allow_llm_fallback=False) == []

    def test_partial_second_law_offline_fails_closed(self):
        # 'Ontario law also applies' (the 'also' defeats the verb pattern) -> law-mention
        # count exceeds captured spans -> fast-path defers -> offline fails closed
        w = ic.verify_intent("Washington law governs, and Ontario law also applies",
                             {"governingLaw": "Washington"}, allow_llm_fallback=False)
        assert w  # not fail-open


class TestGuard:
    def test_raises_on_warning(self):
        with pytest.raises(ic.IntentSubstitutionError):
            ic.guard_intent("governed by the laws of Ontario", {"governingLaw": "Delaware"},
                            allow_llm_fallback=False)

    def test_override_returns_warnings(self):
        w = ic.guard_intent("governed by the laws of Ontario", {"governingLaw": "Delaware"},
                            allow_substitution=True, allow_llm_fallback=False)
        assert w  # report-only: returned, not raised


class TestResolverFailOpen:
    """Audit regression: subset-with-uniqueness silently resolved DISTINCT jurisdictions
    onto a different enum value (Mexico->New_Mexico, United Kingdom->United_Arab_Emirates).
    Fixed to token EQUALITY + explicit alias allowlist."""

    def test_distinct_jurisdictions_do_not_resolve(self):
        # incl. the token-heuristic conflations: DPRK/Taiwan share a core name with an enum entry
        for x in ("Mexico", "United Kingdom", "Jersey", "York", "Hampshire", "Ontario", "Scotland",
                  "Democratic People's Republic of Korea", "North Korea", "Republic of China",
                  # prefix-strip fail-opens: country/province/commonwealth-qualified state names
                  "Republic of Texas", "Province of California", "Republic of England and Wales",
                  "Commonwealth of Texas", "Commonwealth of Japan"):
            assert ic._resolve_clean(x, "nda-mutual") is None, x

    def test_legit_aliases_still_resolve(self):
        for x, exp in [("Singapore", "Republic_of_Singapore"), ("China", "Peoples_Republic_of_China"),
                       ("India", "Republic_of_India"), ("Saudi Arabia", "Kingdom_of_Saudi_Arabia"),
                       ("Korea", "Republic_of_Korea"), ("South Korea", "Republic_of_Korea"),
                       ("South Africa", "Republic_of_South_Africa"), ("Hong Kong", "Hong_Kong_SAR"),
                       ("Nigeria", "Federal_Republic_of_Nigeria"), ("England", "England_and_Wales"),
                       # legit US-state official forms must still resolve (prefix-strip kept for these)
                       ("Commonwealth of Massachusetts", "Massachusetts"), ("State of New York", "New_York"),
                       ("the laws of Delaware", "Delaware")]:
            assert ic._resolve_clean(x, "nda-mutual") == exp, x

    def test_mexico_substitution_caught_end_to_end(self, monkeypatch):
        _mock_llm(monkeypatch, "Mexico")  # online: LLM extracts Mexico
        assert ic.verify_intent("governed by the laws of Mexico", {"governingLaw": "New_Mexico"})
        # offline: Mexico unresolvable -> fail closed, not silently matched
        assert ic.verify_intent("governed by the laws of Mexico", {"governingLaw": "New_Mexico"},
                                allow_llm_fallback=False)

    def test_uk_does_not_false_resolve_to_uae(self, monkeypatch):
        _mock_llm(monkeypatch, "United Kingdom")
        w = ic.verify_intent("under United Kingdom law", {"governingLaw": "England_and_Wales"})
        assert all("United_Arab_Emirates" not in x for x in w)  # must not name a jurisdiction never asked

    def test_country_state_homonym_not_fail_open(self, monkeypatch):
        # Georgia (country) shares its name with the US-state enum value. ANY country-
        # qualified form must NOT resolve to the state (regardless of LLM wording);
        # bare 'Georgia' and 'State of Georgia' (the US state) still resolve.
        for country in ("Country of Georgia", "Republic of Georgia", "the Nation of Georgia",
                        "sovereign state of Georgia"):
            assert ic._resolve_clean(country, "nda-mutual") is None, country
        assert ic._resolve_clean("Georgia", "nda-mutual") == "Georgia"
        assert ic._resolve_clean("State of Georgia", "nda-mutual") == "Georgia"
        _mock_llm(monkeypatch, "Republic of Georgia")
        w = ic.verify_intent("governed by the law of the country of Georgia", {"governingLaw": "Georgia"})
        assert w  # flagged: country Georgia != the US-state enum value

    def test_footgun_sentinel_not_representable(self):
        from contract_drafting import jurisdiction_map as jm
        assert "OTHER" not in jm.known_identifiers("nda-mutual")
        assert jm.resolve_to_identifier("OTHER") is None


class TestAbstainHatch:
    """PR2: the model emits governingLaw=OTHER (+ governingLawRaw) instead of substituting."""

    def test_resolve_excludes_sentinel(self):
        # OTHER is a valid enum value but NOT a resolvable jurisdiction
        assert ic._resolve_clean("OTHER", "nda-mutual") is None

    def test_abstention_correct_on_unrepresentable_online(self, monkeypatch):
        # model abstained on an un-representable ask -> CORRECT, not a substitution
        _mock_llm(monkeypatch, "Ontario")
        assert ic.verify_intent("governed by the laws of Ontario", {"governingLaw": "OTHER"}) == []

    def test_abstention_unverified_offline_flags_for_review(self):
        # offline cannot PROVE the ask un-representable -> conservatively flag the
        # abstention for review rather than accept it (could be a wrong abstention)
        w = ic.verify_intent("governed by the laws of Ontario", {"governingLaw": "OTHER"},
                             allow_llm_fallback=False)
        assert w and "could not be verified offline" in w[0]

    def test_abstention_unverifiable_compound_flags(self):
        # offline: OTHER + a compound representable ask the regex can't resolve -> must
        # NOT be accepted as a correct abstention (it could be a wrong abstention)
        w = ic.verify_intent("governed by the laws of Delaware, New York", {"governingLaw": "OTHER"},
                             allow_llm_fallback=False)
        assert w  # flagged for review, not silently accepted

    def test_render_path_refuses_other(self):
        # the render chokepoint fails closed on OTHER -> covers draft_with_data + any path
        from contract_drafting import cicero_bridge
        res = cicero_bridge.draft_with_data(
            {"disclosingParty": "A", "receivingParty": "B", "effectiveDate": "2026-01-15",
             "governingLaw": "OTHER", "purpose": "x"}, template_name="nda-mutual")
        assert res.success is False  # never renders 'laws of OTHER'

    def test_abstention_wrong_on_representable(self):
        # abstaining on a SUPPORTED jurisdiction is a mistake -> flag
        w = ic.verify_intent("governed by Delaware law", {"governingLaw": "OTHER"},
                             allow_llm_fallback=False)
        assert w and "abstained" in w[0] and "IS supported" in w[0]

    def test_pipeline_escalates_on_other_fail_closed(self, tmp_path):
        """An abstention is NOT a playbook violation: it routes to ESCALATED (human
        review), still fails closed (no rendered contract), and ALWAYS writes an audit
        row carrying the abstained field, the raw captured ask, and a sha256 over the
        canonical escalation record."""
        import json as _json
        from contract_drafting.compliance_draft import draft_contract, DraftRequest, get_audit_log
        db = str(tmp_path / "audit.db")
        res = draft_contract(DraftRequest(disclosing_party="A", receiving_party="B",
                                          effective_date="2026-01-15", governing_law="OTHER",
                                          governing_law_raw="laws of Scotland"),
                             db_path=db)
        assert res.get("gate_result") == "ESCALATED"
        assert res.get("abstained") is True
        assert res.get("output_path") is None  # fail-closed: no signable contract
        assert res.get("abstained_fields") == ["governingLaw"]
        assert res.get("governing_law_raw") == "laws of Scotland"
        # the audit row is ALWAYS written
        rows = get_audit_log(db, doc_id=res["audit_id"])
        assert len(rows) == 1
        row = rows[0]
        assert row["gate_result"] == "ESCALATED"
        slot = _json.loads(row["slot_values"])
        assert slot["status"] == "ESCALATED"
        assert slot["abstentions"] == [
            {"field": "governingLaw", "sentinel": "OTHER", "raw": "laws of Scotland"}]
        assert len(slot["escalation_sha256"]) == 64
        assert slot["escalation_sha256"] == res["escalation_sha256"]


class TestCalendarDates:
    def test_impossible_dates_rejected(self):
        for bad in ("2026-02-30", "2026-13-45", "0000-00-00", "2026-02-31"):
            errs = sv.validate_semantics({**_VALID, "effectiveDate": bad})
            assert any("calendar date" in e for e in errs), bad

    def test_real_date_ok(self):
        assert not any("calendar date" in e for e in sv.validate_semantics({**_VALID, "effectiveDate": "2026-01-15"}))


class TestIntegerBounds:
    def test_float_out_of_range_rejected(self):
        # a whole-valued float (9999.0) bypasses Draft7 'integer' + an int-only check
        assert any("out of range" in e for e in sv.validate_semantics({**_VALID, "survivalYears": 9999.0}))

    def test_absurd_values_rejected(self):
        assert any("out of range" in e for e in sv.validate_semantics({**_VALID, "survivalYears": 9999}))
        assert any("out of range" in e for e in sv.validate_semantics({**_VALID, "termMonths": 0}))
        assert any("out of range" in e for e in sv.validate_semantics({**_VALID, "termMonths": -5}))

    def test_sane_values_ok(self):
        # both layers clean on a valid dict; the eval oracle (validate_template_data)
        # stays schema-pure so 9999 remains a wrong_sub there, not a schema error.
        assert sv.validate_template_data(_VALID) == []
        assert sv.validate_semantics(_VALID) == []
        assert sv.validate_template_data({**_VALID, "survivalYears": 9999}) == []  # schema allows it
