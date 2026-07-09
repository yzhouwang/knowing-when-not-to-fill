"""
compliance_review.py — Contract review pipeline wrapping Anthropic Legal Plugin prompts.

Extracts the analytical content from triage-nda (40-checkpoint screening) and
review-contract (12-category clause analysis) into single-shot API calls.
Uses the same playbook and audit infrastructure as compliance_draft.py.

The prompts are derived from anthropics/knowledge-work-plugins/legal SKILL.md files,
restructured for programmatic use (interactive conversation steps stripped).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from contract_drafting.compliance_playbook import Playbook
from contract_drafting.compliance_draft import _log_audit
from contract_drafting.llm import call_llm

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# System prompts — derived from Legal Plugin SKILL.md, stripped of interactive steps
# ---------------------------------------------------------------------------

TRIAGE_SYSTEM_PROMPT = """\
You are a legal NDA screening assistant. You assist with legal workflows but do not
provide legal advice. All analysis should be reviewed by qualified legal professionals.

Evaluate the NDA against these 10 screening categories (~40 checkpoints):

1. AGREEMENT STRUCTURE: Type (mutual/unilateral), appropriate for context, standalone
2. DEFINITION OF CONFIDENTIAL INFORMATION: Reasonable scope, marking requirements, exclusions present, no problematic inclusions
3. OBLIGATIONS OF RECEIVING PARTY: Standard of care, use restriction, disclosure restriction, no onerous obligations
4. STANDARD CARVEOUTS (all must be present): Public knowledge, prior possession, independent development, third-party receipt, legal compulsion
5. PERMITTED DISCLOSURES: Employees, contractors/advisors, affiliates, legal/regulatory
6. TERM AND DURATION: Agreement term reasonable (1-3y standard), survival reasonable (2-5y), not perpetual
7. RETURN AND DESTRUCTION: Triggered on termination/request, reasonable scope, retention exception, certification not onerous
8. REMEDIES: Injunctive relief standard, no pre-determined damages, not one-sided
9. PROBLEMATIC PROVISIONS: No non-solicitation, no non-compete, no exclusivity, no standstill, no broad residuals, no IP assignment, no audit rights
10. GOVERNING LAW: Reasonable jurisdiction, consistent, no mandatory arbitration

PLAYBOOK RULES:
{playbook_context}

CLASSIFICATION CRITERIA:
- GREEN (standard approval): ALL checkpoints pass, no prohibited provisions, standard terms
- YELLOW (counsel review): Minor deviations (broader definition, longer term within market range, missing one carveout, narrow residuals, non-preferred jurisdiction)
- RED (significant issues): Unilateral when mutual required, missing critical carveouts, non-compete/non-solicitation embedded, exclusivity/standstill, 10+ year or perpetual term, overbroad definition, broad residuals, IP assignment, liquidated damages, audit rights, unfavorable jurisdiction

Output ONLY valid JSON matching this schema (no markdown, no explanation):
{{
  "classification": "GREEN|YELLOW|RED",
  "nda_type": "mutual|unilateral_disclosing|unilateral_receiving",
  "parties": {{"disclosing": "...", "receiving": "..."}},
  "term": "...",
  "governing_law": "...",
  "screening_results": [
    {{"category": "...", "status": "PASS|FLAG|FAIL", "notes": "..."}}
  ],
  "issues": [
    {{"description": "...", "severity": "YELLOW|RED", "risk": "...", "suggested_fix": "..."}}
  ],
  "recommendation": "..."
}}
"""

REVIEW_SYSTEM_PROMPT = """\
You are a contract review assistant. You assist with legal workflows but do not
provide legal advice. All analysis should be reviewed by qualified legal professionals.

Review the contract clause-by-clause against the playbook positions. Cover these 12 categories:

1. LIMITATION OF LIABILITY: Cap amount, carveouts, mutual vs unilateral, consequential damages
2. INDEMNIFICATION: Scope, mutual vs unilateral, cap, IP infringement, data breach
3. IP OWNERSHIP: Pre-existing IP, developed IP, work-for-hire, license grants, assignment
4. DATA PROTECTION: DPA requirement, processing terms, sub-processors, breach notification, cross-border
5. CONFIDENTIALITY: Scope, term, carveouts, return/destruction obligations
6. REPRESENTATIONS & WARRANTIES: Scope, disclaimers, survival period
7. TERM & TERMINATION: Duration, renewal, termination for convenience/cause, wind-down
8. GOVERNING LAW & DISPUTE RESOLUTION: Jurisdiction, venue, arbitration vs litigation
9. INSURANCE: Coverage requirements, minimums, evidence of coverage
10. ASSIGNMENT: Consent requirements, change of control, exceptions
11. FORCE MAJEURE: Scope, notification, termination rights
12. PAYMENT TERMS: Net terms, late fees, taxes, price escalation

CLASSIFICATION PER CLAUSE:
- GREEN: Aligns with or is better than standard position
- YELLOW: Outside standard but within negotiable range (generate redline + fallback)
- RED: Outside acceptable range, material risk (escalate, provide market-standard alternative)

PLAYBOOK RULES:
{playbook_context}

PARTY CONTEXT:
{party_context}

Output ONLY valid JSON matching this schema (no markdown, no explanation):
{{
  "contract_type": "...",
  "parties": {{"party_a": "...", "party_b": "..."}},
  "overall_risk": "GREEN|YELLOW|RED",
  "clauses": [
    {{
      "category": "...",
      "classification": "GREEN|YELLOW|RED",
      "contract_says": "...",
      "playbook_position": "...",
      "deviation": "...",
      "business_impact": "...",
      "redline": {{
        "current_language": "...",
        "proposed_language": "...",
        "rationale": "...",
        "priority": "must_have|should_have|nice_to_have",
        "fallback": "..."
      }}
    }}
  ],
  "top_issues": ["...", "...", "..."],
  "negotiation_strategy": "...",
  "negotiation_tiers": {{
    "tier_1_must_haves": ["..."],
    "tier_2_should_haves": ["..."],
    "tier_3_nice_to_haves": ["..."]
  }}
}}

For GREEN clauses, set redline to null. Only include redlines for YELLOW and RED.
"""


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class ReviewRequest:
    """Input parameters for contract review."""
    contract_text: str = ""
    file_path: str = ""
    sub_mode: str = "triage"          # "triage" or "full"
    doc_type: str = "nda"             # "nda", "saas", "services", "license", "other"
    party_side: str = ""              # "vendor", "customer", "licensor", "licensee"
    focus_areas: list[str] = field(default_factory=list)
    playbook_path: str = ""
    user: str = "system"


# ---------------------------------------------------------------------------
# Document reading
# ---------------------------------------------------------------------------

def _read_contract(request: ReviewRequest) -> str:
    """Read contract text from direct text, .txt, .docx, or .pdf."""
    if request.contract_text:
        return request.contract_text

    if not request.file_path:
        raise ValueError("No contract text or file path provided")

    path = Path(request.file_path)
    if not path.exists():
        raise FileNotFoundError(f"Contract file not found: {path}")

    suffix = path.suffix.lower()
    if suffix == ".txt":
        return path.read_text(encoding="utf-8")
    elif suffix == ".docx":
        from docx import Document
        doc = Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs)
    elif suffix == ".pdf":
        import pdfplumber
        with pdfplumber.open(str(path)) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    else:
        raise ValueError(f"Unsupported file format: {suffix}. Use .txt, .docx, or .pdf")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _build_playbook_context(playbook: Playbook) -> str:
    """Format playbook rules as context for the review prompt."""
    parts = []
    for rule in playbook.rules:
        parts.append(
            f"- {rule.clause_type}: standard={rule.standard_position}, "
            f"range={rule.acceptable_range}, escalation={rule.escalation_trigger}"
        )
    nda = playbook.nda_defaults
    parts.append(f"\nNDA Defaults: mutual={nda.mutual_required}, "
                 f"term={nda.term_years_standard}y standard, "
                 f"carveouts={', '.join(nda.carveouts)}, "
                 f"prohibited={', '.join(nda.prohibited_provisions)}")
    return "\n".join(parts)


def _parse_review_response(raw: str) -> dict:
    """Parse LLM JSON response with markdown-fence stripping and fallback."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError) as e:
        log.warning(f"JSON parse failed ({e}), returning raw analysis")
        return {"raw_analysis": raw, "parse_error": str(e)}


def _cross_validate_triage(llm_result: dict, playbook: Playbook, contract_text: str) -> dict:
    """Cross-validate LLM triage result against playbook rules.

    If the playbook detects issues the LLM missed, upgrade the classification.
    """
    if "parse_error" in llm_result:
        return llm_result

    text_lower = contract_text.lower()
    upgrades = []

    nda = playbook.nda_defaults

    # Check for prohibited provisions the LLM might have missed
    if nda.mutual_required and llm_result.get("nda_type") == "unilateral_receiving":
        if llm_result.get("classification") != "RED":
            upgrades.append({
                "description": "Unilateral NDA when mutual is required by playbook",
                "severity": "RED",
                "risk": "Imbalanced obligations",
                "suggested_fix": "Request mutual NDA or counterpropose with standard form",
            })

    for provision in nda.prohibited_provisions:
        prov_lower = provision.lower().replace("-", "").replace("_", "").replace(" ", "")
        # Check common variants
        variants = [prov_lower, provision.lower()]
        if "noncompete" in prov_lower or "non compete" in provision.lower():
            variants.extend(["non-compete", "noncompete", "covenant not to compete", "non compete"])
        if "nonsolicitation" in prov_lower or "non solicitation" in provision.lower():
            variants.extend(["non-solicitation", "nonsolicitation", "non solicitation"])
        if "residual" in prov_lower:
            variants.extend(["residuals clause", "residual knowledge", "residual information"])

        for variant in variants:
            if variant in text_lower:
                already_flagged = any(
                    variant in (i.get("description", "").lower())
                    for i in llm_result.get("issues", [])
                )
                if not already_flagged:
                    upgrades.append({
                        "description": f"Prohibited provision detected: {provision}",
                        "severity": "RED",
                        "risk": f"Playbook prohibits {provision} in NDAs",
                        "suggested_fix": f"Remove {provision} provision entirely",
                    })
                break

    if upgrades:
        llm_result.setdefault("issues", []).extend(upgrades)
        # Upgrade classification if needed
        severities = [i["severity"] for i in llm_result.get("issues", [])]
        if "RED" in severities:
            llm_result["classification"] = "RED"
        elif "YELLOW" in severities and llm_result.get("classification") == "GREEN":
            llm_result["classification"] = "YELLOW"

    return llm_result


# ---------------------------------------------------------------------------
# Pipeline functions
# ---------------------------------------------------------------------------

def triage_nda(
    request: ReviewRequest,
    *,
    provider: str = "anthropic",
    model: Optional[str] = None,
    db_path: str = "data/law_corpus.db",
) -> dict:
    """NDA triage pipeline — fast GREEN/YELLOW/RED screening.

    Pipeline: read contract -> load playbook -> call LLM with triage prompt
    -> parse JSON -> cross-validate against playbook -> audit log -> return.
    """
    # 1. Read contract
    contract_text = _read_contract(request)

    # 2. Load playbook
    playbook = Playbook.load(request.playbook_path or None)

    # 3. Build prompt and call LLM
    system_prompt = TRIAGE_SYSTEM_PROMPT.format(
        playbook_context=_build_playbook_context(playbook),
    )

    try:
        raw = call_llm(
            question=f"Triage this NDA:\n\n{contract_text}",
            context="Screen this NDA against all 10 categories and classify as GREEN/YELLOW/RED.",
            provider=provider,
            model=model,
            system_prompt=system_prompt,
            max_tokens=8192,
        )
        result = _parse_review_response(raw)
    except Exception as e:
        log.error(f"LLM triage failed: {e}")
        result = {"classification": "RED", "error": str(e),
                  "recommendation": "LLM analysis failed. Manual review required."}

    # 4. Cross-validate against playbook
    result = _cross_validate_triage(result, playbook, contract_text)

    # 5. Audit log
    try:
        audit_id = _log_audit(
            db_path,
            user=request.user,
            doc_type="review-triage",
            template_id=Path(request.file_path).name if request.file_path else "direct-text",
            playbook_ver=playbook.version,
            slot_values={"contract_length": len(contract_text), "sub_mode": "triage"},
            gate_result=result.get("classification", "RED"),
            violations=[{"description": i.get("description", "unknown"), "severity": i.get("severity", "RED")}
                        for i in result.get("issues", [])],
            notes=f"Triage via {provider}" + (f" model={model}" if model else ""),
        )
    except Exception as e:
        log.warning(f"Audit log write failed: {e}")
        audit_id = None

    # 6. Return
    result["mode"] = "review-triage"
    result["audit_id"] = audit_id
    result["playbook_version"] = playbook.version
    return result


def review_contract(
    request: ReviewRequest,
    *,
    provider: str = "anthropic",
    model: Optional[str] = None,
    db_path: str = "data/law_corpus.db",
) -> dict:
    """Full contract review — clause-by-clause analysis with redlines.

    Pipeline: read contract -> load playbook -> call LLM with review prompt
    -> parse JSON -> cross-validate clauses -> audit log -> return.
    """
    # 1. Read contract
    contract_text = _read_contract(request)

    # 2. Load playbook
    playbook = Playbook.load(request.playbook_path or None)

    # 3. Build context
    party_context = f"Your side: {request.party_side}" if request.party_side else "Side not specified"
    if request.focus_areas:
        party_context += f"\nFocus areas: {', '.join(request.focus_areas)}"

    system_prompt = REVIEW_SYSTEM_PROMPT.format(
        playbook_context=_build_playbook_context(playbook),
        party_context=party_context,
    )

    try:
        raw = call_llm(
            question=f"Review this contract clause-by-clause:\n\n{contract_text}",
            context="Analyze against the playbook. Flag deviations. Generate redlines for YELLOW and RED.",
            provider=provider,
            model=model,
            system_prompt=system_prompt,
            max_tokens=8192,
        )
        result = _parse_review_response(raw)
    except Exception as e:
        log.error(f"LLM review failed: {e}")
        result = {"overall_risk": "RED", "error": str(e),
                  "top_issues": ["LLM analysis failed. Manual review required."]}

    # 4. Cross-validate clauses against playbook rules
    for clause in result.get("clauses", []):
        rule = playbook.get_rule(clause.get("category", ""))
        if rule and rule.escalation_trigger:
            trigger_lower = rule.escalation_trigger.lower()
            contract_says = clause.get("contract_says", "").lower()
            # If escalation trigger keywords appear in the contract language
            # and LLM rated GREEN, upgrade to YELLOW
            if clause.get("classification") == "GREEN":
                trigger_words = [w.strip() for w in trigger_lower.split(",")]
                for tw in trigger_words:
                    if tw and tw in contract_says:
                        clause["classification"] = "YELLOW"
                        clause["deviation"] = (clause.get("deviation", "") +
                                               f" [Playbook escalation trigger matched: {tw}]")
                        break

    # Recompute overall risk from clause classifications
    if result.get("clauses"):
        clause_risks = [c.get("classification", "GREEN") for c in result["clauses"]]
        if "RED" in clause_risks:
            result["overall_risk"] = "RED"
        elif "YELLOW" in clause_risks:
            result["overall_risk"] = "YELLOW"
        else:
            result["overall_risk"] = "GREEN"

    # 5. Audit log
    try:
        audit_id = _log_audit(
            db_path,
            user=request.user,
            doc_type="review-full",
            template_id=Path(request.file_path).name if request.file_path else "direct-text",
            playbook_ver=playbook.version,
            slot_values={
                "contract_length": len(contract_text),
                "sub_mode": "full",
                "party_side": request.party_side,
                "doc_type": request.doc_type,
            },
            gate_result=result.get("overall_risk", "RED"),
            violations=[{"category": c.get("category", "unknown"), "classification": c.get("classification", "RED")}
                        for c in result.get("clauses", [])
                        if c.get("classification") in ("YELLOW", "RED")],
            notes=f"Full review via {provider}" + (f" model={model}" if model else ""),
        )
    except Exception as e:
        log.warning(f"Audit log write failed: {e}")
        audit_id = None

    # 6. Return
    result["mode"] = "review-full"
    result["audit_id"] = audit_id
    result["playbook_version"] = playbook.version
    return result
