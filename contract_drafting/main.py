"""
main.py — Router/entrypoint for contract drafting and review.

Modes: draft, review, review-triage, review-full.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Optional

log = logging.getLogger(__name__)


def _run_draft_mode(
    *,
    doc_type: str = "nda",
    party_a: str = "",
    party_b: str = "",
    jurisdiction: Optional[str] = None,
    term_months: int = 24,
    effective_date: str = "",
    has_non_compete: bool = False,
    has_non_solicitation: bool = False,
    has_residuals_clause: bool = False,
    playbook_path: str = "",
    template_path: str = "data/templates/nda_mutual.docx",
    output_path: str = "",
    provider: str = "anthropic",
    model: Optional[str] = None,
    db_path: str = "data/contract_drafting.db",
    user: str = "system",
    engine: str = "cicero",
) -> dict:
    """Run contract drafting pipeline."""
    from contract_drafting.compliance_draft import draft_contract, DraftRequest

    request = DraftRequest(
        doc_type=doc_type,
        disclosing_party=party_a,
        receiving_party=party_b,
        governing_law=jurisdiction or "Washington",
        term_months=term_months,
        effective_date=effective_date,
        has_non_compete=has_non_compete,
        has_non_solicitation=has_non_solicitation,
        has_residuals_clause=has_residuals_clause,
        playbook_path=playbook_path,
        template_path=template_path,
        output_path=output_path,
        user=user,
    )

    return draft_contract(request, engine=engine, provider=provider, model=model, db_path=db_path)


def _run_review_mode(
    *,
    contract_text: str = "",
    file_path: str = "",
    sub_mode: str = "triage",
    doc_type: str = "nda",
    party_side: str = "",
    focus_areas: Optional[list] = None,
    playbook_path: str = "",
    provider: str = "anthropic",
    model: Optional[str] = None,
    db_path: str = "data/contract_drafting.db",
    user: str = "system",
) -> dict:
    """Run contract review pipeline."""
    from contract_drafting.compliance_review import (
        triage_nda,
        review_contract,
        ReviewRequest,
    )

    request = ReviewRequest(
        contract_text=contract_text,
        file_path=file_path,
        sub_mode=sub_mode,
        doc_type=doc_type,
        party_side=party_side,
        focus_areas=focus_areas or [],
        playbook_path=playbook_path,
        user=user,
    )

    if sub_mode == "triage":
        return triage_nda(request, provider=provider, model=model, db_path=db_path)
    else:
        return review_contract(request, provider=provider, model=model, db_path=db_path)


def run(
    *,
    mode: str = "draft",
    provider: str = "anthropic",
    model: Optional[str] = None,
    db_path: str = "data/contract_drafting.db",
    **kwargs,
) -> dict:
    """Main entry point for contract-drafting skill."""

    if mode == "draft":
        return _run_draft_mode(
            doc_type=kwargs.get("doc_type", "nda"),
            party_a=kwargs.get("party_a", ""),
            party_b=kwargs.get("party_b", ""),
            jurisdiction=kwargs.get("jurisdiction"),
            term_months=kwargs.get("term_months", 24),
            effective_date=kwargs.get("effective_date", ""),
            has_non_compete=kwargs.get("has_non_compete", False),
            has_non_solicitation=kwargs.get("has_non_solicitation", False),
            has_residuals_clause=kwargs.get("has_residuals_clause", False),
            playbook_path=kwargs.get("playbook_path", ""),
            template_path=kwargs.get("template_path", "data/templates/nda_mutual.docx"),
            output_path=kwargs.get("output_path", ""),
            provider=provider,
            model=model,
            db_path=db_path,
            user=kwargs.get("user", "system"),
            engine=kwargs.get("engine", "cicero"),
        )

    if mode in ("review", "review-triage", "review-full"):
        sub = "full" if mode == "review-full" else kwargs.get("review_sub_mode", "triage")
        return _run_review_mode(
            contract_text=kwargs.get("contract_text", ""),
            file_path=kwargs.get("file_path", ""),
            sub_mode=sub,
            doc_type=kwargs.get("doc_type", "nda"),
            party_side=kwargs.get("party_side", ""),
            focus_areas=kwargs.get("focus_areas"),
            playbook_path=kwargs.get("playbook_path", ""),
            provider=provider,
            model=model,
            db_path=db_path,
            user=kwargs.get("user", "system"),
        )

    return {"error": f"Unknown mode: {mode}"}


def main() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Contract drafting and review")
    parser.add_argument("--mode", choices=["draft", "review", "review-triage", "review-full"], default="draft")
    # Draft flags
    parser.add_argument("--doc-type", choices=["nda"], default="nda")
    parser.add_argument("--party-a", default="", help="Disclosing party name")
    parser.add_argument("--party-b", default="", help="Receiving party name")
    parser.add_argument("--jurisdiction", default=None)
    parser.add_argument("--term-months", type=int, default=24)
    parser.add_argument("--playbook", default="", help="Path to playbook file")
    parser.add_argument("--template", default="data/templates/nda_mutual.docx")
    parser.add_argument("--output-path", default="", help="Output .docx path")
    parser.add_argument("--effective-date", default="", help="Effective date (YYYY-MM-DD)")
    parser.add_argument("--has-non-compete", action="store_true", help="Include non-compete clause")
    parser.add_argument("--has-non-solicitation", action="store_true", help="Include non-solicitation clause")
    parser.add_argument("--has-residuals-clause", action="store_true", help="Include residuals clause")
    parser.add_argument("--engine", choices=["cicero", "llm"], default="cicero")
    # Review flags
    parser.add_argument("--contract-text", default="", help="Contract text for review")
    parser.add_argument("--file-path", default="", help="Path to contract file")
    parser.add_argument("--review-sub-mode", choices=["triage", "full"], default="triage")
    parser.add_argument("--party-side", default="", help="Your side: vendor, customer, etc.")
    parser.add_argument("--focus-areas", nargs="*", default=[])
    # Common flags
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--provider", choices=["openai", "anthropic"], default="anthropic")
    parser.add_argument("--model", default=None)
    parser.add_argument("--db-path", default="data/contract_drafting.db")
    args = parser.parse_args()

    result = run(
        mode=args.mode,
        provider=args.provider,
        model=args.model,
        db_path=args.db_path,
        doc_type=args.doc_type,
        party_a=args.party_a,
        party_b=args.party_b,
        jurisdiction=args.jurisdiction,
        term_months=args.term_months,
        effective_date=args.effective_date,
        has_non_compete=args.has_non_compete,
        has_non_solicitation=args.has_non_solicitation,
        has_residuals_clause=args.has_residuals_clause,
        playbook_path=args.playbook,
        template_path=args.template,
        output_path=args.output_path,
        engine=args.engine,
        contract_text=args.contract_text,
        file_path=args.file_path,
        review_sub_mode=args.review_sub_mode,
        party_side=args.party_side,
        focus_areas=args.focus_areas,
    )

    if args.json:
        # C2: result['rendered_text'] carries the FULL contract body (in-process use
        # only: demo clause extraction / previews). The CLI JSON surface must not leak
        # it -- render_sha256 remains the verifiable link to the rendered bytes.
        print(json.dumps({k: v for k, v in result.items() if k != "rendered_text"},
                         indent=2, default=str))
    else:
        # Typed-abstention ESCALATED results keep an 'error' key (back-compat for API
        # callers/tests), but they are NOT generic errors: the CLI must surface the
        # ESCALATED status, the abstained field(s) with their raw asks, the audit_id,
        # and the audit-lookup hint -- never a bare "Error: ..." that hides the
        # escalation record. Handled BEFORE the generic error branch (Codex P2).
        if result.get("gate_result") == "ESCALATED" and "error" in result:
            print(f"\nDraft: {result.get('doc_type', 'unknown').upper()} | Gate: ESCALATED")
            print(result.get("message") or result["error"])
            _raw_keys = {
                "governingLaw": "governing_law_raw",
                "disclosingEntityType": "disclosing_entity_type_raw",
                "receivingEntityType": "receiving_entity_type_raw",
            }
            abstained = result.get("abstained_fields") or []
            if abstained:
                print("\nAbstained fields (a human must supply a supported value):")
                for f in abstained:
                    raw = result.get(_raw_keys.get(f, ""), None)
                    print(f"  - {f}: requested {raw!r}" if raw
                          else f"  - {f}: requested value unspecified")
            if result.get("audit_id") is not None:
                print(f"\nAudit ID: {result['audit_id']} -- run "
                      f"`audit --doc {result['audit_id']}` to review the escalation record.")
            # Non-success exit, same convention as BLOCKED drafts: no signable
            # contract was rendered (fail-closed).
            sys.exit(1)

        if "error" in result:
            print(f"Error: {result['error']}")
            sys.exit(1)

        if result.get("mode") == "draft":
            gate = result.get("gate_result", "?")
            print(f"\nDraft: {result.get('doc_type', 'unknown').upper()} | Gate: {gate}")
            print(result.get("message", ""))
            if result.get("violations"):
                print("\nViolations:")
                for v in result["violations"]:
                    print(f"  [{v['severity'].upper()}] {v['clause_type']}: {v['description']}")
            # T6: non-fatal engine warnings (e.g. a dispute forum captured for human
            # review but deliberately NOT rendered into the clause) must reach the
            # operator, not just the audit row.
            if result.get("warnings"):
                print("\nWarnings:")
                for w in result["warnings"]:
                    print(f"  [WARNING] {w}")
            # Surface the not-legal-advice footer in the default (non-JSON) output too,
            # so a PASS draft visibly states "conformance, not legal correctness" -- the
            # mitigation the Ethics section relies on ("the UI states this"). Gate on PASS:
            # the disclaimer text is PASS-specific ("PASS certifies..."), so printing it
            # under an ESCALATED/BLOCKED gate would contradict the status and mislead.
            if gate == "PASS" and result.get("disclaimer"):
                print(f"\n{result['disclaimer']}")
            sys.exit(0 if gate != "BLOCKED" else 1)

        if result.get("mode") in ("review-triage", "review-full"):
            classification = result.get("classification") or result.get("overall_risk", "?")
            sub = "TRIAGE" if result.get("mode") == "review-triage" else "FULL REVIEW"
            print(f"\nReview: {sub} | Classification: {classification}")
            if result.get("top_issues"):
                print("\nTop Issues:")
                for issue in result["top_issues"][:5]:
                    print(f"  - {issue}")
            sys.exit(0 if classification in ("GREEN", "YELLOW") else 1)


if __name__ == "__main__":
    main()
