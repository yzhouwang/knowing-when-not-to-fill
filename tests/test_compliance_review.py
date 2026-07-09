"""
test_compliance_review.py — Contract review pipeline (triage-nda / review-contract).

Covers contract_drafting.compliance_review: the two public pipeline entry points
(triage_nda / review_contract) plus the pure helpers (_read_contract,
_build_playbook_context, _parse_review_response, _cross_validate_triage).

All LLM calls are mocked — NO real network access. Following the house style in
tests/test_contract_drafting.py, we monkeypatch the module-level ``call_llm``
symbol that compliance_review imported (``from contract_drafting.llm import
call_llm``) so the pipeline never touches a provider, and every audit write goes
to a throwaway sqlite file under tmp_path.
"""
from __future__ import annotations

import json

import pytest

from contract_drafting import compliance_review as cr
from contract_drafting.compliance_review import (
    ReviewRequest,
    triage_nda,
    review_contract,
    _read_contract,
    _build_playbook_context,
    _parse_review_response,
    _cross_validate_triage,
)
from contract_drafting.compliance_playbook import (
    Playbook,
    PlaybookRule,
    NDADefaults,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def tmp_db(tmp_path) -> str:
    return str(tmp_path / "review_audit.db")


@pytest.fixture
def benign_nda() -> str:
    return (
        "MUTUAL NON-DISCLOSURE AGREEMENT between Acme Corp (disclosing) and "
        "Beta LLC (receiving). Term: 2 years. Standard carveouts for publicly "
        "available information, independent development, and disclosure required "
        "by law. Governing law: Washington. Injunctive relief available."
    )


@pytest.fixture
def noncompete_nda() -> str:
    return (
        "UNILATERAL NON-DISCLOSURE AGREEMENT. The receiving party additionally "
        "agrees to a non-compete covenant for two years following termination. "
        "Governing law: Delaware."
    )


def _mock_call_llm(monkeypatch, response: str) -> dict:
    """Replace compliance_review.call_llm with a stub returning ``response``.

    Returns a dict that captures the kwargs of the last call so tests can assert
    the constraint (system prompt / playbook context) was actually wired in.
    """
    captured: dict = {}

    def fake(**kwargs):
        captured.update(kwargs)
        return response

    monkeypatch.setattr(cr, "call_llm", fake)
    return captured


# A minimal valid triage JSON payload the fake LLM can emit.
def _triage_json(classification="GREEN", nda_type="mutual", issues=None) -> str:
    return json.dumps({
        "classification": classification,
        "nda_type": nda_type,
        "parties": {"disclosing": "Acme Corp", "receiving": "Beta LLC"},
        "term": "2 years",
        "governing_law": "Washington",
        "screening_results": [{"category": "CARVEOUTS", "status": "PASS", "notes": "ok"}],
        "issues": issues or [],
        "recommendation": "Approve under standard delegation.",
    })


def _review_json(clauses, overall="GREEN") -> str:
    return json.dumps({
        "contract_type": "saas",
        "parties": {"party_a": "Acme", "party_b": "Beta"},
        "overall_risk": overall,
        "clauses": clauses,
        "top_issues": ["issue one"],
        "negotiation_strategy": "hold the cap",
        "negotiation_tiers": {
            "tier_1_must_haves": ["cap"],
            "tier_2_should_haves": [],
            "tier_3_nice_to_haves": [],
        },
    })


# ===========================================================================
# _read_contract
# ===========================================================================
class TestReadContract:
    def test_direct_text_preferred(self):
        req = ReviewRequest(contract_text="hello contract", file_path="/does/not/matter")
        assert _read_contract(req) == "hello contract"

    def test_no_text_no_path_raises(self):
        with pytest.raises(ValueError, match="No contract text or file path"):
            _read_contract(ReviewRequest())

    def test_missing_file_raises(self, tmp_path):
        req = ReviewRequest(file_path=str(tmp_path / "nope.txt"))
        with pytest.raises(FileNotFoundError, match="Contract file not found"):
            _read_contract(req)

    def test_txt_file_read(self, tmp_path):
        p = tmp_path / "nda.txt"
        p.write_text("file body text", encoding="utf-8")
        assert _read_contract(ReviewRequest(file_path=str(p))) == "file body text"

    def test_unsupported_suffix_raises(self, tmp_path):
        p = tmp_path / "nda.rtf"
        p.write_text("x", encoding="utf-8")
        with pytest.raises(ValueError, match="Unsupported file format"):
            _read_contract(ReviewRequest(file_path=str(p)))


# ===========================================================================
# _build_playbook_context
# ===========================================================================
class TestBuildPlaybookContext:
    def test_includes_rules_and_nda_defaults(self):
        pb = Playbook(
            rules=[PlaybookRule(
                clause_type="Limitation of Liability",
                standard_position="12 months fees",
                acceptable_range="6-24 months",
                escalation_trigger="uncapped",
            )],
            nda_defaults=NDADefaults(mutual_required=True, term_years_standard="2-3"),
        )
        ctx = _build_playbook_context(pb)
        assert "Limitation of Liability" in ctx
        assert "12 months fees" in ctx
        assert "uncapped" in ctx
        # NDA defaults block appended
        assert "NDA Defaults:" in ctx
        assert "mutual=True" in ctx
        assert "non-compete" in ctx  # a default prohibited provision

    def test_empty_playbook_still_renders_defaults_line(self):
        ctx = _build_playbook_context(Playbook(rules=[]))
        assert "NDA Defaults:" in ctx


# ===========================================================================
# _parse_review_response
# ===========================================================================
class TestParseReviewResponse:
    def test_plain_json(self):
        assert _parse_review_response('{"classification": "GREEN"}') == {"classification": "GREEN"}

    def test_strips_code_fence(self):
        fenced = '```json\n{"classification": "RED"}\n```'
        assert _parse_review_response(fenced) == {"classification": "RED"}

    def test_strips_bare_fence(self):
        fenced = '```\n{"a": 1}\n```'
        assert _parse_review_response(fenced) == {"a": 1}

    def test_malformed_returns_raw_and_error(self):
        out = _parse_review_response("this is not json at all")
        assert out["raw_analysis"] == "this is not json at all"
        assert "parse_error" in out


# ===========================================================================
# _cross_validate_triage
# ===========================================================================
class TestCrossValidateTriage:
    def test_parse_error_passthrough(self):
        pb = Playbook()
        broken = {"raw_analysis": "x", "parse_error": "boom"}
        assert _cross_validate_triage(broken, pb, "text") is broken

    def test_unilateral_when_mutual_required_upgrades_to_red(self):
        pb = Playbook(nda_defaults=NDADefaults(mutual_required=True))
        result = {"classification": "YELLOW", "nda_type": "unilateral_receiving", "issues": []}
        out = _cross_validate_triage(result, pb, "some benign text")
        assert out["classification"] == "RED"
        assert any("Unilateral NDA" in i["description"] for i in out["issues"])

    def test_prohibited_provision_detected_in_text(self):
        pb = Playbook(nda_defaults=NDADefaults(
            mutual_required=True,
            prohibited_provisions=["non-compete"],
        ))
        result = {"classification": "GREEN", "nda_type": "mutual", "issues": []}
        out = _cross_validate_triage(result, pb, "includes a non-compete covenant")
        assert out["classification"] == "RED"
        assert any("non-compete" in i["description"].lower() for i in out["issues"])

    def test_already_flagged_not_double_counted(self):
        pb = Playbook(nda_defaults=NDADefaults(
            mutual_required=True,
            prohibited_provisions=["non-compete"],
        ))
        result = {
            "classification": "RED",
            "nda_type": "mutual",
            "issues": [{"description": "found a non-compete already", "severity": "RED"}],
        }
        out = _cross_validate_triage(result, pb, "text with non-compete")
        # No duplicate 'Prohibited provision detected' issue added.
        added = [i for i in out["issues"] if i["description"].startswith("Prohibited provision detected")]
        assert added == []

    def test_clean_contract_no_upgrade(self):
        pb = Playbook(nda_defaults=NDADefaults(mutual_required=True))
        result = {"classification": "GREEN", "nda_type": "mutual", "issues": []}
        out = _cross_validate_triage(result, pb, "a perfectly standard mutual nda")
        assert out["classification"] == "GREEN"
        assert out["issues"] == []


# ===========================================================================
# triage_nda pipeline
# ===========================================================================
class TestTriageNda:
    def test_green_happy_path(self, monkeypatch, tmp_db, benign_nda):
        _mock_call_llm(monkeypatch, _triage_json(classification="GREEN"))
        req = ReviewRequest(contract_text=benign_nda)
        out = triage_nda(req, db_path=tmp_db)
        assert out["classification"] == "GREEN"
        assert out["mode"] == "review-triage"
        assert out["playbook_version"]  # set from playbook
        assert out["audit_id"] is not None  # audit row written to tmp db

    def test_wires_playbook_context_into_system_prompt(self, monkeypatch, tmp_db, benign_nda):
        captured = _mock_call_llm(monkeypatch, _triage_json())
        triage_nda(ReviewRequest(contract_text=benign_nda), db_path=tmp_db)
        # The prompt actually carries the classification taxonomy + playbook block.
        sys_prompt = captured["system_prompt"]
        assert "GREEN" in sys_prompt and "RED" in sys_prompt
        assert "NDA Defaults:" in sys_prompt
        assert benign_nda in captured["question"]

    def test_playbook_upgrade_overrides_llm_green(self, monkeypatch, tmp_db, noncompete_nda):
        # LLM naively says GREEN; the playbook cross-check must catch the embedded
        # non-compete and force RED.
        _mock_call_llm(monkeypatch, _triage_json(classification="GREEN"))
        out = triage_nda(ReviewRequest(contract_text=noncompete_nda), db_path=tmp_db)
        assert out["classification"] == "RED"
        assert any("non-compete" in i["description"].lower() for i in out["issues"])

    def test_llm_exception_fails_closed_to_red(self, monkeypatch, tmp_db, benign_nda):
        def boom(**kwargs):
            raise RuntimeError("provider down")
        monkeypatch.setattr(cr, "call_llm", boom)
        out = triage_nda(ReviewRequest(contract_text=benign_nda), db_path=tmp_db)
        assert out["classification"] == "RED"
        assert "error" in out
        assert out["mode"] == "review-triage"

    def test_malformed_llm_json_surfaces_parse_error(self, monkeypatch, tmp_db, benign_nda):
        _mock_call_llm(monkeypatch, "totally not json")
        out = triage_nda(ReviewRequest(contract_text=benign_nda), db_path=tmp_db)
        assert "parse_error" in out
        assert out["raw_analysis"] == "totally not json"
        assert out["mode"] == "review-triage"

    def test_missing_playbook_path_falls_back_to_defaults(self, monkeypatch, tmp_db, benign_nda):
        _mock_call_llm(monkeypatch, _triage_json())
        req = ReviewRequest(contract_text=benign_nda, playbook_path="/no/such/playbook.md")
        out = triage_nda(req, db_path=tmp_db)
        # Built-in default playbook version is used; pipeline does not crash.
        assert out["playbook_version"] == "1.0.0"
        assert out["classification"] == "GREEN"

    def test_audit_failure_does_not_break_pipeline(self, monkeypatch, benign_nda):
        _mock_call_llm(monkeypatch, _triage_json())
        monkeypatch.setattr(cr, "_log_audit", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
        out = triage_nda(ReviewRequest(contract_text=benign_nda), db_path="/bad/db")
        assert out["audit_id"] is None
        assert out["classification"] == "GREEN"


# ===========================================================================
# review_contract pipeline
# ===========================================================================
class TestReviewContract:
    def test_green_happy_path(self, monkeypatch, tmp_db):
        clauses = [{
            "category": "Payment Terms",
            "classification": "GREEN",
            "contract_says": "Net 30",
            "playbook_position": "Net 30",
            "redline": None,
        }]
        _mock_call_llm(monkeypatch, _review_json(clauses, overall="GREEN"))
        out = review_contract(ReviewRequest(contract_text="a saas contract"), db_path=tmp_db)
        assert out["overall_risk"] == "GREEN"
        assert out["mode"] == "review-full"
        assert out["audit_id"] is not None

    def test_overall_risk_recomputed_from_clauses(self, monkeypatch, tmp_db):
        # LLM claims GREEN overall but a clause is RED — overall must be recomputed.
        clauses = [
            {"category": "Payment Terms", "classification": "GREEN", "contract_says": "Net 30"},
            {"category": "Indemnification", "classification": "RED", "contract_says": "uncapped"},
        ]
        _mock_call_llm(monkeypatch, _review_json(clauses, overall="GREEN"))
        out = review_contract(ReviewRequest(contract_text="c"), db_path=tmp_db)
        assert out["overall_risk"] == "RED"

    def test_escalation_trigger_upgrades_green_clause_to_yellow(self, monkeypatch, tmp_db, tmp_path):
        # Playbook rule with an escalation trigger the contract language matches;
        # an LLM-GREEN clause must be upgraded to YELLOW.
        pb_file = tmp_path / "legal.local.md"
        pb_file.write_text(
            "### Limitation of Liability\n"
            "- Standard position: Mutual cap at 12 months fees\n"
            "- Acceptable range: 6-24 months\n"
            "- Escalation trigger: uncapped\n",
            encoding="utf-8",
        )
        clauses = [{
            "category": "Limitation of Liability",
            "classification": "GREEN",
            "contract_says": "Liability is uncapped for data breaches",
            "deviation": "",
        }]
        _mock_call_llm(monkeypatch, _review_json(clauses, overall="GREEN"))
        req = ReviewRequest(contract_text="c", playbook_path=str(pb_file))
        out = review_contract(req, db_path=tmp_db)
        upgraded = out["clauses"][0]
        assert upgraded["classification"] == "YELLOW"
        assert "escalation trigger matched" in upgraded["deviation"].lower()
        # And overall risk reflects the upgraded clause.
        assert out["overall_risk"] == "YELLOW"

    def test_party_context_and_focus_areas_wired(self, monkeypatch, tmp_db):
        captured = _mock_call_llm(monkeypatch, _review_json([]))
        req = ReviewRequest(
            contract_text="c",
            party_side="vendor",
            focus_areas=["indemnity", "liability"],
        )
        review_contract(req, db_path=tmp_db)
        sys_prompt = captured["system_prompt"]
        assert "vendor" in sys_prompt
        assert "indemnity" in sys_prompt and "liability" in sys_prompt

    def test_llm_exception_fails_closed_to_red(self, monkeypatch, tmp_db):
        def boom(**kwargs):
            raise RuntimeError("provider down")
        monkeypatch.setattr(cr, "call_llm", boom)
        out = review_contract(ReviewRequest(contract_text="c"), db_path=tmp_db)
        assert out["overall_risk"] == "RED"
        assert "error" in out
        assert out["mode"] == "review-full"

    def test_malformed_llm_json_surfaces_parse_error(self, monkeypatch, tmp_db):
        _mock_call_llm(monkeypatch, "```\nnot json\n```")
        out = review_contract(ReviewRequest(contract_text="c"), db_path=tmp_db)
        assert "parse_error" in out
        assert out["mode"] == "review-full"
