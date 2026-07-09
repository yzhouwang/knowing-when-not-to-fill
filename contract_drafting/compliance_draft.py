"""
compliance_draft.py — Contract drafting pipeline with playbook governance.

Two engines:
- **cicero** (default): Deterministic CiceroMark template fill via cicero_bridge.
  No LLM in the draft loop — same inputs always produce the same contract.
- **llm** (legacy fallback): Claude/GPT field generation + docxtpl assembly.

Both engines validate against the organizational playbook before producing output.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field, asdict, replace
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

from contract_drafting.compliance_playbook import (
    Playbook,
    PlaybookValidationResult,
)
from contract_drafting.demo_mars_beat import LEGAL_DISCLAIMER
from contract_drafting.llm import call_llm

log = logging.getLogger(__name__)

DRAFT_SYSTEM_PROMPT = """\
You are a legal document drafting assistant. Given deal parameters and a playbook
of organizational rules, generate the specific field values needed to fill a
contract template.

RULES:
1. Generate values that comply with the provided playbook rules.
2. Use standard legal language appropriate for the document type.
3. All dates must be in YYYY-MM-DD format.
4. Term must be specified in months as an integer.
5. Governing law must be a US state name (e.g., "Washington").
6. Do not add provisions not requested (no non-compete, no non-solicitation
   unless explicitly requested).
7. Output ONLY valid JSON matching the requested schema. No markdown, no explanation.

PLAYBOOK RULES:
{playbook_context}

DEAL PARAMETERS:
{deal_params}

OUTPUT SCHEMA:
{schema}
"""


@dataclass
class DraftRequest:
    """Input parameters for contract drafting."""
    doc_type: str = "nda"
    disclosing_party: str = ""
    receiving_party: str = ""
    disclosing_entity_type: str = "corporation"
    receiving_entity_type: str = "corporation"
    disclosing_jurisdiction: str = "Washington"
    receiving_jurisdiction: str = ""
    purpose: str = "exploring a potential business relationship"
    term_months: int = 24
    notice_days: int = 30
    survival_years: int = 3
    governing_law: str = "Washington"
    effective_date: str = ""  # defaults to today
    # Signatories
    disclosing_signatory: str = ""
    disclosing_title: str = ""
    receiving_signatory: str = ""
    receiving_title: str = ""
    # Optional overrides
    mutual: bool = True
    has_non_compete: bool = False
    has_non_solicitation: bool = False
    has_residuals_clause: bool = False
    # Abstain-hatch raw captures (PR2 sentinels): the verbatim asked value that
    # accompanies an abstain sentinel (governingLaw=OTHER / entityType=OTHER_ENTITY).
    # Threaded into the ESCALATED audit row so the human reviewer sees the actual ask.
    governing_law_raw: str = ""
    disclosing_entity_type_raw: str = ""
    receiving_entity_type_raw: str = ""
    # Dispute forum: CAPTURED for human review, never auto-rendered into the clause
    # (rendering a forum would silently flip a material term -- court vs arbitration).
    # A supplied forum surfaces as a render warning (result['warnings'] + audit notes,
    # T6) so the capture is never a silent drop. dispute_forum_raw carries the verbatim
    # ask accompanying the OTHER_FORUM abstain sentinel.
    dispute_forum: str = ""
    dispute_forum_raw: str = ""
    # Drafting config
    user: str = "system"
    template_path: str = "data/templates/nda_mutual.docx"
    playbook_path: str = ""
    output_path: str = ""

    def to_deal_params(self) -> str:
        """Format as deal parameters string for LLM prompt."""
        return (
            f"Document type: {self.doc_type.upper()}\n"
            f"Disclosing party: {self.disclosing_party}\n"
            f"Receiving party: {self.receiving_party}\n"
            f"Purpose: {self.purpose}\n"
            f"Term: {self.term_months} months\n"
            f"Notice period: {self.notice_days} days\n"
            f"Survival: {self.survival_years} years\n"
            f"Governing law: {self.governing_law}\n"
            f"Mutual: {self.mutual}\n"
        )


# NDA template fields that the LLM can generate or refine
NDA_FIELD_SCHEMA = {
    "type": "object",
    "properties": {
        "purpose": {"type": "string", "description": "Refined purpose statement for the NDA"},
        "term_months": {"type": "integer", "description": "Term in months"},
        "notice_days": {"type": "integer", "description": "Notice period in days"},
        "survival_years": {"type": "integer", "description": "Confidentiality survival in years"},
        "governing_law": {"type": "string", "description": "US state name for governing law"},
    },
    "required": ["purpose", "term_months", "notice_days", "survival_years", "governing_law"],
}


def _build_playbook_context(playbook: Playbook) -> str:
    """Format playbook rules as context for the LLM prompt."""
    parts = []
    for rule in playbook.rules:
        parts.append(
            f"- {rule.clause_type}: standard={rule.standard_position}, "
            f"range={rule.acceptable_range}, escalation={rule.escalation_trigger}"
        )
    nda = playbook.nda_defaults
    parts.append(f"\nNDA Defaults: mutual={nda.mutual_required}, "
                 f"term={nda.term_years_standard}y standard, "
                 f"carveouts={', '.join(nda.carveouts)}")
    return "\n".join(parts)


def _call_llm_for_fields(
    request: DraftRequest,
    playbook: Playbook,
    *,
    provider: str = "anthropic",
    model: Optional[str] = None,
) -> dict:
    """Call LLM to generate/refine template field values.

    Returns a dict of field values matching NDA_FIELD_SCHEMA.
    Falls back to request values if LLM call fails.
    """
    prompt = DRAFT_SYSTEM_PROMPT.format(
        playbook_context=_build_playbook_context(playbook),
        deal_params=request.to_deal_params(),
        schema=json.dumps(NDA_FIELD_SCHEMA, indent=2),
    )

    try:
        raw = call_llm(
            question=f"Generate NDA field values for {request.disclosing_party} and {request.receiving_party}",
            context=prompt,
            provider=provider,
            model=model,
            system_prompt="You are a contract field generator. Output ONLY valid JSON. No markdown fences, no explanation.",
        )
        # Strip markdown fences if present
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

        return json.loads(cleaned)
    except Exception as e:
        log.warning(f"LLM field generation failed ({e}), using request defaults")
        return {
            "purpose": request.purpose,
            "term_months": request.term_months,
            "notice_days": request.notice_days,
            "survival_years": request.survival_years,
            "governing_law": request.governing_law,
        }


def _assemble_docx(template_path: str, fields: dict, output_path: str) -> str:
    """Fill the docx template with field values using docxtpl.

    Returns the path to the generated file.
    """
    from docxtpl import DocxTemplate

    tpl = DocxTemplate(template_path)
    tpl.render(fields)

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tpl.save(str(out))
    return str(out)


def _init_audit_db(db_path: str) -> sqlite3.Connection:
    """Create audit_log table if it doesn't exist. Migrates schema if needed.

    timeout=10 is sqlite's busy timeout: a concurrently-locked DB waits up to 10s
    instead of failing instantly with 'database is locked' (RT1)."""
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp     TEXT NOT NULL,
            user          TEXT NOT NULL,
            doc_type      TEXT NOT NULL,
            template_id   TEXT NOT NULL,
            playbook_ver  TEXT NOT NULL,
            slot_values   TEXT NOT NULL,
            gate_result   TEXT NOT NULL,
            violations    TEXT,
            output_path   TEXT,
            notes         TEXT,
            model_version TEXT,
            schema_hash   TEXT
        )
    """)
    # Migrate existing tables: add columns if missing
    for col in ("model_version", "schema_hash"):
        try:
            conn.execute(f"ALTER TABLE audit_log ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()
    return conn


def _log_audit(
    db_path: str,
    *,
    user: str,
    doc_type: str,
    template_id: str,
    playbook_ver: str,
    slot_values: dict,
    gate_result: str,
    violations: list[dict] | None = None,
    output_path: str | None = None,
    notes: str | None = None,
    model_version: str | None = None,
    schema_hash: str | None = None,
) -> int:
    """Insert an audit log entry. Returns the row ID."""
    conn = _init_audit_db(db_path)
    try:
        cursor = conn.execute(
            """INSERT INTO audit_log
               (timestamp, user, doc_type, template_id, playbook_ver,
                slot_values, gate_result, violations, output_path, notes,
                model_version, schema_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now().isoformat(),
                user,
                doc_type,
                template_id,
                playbook_ver,
                json.dumps(slot_values),
                gate_result,
                json.dumps(violations) if violations else None,
                output_path,
                notes,
                model_version,
                schema_hash,
            ),
        )
        conn.commit()
        return cursor.lastrowid or 0
    finally:
        conn.close()


def draft_contract(
    request: DraftRequest,
    *,
    engine: str = "cicero",
    provider: str = "anthropic",
    model: Optional[str] = None,
    db_path: str = "data/contract_drafting.db",
) -> dict:
    """Main drafting pipeline.

    Engines:
    - cicero: playbook validate -> CiceroMark template fill -> markdown-to-docx -> audit
    - llm:    LLM field generation -> merge -> playbook validate -> docxtpl -> audit

    Returns: {doc_type, gate_result, output_path, violations, audit_id, ...}
    """
    if engine == "cicero":
        return _draft_cicero(request, db_path=db_path)
    return _draft_llm(request, provider=provider, model=model, db_path=db_path)


# C3: audit notes are " | "-joined free text. User-controlled segments (a captured
# disputeForum / disputeForumRaw) must not be able to forge entry boundaries or smuggle
# control characters into the notes column.
_NOTE_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _sanitize_note_segment(text: str) -> str:
    """Strip control chars and neutralize the literal ' | ' notes separator inside a
    single user-influenced note segment, so a crafted value (e.g. disputeForumRaw=
    'KCAB | gate_result=PASS') cannot spoof an extra notes entry."""
    text = _NOTE_CTRL_RE.sub(" ", str(text))
    while " | " in text:
        text = text.replace(" | ", " / ")
    return text


def _escalation_sha256(record: dict) -> str:
    """sha256 over the CANONICAL (sorted-keys, compact) JSON form of an escalation
    record, so the audit row carries a verifiable digest of exactly what was stored."""
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _abstention_message(field: str, raw, who: str = "") -> str:
    """The per-field fail-closed handoff message for a typed abstention sentinel."""
    if field == "governingLaw":
        return (f"governing law abstained (OTHER): the requested law "
                f"{(raw or 'unspecified')!r} is not a supported jurisdiction -- a human must "
                f"supply a supported governing law before this contract can be rendered.")
    return (f"{who} entity type abstained (OTHER_ENTITY): the requested form "
            f"{(raw or 'unspecified')!r} is not a representable entity type -- a human must "
            f"supply a supported entity form before this contract can be rendered.")


def _abstention_escalation(abstentions, doc_type, engine, *, db_path, user="system",
                           template_id=None):
    """Fail-closed ESCALATED result for typed abstention sentinels (PR2 hatch).

    An abstention is NOT a playbook violation: the model honestly declined to fill an
    un-representable value instead of silently substituting. It therefore routes to
    ESCALATED (human review), never BLOCKED -- but it still FAILS CLOSED: no signable
    contract is rendered (output_path=None), and an audit row is ALWAYS written carrying
    status=ESCALATED, the abstained field(s), the raw captured ask(s), and a sha256 over
    the canonical escalation record. Genuine playbook violations remain BLOCKED.

    abstentions: list of {"field", "sentinel", "raw"[, "who"]} dicts.
    """
    errors = [_abstention_message(a["field"], a.get("raw"), a.get("who", ""))
              for a in abstentions]
    record = {
        "status": "ESCALATED",
        "reason": "typed-abstention",
        "doc_type": doc_type,
        "engine": engine,
        "abstentions": [
            {"field": a["field"], "sentinel": a["sentinel"], "raw": a.get("raw")}
            for a in abstentions
        ],
    }
    sha = _escalation_sha256(record)
    # C12: an audit-write failure here must return a clean fail-closed ERROR result,
    # never a raw traceback. No artifact exists at this point (the abstention pre-empts
    # any render), so there is nothing to withdraw -- but without an audit row the
    # escalation is unrecorded, so the result is ERROR, not ESCALATED.
    try:
        audit_id = _log_audit(
            db_path,
            user=user,
            doc_type=doc_type,
            template_id=template_id or f"{engine}/abstention",
            playbook_ver="n/a (abstention pre-gate)",
            slot_values={**record, "escalation_sha256": sha},
            gate_result="ESCALATED",
            violations=None,
            output_path=None,  # fail-closed: no signable contract was rendered
            notes=("Typed abstention (fail-closed): no contract rendered; a human must "
                   "supply a supported value. escalation_sha256=" + sha),
        )
    except Exception as e:  # noqa: BLE001 -- any audit failure fails closed, cleanly
        log.error(f"Audit write failed for typed-abstention escalation ({e})")
        return {
            "error": (f"Audit write failed ({e}) while recording the typed-abstention "
                      f"escalation; failing closed -- no contract was rendered and no "
                      f"audit row exists."),
            "doc_type": doc_type,
            "mode": "draft",
            "engine": engine,
            "gate_result": "ERROR",
            "abstained": True,
            "abstained_fields": [a["field"] for a in abstentions],
            "output_path": None,
        }
    result = {
        "error": "; ".join(errors),
        "doc_type": doc_type,
        "mode": "draft",
        "engine": engine,
        "gate_result": "ESCALATED",
        "abstained": True,
        "abstained_fields": [a["field"] for a in abstentions],
        "output_path": None,
        "audit_id": audit_id,
        "escalation_sha256": sha,
        "message": (
            f"Draft escalated for human review (typed abstention on "
            f"{', '.join(a['field'] for a in abstentions)}): no contract rendered. "
            f"Run `audit --doc {audit_id}` to review."
        ),
    }
    # Back-compat raw-value keys (parity with the pre-escalation result shape).
    # M8: per-field keys so a double entity abstention never silently overwrites
    # one raw ask with the other; the legacy aggregate key 'entity_type_raw' keeps
    # the first NON-EMPTY raw among the abstained entities (C12: a leading raw-less
    # entity abstention must not pin None when a later one carries the actual ask).
    _entity_raws: list = []
    for a in abstentions:
        if a["field"] == "governingLaw":
            result["governing_law_raw"] = a.get("raw")
        else:
            per_field_key = ("disclosing_entity_type_raw"
                             if a["field"] == "disclosingEntityType"
                             else "receiving_entity_type_raw")
            result[per_field_key] = a.get("raw")
            _entity_raws.append(a.get("raw"))
    if _entity_raws:
        result["entity_type_raw"] = next((r for r in _entity_raws if r), None)
    return result


def _withdraw_draft(generated_path: str, doc_type: str, engine: str, exc: Exception) -> dict:
    """RT1 fail-closed: the audit write failed AFTER the signable docx was written.
    A signable draft must not exist without an audit row -- delete the just-written
    artifact and return a clean ERROR result instead of propagating a raw traceback."""
    log.error(f"Audit write failed after rendering ({exc}); withdrawing {generated_path}")
    try:
        os.remove(generated_path)
    except OSError as rm_exc:
        log.error(f"Withdrawal removal failed for {generated_path}: {rm_exc}")
    # C7: never claim a withdrawal that did not happen. If the artifact survived the
    # remove attempt, the error must say so and name the orphan path explicitly.
    if os.path.exists(generated_path):
        return {
            "error": (f"Audit write failed ({exc}); withdrawal of the rendered draft "
                      f"FAILED -- an ORPHAN signable artifact remains at "
                      f"{generated_path} with NO audit row. Delete it manually "
                      f"(fail-closed: no signable contract may exist without an "
                      f"audit row)."),
            "doc_type": doc_type,
            "mode": "draft",
            "engine": engine,
            "gate_result": "ERROR",
            "output_path": None,
            "orphan_path": generated_path,
        }
    return {
        "error": (f"Audit write failed ({exc}); the rendered draft was withdrawn "
                  f"(fail-closed: no signable contract may exist without an audit row)."),
        "doc_type": doc_type,
        "mode": "draft",
        "engine": engine,
        "gate_result": "ERROR",
        "output_path": None,
    }


def _draft_cicero(
    request: DraftRequest,
    *,
    db_path: str = "data/contract_drafting.db",
) -> dict:
    """Cicero engine: deterministic template fill, no LLM."""
    from contract_drafting import cicero_bridge
    from contract_drafting.cicero_bridge import draft as cicero_draft, markdown_to_docx
    from contract_drafting.schema_validator import validate_template_data
    from contract_drafting.template_registry import get_registry

    # 1. Resolve template via registry
    registry = get_registry()
    template_info = registry.get_for_doc_type(request.doc_type)
    if template_info is None:
        return {
            "error": f"No template registered for document type: {request.doc_type}",
            "doc_type": request.doc_type,
            "mode": "draft",
            "engine": "cicero",
            "gate_result": "BLOCKED",
        }
    template_name = template_info.name

    # Normalize governingLaw display -> identifier for the deterministic path so
    # callers passing a human-readable name ("Republic of Singapore") validate
    # against the identifier enum, and single-word names ("Washington") map to
    # themselves. Unknown values ("Mars", "Wahington") pass through unchanged and
    # are rejected by the schema validator with its nicer message. We mutate the
    # request so all downstream uses (render via _build_data, audit) start from
    # the identifier; _build_data then maps identifier -> display for rendering.
    # (LLM path / _draft_llm is intentionally untouched in this lane.)
    from contract_drafting import jurisdiction_map

    request.governing_law = jurisdiction_map.to_identifier(
        request.governing_law, template_name=template_name
    )
    # Same display->identifier normalization for the typed entity-type fields, so a
    # caller passing "limited liability company" validates against the EntityType enum.
    request.disclosing_entity_type = jurisdiction_map.to_identifier_enum(
        "EntityType", request.disclosing_entity_type, template_name=template_name
    )
    request.receiving_entity_type = jurisdiction_map.to_identifier_enum(
        "EntityType", request.receiving_entity_type, template_name=template_name
    )
    # C1: same display->identifier normalization for a supplied dispute forum, so a
    # caller passing the display form ("London Court of International Arbitration
    # (LCIA)") validates against the DisputeForum enum. Unknown values ("KCAB") pass
    # through unchanged and are rejected by the schema validator below.
    if request.dispute_forum:
        request.dispute_forum = jurisdiction_map.to_identifier_enum(
            "DisputeForum", request.dispute_forum, template_name=template_name
        )

    # 2. Validate input against Concerto-generated JSON Schema.
    #
    # _build_data maps governingLaw + entityType to display names for rendering and is
    # fail-closed on unknown values. Here we are validating UNVALIDATED input, so an
    # invalid value (e.g. "Mars", "Anstalt") must not crash _build_data before the
    # validator can reject it with a friendly message. Build the data with known-safe
    # placeholders, then validate against the actual (possibly-invalid) identifiers so
    # the schema is the gatekeeper.
    _draft_request_for_data = replace(
        request, governing_law="Washington",
        disclosing_entity_type="corporation", receiving_entity_type="corporation",
    )
    validation_data = {
        **cicero_bridge._build_data(_draft_request_for_data, template_name=template_name),
        "governingLaw": request.governing_law,
        "disclosingEntityType": request.disclosing_entity_type,
        "receivingEntityType": request.receiving_entity_type,
    }
    # C1: a supplied dispute forum is captured-not-rendered (T6), but it must pass the
    # SAME schema gate as every other typed field -- an out-of-enum string ("KCAB")
    # fails closed here instead of slipping into a PASS draft's capture channel.
    if request.dispute_forum:
        validation_data["disputeForum"] = request.dispute_forum
        if request.dispute_forum_raw:
            validation_data["disputeForumRaw"] = request.dispute_forum_raw
    from contract_drafting.schema_validator import validate_semantics
    schema_errors = validate_template_data(validation_data, template_name=template_name)
    schema_errors += validate_semantics(validation_data, template_name=template_name)
    if schema_errors:
        # C8: schema-invalid input is as auditable as every other failure path --
        # write the fail-fast BLOCKED row (notes carry the errors; no output_path).
        audit_id = _log_audit(
            db_path,
            user=request.user,
            doc_type=request.doc_type,
            template_id=f"cicero/{template_name}",
            playbook_ver="n/a (schema pre-gate)",
            slot_values=validation_data,
            gate_result="BLOCKED",
            notes="Blocked at schema validation (fail-fast): " + "; ".join(schema_errors),
        )
        return {
            "error": "; ".join(schema_errors),
            "doc_type": request.doc_type,
            "mode": "draft",
            "engine": "cicero",
            "gate_result": "BLOCKED",
            "output_path": None,
            "audit_id": audit_id,
        }

    # 1b/1c. Abstain hatch (PR2): a typed abstain sentinel (governingLaw=OTHER,
    # entityType=OTHER_ENTITY) means the requested value is not representable. The
    # sentinels validate fine against the schema, but they must NEVER render -- fail
    # closed (no contract), route to ESCALATED (human review, not a playbook violation),
    # and ALWAYS write an audit row carrying the raw captured ask(s).
    _abstentions = []
    if validation_data.get("governingLaw") == "OTHER":
        _abstentions.append({
            "field": "governingLaw", "sentinel": "OTHER",
            "raw": request.governing_law_raw or validation_data.get("governingLawRaw"),
        })
    for _ef, _raw, _who in (
        ("disclosingEntityType", request.disclosing_entity_type_raw, "disclosing"),
        ("receivingEntityType", request.receiving_entity_type_raw, "receiving"),
    ):
        if validation_data.get(_ef) == "OTHER_ENTITY":
            _abstentions.append({"field": _ef, "sentinel": "OTHER_ENTITY",
                                 "raw": _raw or validation_data.get(f"{_ef}Raw"),
                                 "who": _who})
    if _abstentions:
        return _abstention_escalation(_abstentions, request.doc_type, "cicero",
                                      db_path=db_path, user=request.user,
                                      template_id=f"cicero/{template_name}")

    # 2. Load playbook
    playbook = Playbook.load(request.playbook_path or None)

    # 3. Validate BEFORE drafting (fail-fast)
    validation_fields = {
        "term_months": request.term_months,
        "mutual": request.mutual,
        "governing_law": request.governing_law,
        "has_non_compete": request.has_non_compete,
        "has_non_solicitation": request.has_non_solicitation,
        "has_residuals_clause": request.has_residuals_clause,
    }
    validation: PlaybookValidationResult = playbook.validate_nda(validation_fields)

    violations_dicts = [
        {"clause_type": v.clause_type, "rule_field": v.rule_field,
         "description": v.description, "severity": v.severity}
        for v in validation.violations
    ]

    # Block before drafting — don't waste cycles on a contract that can't ship
    if validation.gate_result == "BLOCKED":
        # P1.2c: the ACTUAL registry-resolved template id, not a hardcoded
        # "cicero/nda-mutual" -- a JV/consulting playbook block must not be
        # audited against the wrong template.
        audit_id = _log_audit(
            db_path,
            user=request.user,
            doc_type=request.doc_type,
            template_id=f"cicero/{template_name}",
            playbook_ver=validation.playbook_version,
            slot_values=validation_fields,
            gate_result="BLOCKED",
            violations=violations_dicts,
            notes="Blocked before drafting (fail-fast)",
        )
        return {
            "doc_type": request.doc_type,
            "mode": "draft",
            "engine": "cicero",
            "gate_result": "BLOCKED",
            "playbook_version": validation.playbook_version,
            "violations": violations_dicts,
            "output_path": None,
            "audit_id": audit_id,
            "message": (
                f"Document blocked: {len(validation.violations)} issue(s) found. "
                f"Run `audit --doc {audit_id}` to review."
            ),
        }

    # 3. Draft via Cicero template engine. P1.2c: template_name is the ONE
    # registry resolution from step 1 (template_info.name) -- no second, divergent
    # doc_type mapping here, so registry aliases (jv/finder/consultant/cooperation)
    # render against the directory the registry resolved, and every audit row in
    # this function names the same template id.
    cicero_result = cicero_draft(request, template_name=template_name)

    if not cicero_result.success:
        # RT2: fail-fast audit row -- a render failure must be as auditable as a
        # playbook BLOCKED (mirrors the playbook-BLOCKED row above; no output_path).
        audit_id = _log_audit(
            db_path,
            user=request.user,
            doc_type=request.doc_type,
            template_id=f"cicero/{template_name}",
            playbook_ver=validation.playbook_version,
            slot_values=validation_fields,
            gate_result="BLOCKED",
            notes=f"Cicero draft failed: {cicero_result.error}",
        )
        return {
            "error": f"Cicero draft failed: {cicero_result.error}",
            "doc_type": request.doc_type,
            "mode": "draft",
            "engine": "cicero",
            "gate_result": "BLOCKED",
            "audit_id": audit_id,
        }

    # 4. Convert markdown to .docx
    output_path = (
        request.output_path
        or f"data/drafts/{request.doc_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.docx"
    )
    try:
        generated_path = markdown_to_docx(cicero_result.text, output_path)
    except Exception as e:
        log.error(f"Markdown-to-docx failed: {e}")
        # RT2: fail-fast audit row for the conversion failure (no output_path).
        audit_id = _log_audit(
            db_path,
            user=request.user,
            doc_type=request.doc_type,
            template_id=f"cicero/{template_name}",
            playbook_ver=validation.playbook_version,
            slot_values=validation_fields,
            gate_result="BLOCKED",
            notes=f"Document conversion failed: {e}",
        )
        return {
            "error": f"Document conversion failed: {e}",
            "doc_type": request.doc_type,
            "mode": "draft",
            "engine": "cicero",
            "gate_result": "BLOCKED",
            "audit_id": audit_id,
        }

    # 5. Compute schema hash for version tracking
    _schema_hash = None
    if template_info.schema_path and template_info.schema_path.exists():
        _schema_hash = hashlib.sha256(
            template_info.schema_path.read_text(encoding="utf-8").encode()
        ).hexdigest()

    # 5b. sha256 of the EXACT rendered bytes (the deterministic Cicero/Mustache
    # markdown render), so the audit row links audit_id to the rendered artifact
    # itself, not just the input data (data_hash). Byte-stability is the Cicero
    # render layer; the docx conversion (pandoc) is NOT claimed byte-identical.
    render_sha256 = hashlib.sha256(cicero_result.text.encode("utf-8")).hexdigest()

    # 6. Audit log. RT1: the signable docx was already written above -- if the audit
    # write fails, withdraw the artifact (fail-closed) instead of leaving an orphan
    # signable draft that no audit row knows about.
    # T6: engine warnings (e.g. a captured-but-not-rendered disputeForum) go into the
    # NOTES column -- never into slot_values, whose RENDER-data contents stay
    # byte-compatible. C3: each warning segment is sanitized (control chars + the
    # literal ' | ' separator) so user-controlled values cannot spoof notes entries.
    _notes = (f"Cicero deterministic draft, data_hash={cicero_result.data_hash}, "
              f"render_sha256={render_sha256}")
    if cicero_result.warnings:
        _notes += "; WARNINGS: " + " | ".join(
            _sanitize_note_segment(w) for w in cicero_result.warnings)
    # C3: the captured forum is ALSO stored structured, under a clearly
    # capture-namespaced key -- the plain 'disputeForum' key stays absent from
    # slot_values (it is never render data), and the free-text notes stop being the
    # only durable record of the capture.
    _slot_values = {**cicero_bridge._build_data(request, template_name=template_name),
                    "data_hash": cicero_result.data_hash,
                    "render_sha256": render_sha256}
    if request.dispute_forum:
        _slot_values["captured_dispute_forum"] = {
            "value": request.dispute_forum,
            "raw": request.dispute_forum_raw or None,
        }
    try:
        audit_id = _log_audit(
            db_path,
            user=request.user,
            doc_type=request.doc_type,
            template_id=f"cicero/{cicero_result.template_name}@{cicero_result.template_version}",
            playbook_ver=validation.playbook_version,
            slot_values=_slot_values,
            gate_result=validation.gate_result,
            violations=violations_dicts if violations_dicts else None,
            output_path=generated_path,
            notes=_notes,
            model_version=template_info.version,
            schema_hash=_schema_hash,
        )
    except Exception as e:  # noqa: BLE001 -- any audit failure withdraws the draft
        return _withdraw_draft(generated_path, request.doc_type, "cicero", e)

    # 6. Build result
    result: dict = {
        "doc_type": request.doc_type,
        "mode": "draft",
        "engine": "cicero",
        "gate_result": validation.gate_result,
        "playbook_version": validation.playbook_version,
        "template_name": cicero_result.template_name,
        "template_version": cicero_result.template_version,
        "data_hash": cicero_result.data_hash,
        "render_sha256": render_sha256,
        # The EXACT rendered markdown bytes (sha256 == render_sha256), so callers that
        # need the rendered text (demo clause extraction, previews) never re-render (P1).
        # Result-dict only: deliberately NOT written into the audit row's slot_values.
        "rendered_text": cicero_result.text,
        "audit_id": audit_id,
    }

    if validation.gate_result == "ESCALATED":
        result["output_path"] = generated_path
        result["message"] = (
            f"Document generated but escalated: {len(validation.violations)} issue(s) require counsel review. "
            f"Output: {generated_path}"
        )
    else:
        result["output_path"] = generated_path
        result["message"] = f"Document generated successfully. Output: {generated_path}"

    # The footer is PASS-SPECIFIC ("PASS certifies...") -- attach it only on a PASS gate,
    # never on ESCALATED, so --json/API consumers never receive a disclaimer that
    # contradicts the gate. (The CLI also gates its print on PASS.)
    if validation.gate_result == "PASS":
        result["disclaimer"] = LEGAL_DISCLAIMER

    # T6: surface engine warnings (captured-but-not-rendered disputeForum) to callers;
    # the same text is already in the audit row's notes. Key present only when non-empty.
    if cicero_result.warnings:
        result["warnings"] = list(cicero_result.warnings)

    if violations_dicts:
        result["violations"] = violations_dicts

    return result


def _draft_llm(
    request: DraftRequest,
    *,
    provider: str = "anthropic",
    model: Optional[str] = None,
    db_path: str = "data/contract_drafting.db",
) -> dict:
    """Legacy LLM engine: field generation + docxtpl assembly."""
    # 1. Load playbook
    playbook = Playbook.load(request.playbook_path or None)

    # 2. Generate/refine fields via LLM
    llm_fields = _call_llm_for_fields(request, playbook, provider=provider, model=model)

    # 3. Merge LLM-generated fields with request fields (request takes precedence for explicit values)
    effective_date = request.effective_date or datetime.now().strftime("%Y-%m-%d")
    template_fields = {
        "effective_date": effective_date,
        "disclosing_party": request.disclosing_party,
        "receiving_party": request.receiving_party,
        "disclosing_entity_type": request.disclosing_entity_type,
        "receiving_entity_type": request.receiving_entity_type,
        "disclosing_jurisdiction": request.disclosing_jurisdiction,
        "receiving_jurisdiction": request.receiving_jurisdiction or request.disclosing_jurisdiction,
        "purpose": llm_fields.get("purpose", request.purpose),
        "term_months": request.term_months or llm_fields.get("term_months", 24),
        "notice_days": request.notice_days or llm_fields.get("notice_days", 30),
        "survival_years": request.survival_years or llm_fields.get("survival_years", 3),
        "governing_law": request.governing_law or llm_fields.get("governing_law", "Washington"),
        "disclosing_signatory": request.disclosing_signatory,
        "disclosing_title": request.disclosing_title,
        "receiving_signatory": request.receiving_signatory,
        "receiving_title": request.receiving_title,
    }

    # 3b. Enforce the SAME structural/jurisdiction validity as the deterministic
    # path. Previously this legacy --engine llm path validated ONLY against the
    # playbook allowlist, so the Concerto schema enum and the playbook could
    # drift. The legacy path is NDA-shaped (NDA_FIELD_SCHEMA, nda_mutual.docx), so
    # the Concerto schema check applies only when the request is actually an NDA;
    # other doc types are served by the deterministic (cicero) engine and must
    # not be validated against the nda-mutual schema.
    from contract_drafting import jurisdiction_map

    # T6/C1 parity with the cicero path: a supplied dispute forum is captured-not-
    # rendered. Normalize it ONCE here (display -> identifier); it is schema-gated
    # below (NDA), surfaced as a warning (single-sourced wording:
    # cicero_bridge._forum_capture_warnings, so the two engines cannot drift),
    # appended -- sanitized -- to the audit notes, and stored as a structured
    # captured_dispute_forum slot. It NEVER enters template_fields (render data).
    from contract_drafting.cicero_bridge import _forum_capture_warnings

    _captured_forum = ""
    _forum_warnings: list[str] = []
    if request.dispute_forum:
        _captured_forum = jurisdiction_map.to_identifier_enum(
            "DisputeForum", request.dispute_forum)
        _forum_warnings = _forum_capture_warnings(
            _captured_forum, request.dispute_forum_raw or "")

    # Typed abstentions collected at 3b (entity sentinels) / 3c (governingLaw=OTHER);
    # a non-empty list escalates (fail-closed) at 3c, always before any render step.
    _abstentions: list[dict] = []

    if request.doc_type == "nda":
        from contract_drafting.schema_validator import validate_template_data, validate_semantics

        # Normalize entity-type display names -> identifiers (parity with governingLaw and the
        # cicero path) so a caller passing "limited liability company" validates against the
        # EntityType enum instead of being rejected as schema-invalid.
        template_fields["disclosing_entity_type"] = jurisdiction_map.to_identifier_enum(
            "EntityType", template_fields["disclosing_entity_type"])
        template_fields["receiving_entity_type"] = jurisdiction_map.to_identifier_enum(
            "EntityType", template_fields["receiving_entity_type"])

        _schema_check = {
            "disclosingParty": template_fields["disclosing_party"],
            "receivingParty": template_fields["receiving_party"],
            "effectiveDate": template_fields["effective_date"],
            "disclosingEntityType": template_fields["disclosing_entity_type"],
            "receivingEntityType": template_fields["receiving_entity_type"],
            "purpose": template_fields["purpose"],
            "termMonths": template_fields["term_months"],
            "noticeDays": template_fields["notice_days"],
            "survivalYears": template_fields["survival_years"],
            "governingLaw": jurisdiction_map.to_identifier(template_fields["governing_law"]),
            "mutual": request.mutual,
            "hasNonCompete": request.has_non_compete,
            "hasNonSolicitation": request.has_non_solicitation,
            "hasResidualsClause": request.has_residuals_clause,
        }
        # C1 (mirror of the cicero path): a supplied dispute forum is captured-not-
        # rendered, but it must pass the same schema gate -- out-of-enum strings
        # ("KCAB") fail closed instead of riding along on a PASS draft.
        if request.dispute_forum:
            _schema_check["disputeForum"] = _captured_forum
            if request.dispute_forum_raw:
                _schema_check["disputeForumRaw"] = request.dispute_forum_raw
        _schema_errors = validate_template_data(_schema_check, template_name="nda-mutual")
        _schema_errors += validate_semantics(_schema_check, template_name="nda-mutual")
        if _schema_errors:
            # C8: fail-fast BLOCKED audit row for schema-invalid input (parity with
            # the cicero path and every other failure path; no output_path).
            audit_id = _log_audit(
                db_path,
                user=request.user,
                doc_type=request.doc_type,
                template_id=Path(request.template_path).stem,
                playbook_ver="n/a (schema pre-gate)",
                slot_values=_schema_check,
                gate_result="BLOCKED",
                notes="Blocked at schema validation (fail-fast): "
                      + "; ".join(_schema_errors),
            )
            return {
                "error": "; ".join(_schema_errors),
                "doc_type": request.doc_type,
                "mode": "draft",
                "engine": "llm",
                "gate_result": "BLOCKED",
                "output_path": None,
                "audit_id": audit_id,
            }

        # Abstain hatch for entity type: OTHER_ENTITY is the abstain sentinel and must NEVER
        # render (parity with governingLaw=OTHER). Collected here, escalated (fail-closed,
        # ESCALATED + audit row) together with any governing-law abstention at step 3c below
        # -- always before any render step.
        for _ef, _camel, _reqraw, _who in (
            ("disclosing_entity_type", "disclosingEntityType",
             request.disclosing_entity_type_raw, "disclosing"),
            ("receiving_entity_type", "receivingEntityType",
             request.receiving_entity_type_raw, "receiving"),
        ):
            if template_fields[_ef] == "OTHER_ENTITY":
                _abstentions.append({
                    "field": _camel,
                    "sentinel": "OTHER_ENTITY",
                    "raw": _reqraw or llm_fields.get(f"{_ef}_raw"),
                    "who": _who,
                })
        # Render the human-readable display name, never the underscore identifier.
        for _ef in ("disclosing_entity_type", "receiving_entity_type"):
            try:
                template_fields[_ef] = jurisdiction_map.to_display_enum("EntityType", template_fields[_ef])
            except ValueError:
                pass


    # 3c. Abstain hatch (PR2): governingLaw=OTHER must NEVER render. Checked here -- BEFORE
    # the to_display step and UNCONDITIONALLY (not just for doc_type=="nda") -- so every
    # _draft_llm render path fails closed regardless of doc type. (to_display also refuses
    # OTHER as a map-level backstop.) Any abstention (this one + the entity sentinels
    # collected at 3b) escalates ONCE here: fail-closed, ESCALATED, audit row with raw ask.
    if jurisdiction_map.to_identifier(template_fields["governing_law"]) == "OTHER":
        _abstentions.append({
            "field": "governingLaw", "sentinel": "OTHER",
            "raw": request.governing_law_raw or llm_fields.get("governing_law_raw"),
        })
    if _abstentions:
        return _abstention_escalation(_abstentions, request.doc_type, "llm",
                                      db_path=db_path, user=request.user)

    # 3d. Render the human-readable display name, never the underscore identifier.
    # Input is normally already a display name ("New York"); if an identifier
    # ("New_York") was supplied, map it. to_display is fail-closed, so a value it
    # does not recognize (an existing display name, or a non-NDA jurisdiction) is
    # left verbatim for the docx and the playbook check.
    try:
        template_fields["governing_law"] = jurisdiction_map.to_display(
            template_fields["governing_law"]
        )
    except ValueError:
        pass

    # 4. Validate against playbook
    validation_fields = {
        "term_months": template_fields["term_months"],
        "mutual": request.mutual,
        "governing_law": template_fields["governing_law"],
        "has_non_compete": request.has_non_compete,
        "has_non_solicitation": request.has_non_solicitation,
        "has_residuals_clause": request.has_residuals_clause,
    }
    validation: PlaybookValidationResult = playbook.validate_nda(validation_fields)

    violations_dicts = [
        {"clause_type": v.clause_type, "rule_field": v.rule_field,
         "description": v.description, "severity": v.severity}
        for v in validation.violations
    ]

    # 4b. P1.2a fail-closed: a playbook-BLOCKED draft must NOT leave a signable
    # .docx on disk. Previously this path assembled the artifact "for review",
    # then wrote an audit row with output_path=None that disowned it -- an orphan
    # signable draft no audit row knew about. Skip assembly entirely: the audit
    # row (output_path=None) and the result now agree that nothing was written.
    if validation.gate_result == "BLOCKED":
        _blocked_notes = "Blocked before assembly (fail-closed): no artifact written"
        if _forum_warnings:
            _blocked_notes += "; WARNINGS: " + " | ".join(
                _sanitize_note_segment(w) for w in _forum_warnings)
        _blocked_slots = dict(template_fields)
        if request.dispute_forum:
            _blocked_slots["captured_dispute_forum"] = {
                "value": _captured_forum,
                "raw": request.dispute_forum_raw or None,
            }
        audit_id = _log_audit(
            db_path,
            user=request.user,
            doc_type=request.doc_type,
            template_id=Path(request.template_path).stem,
            playbook_ver=validation.playbook_version,
            slot_values=_blocked_slots,
            gate_result="BLOCKED",
            violations=violations_dicts,
            output_path=None,
            notes=_blocked_notes,
        )
        result = {
            "doc_type": request.doc_type,
            "mode": "draft",
            "engine": "llm",
            "gate_result": "BLOCKED",
            "playbook_version": validation.playbook_version,
            "template_fields": template_fields,
            "violations": violations_dicts,
            "output_path": None,
            "audit_id": audit_id,
            "message": (
                f"Document blocked: {len(validation.violations)} issue(s) found. "
                f"Run `audit --doc {audit_id}` to review."
            ),
        }
        if _forum_warnings:
            result["warnings"] = list(_forum_warnings)
        return result

    # 5. Assemble .docx
    output_path = request.output_path or f"data/drafts/{request.doc_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.docx"
    template_path = request.template_path

    if not Path(template_path).exists():
        # RT2: fail-fast audit row (mirrors the playbook-BLOCKED row; no output_path).
        audit_id = _log_audit(
            db_path,
            user=request.user,
            doc_type=request.doc_type,
            template_id=Path(template_path).stem,
            playbook_ver=validation.playbook_version,
            slot_values=template_fields,
            gate_result="BLOCKED",
            notes=f"Template not found: {template_path}",
        )
        return {
            "error": f"Template not found: {template_path}",
            "doc_type": request.doc_type,
            "gate_result": "BLOCKED",
            "audit_id": audit_id,
        }

    try:
        generated_path = _assemble_docx(template_path, template_fields, output_path)
    except Exception as e:
        log.error(f"Document assembly failed: {e}")
        # RT2: fail-fast audit row for the assembly failure (no output_path).
        audit_id = _log_audit(
            db_path,
            user=request.user,
            doc_type=request.doc_type,
            template_id=Path(template_path).stem,
            playbook_ver=validation.playbook_version,
            slot_values=template_fields,
            gate_result="BLOCKED",
            notes=f"Document assembly failed: {e}",
        )
        return {
            "error": f"Document assembly failed: {e}",
            "doc_type": request.doc_type,
            "gate_result": "BLOCKED",
            "audit_id": audit_id,
        }

    # 6. Log to audit trail
    # RT1: the docx was already written above -- if the audit write fails, withdraw
    # the artifact (fail-closed) instead of leaving an orphan draft with no audit row.
    # T6 (parity with the cicero path): a captured forum's warning goes into the NOTES
    # column (C3: each user-influenced segment sanitized), and the capture is ALSO
    # stored structured under the capture-namespaced key -- the plain 'disputeForum'
    # key stays absent from slot_values, and template_fields (render data) is untouched.
    _notes = f"Generated via {provider}" + (f" model={model}" if model else "")
    if _forum_warnings:
        _notes += "; WARNINGS: " + " | ".join(
            _sanitize_note_segment(w) for w in _forum_warnings)
    _slot_values = dict(template_fields)
    if request.dispute_forum:
        _slot_values["captured_dispute_forum"] = {
            "value": _captured_forum,
            "raw": request.dispute_forum_raw or None,
        }
    try:
        audit_id = _log_audit(
            db_path,
            user=request.user,
            doc_type=request.doc_type,
            template_id=Path(template_path).stem,
            playbook_ver=validation.playbook_version,
            slot_values=_slot_values,
            gate_result=validation.gate_result,
            violations=violations_dicts if violations_dicts else None,
            output_path=generated_path,
            notes=_notes,
        )
    except Exception as e:  # noqa: BLE001 -- any audit failure withdraws the draft
        return _withdraw_draft(generated_path, request.doc_type, "llm", e)

    # 7. Build result
    result: dict = {
        "doc_type": request.doc_type,
        "mode": "draft",
        "engine": "llm",
        "gate_result": validation.gate_result,
        "playbook_version": validation.playbook_version,
        "template_fields": template_fields,
        "audit_id": audit_id,
    }

    # P1.2a: BLOCKED returned before assembly above, so only ESCALATED/PASS reach here.
    if validation.gate_result == "ESCALATED":
        result["output_path"] = generated_path
        result["message"] = (
            f"Document generated but escalated: {len(validation.violations)} issue(s) require counsel review. "
            f"Output: {generated_path}"
        )
    else:
        result["output_path"] = generated_path
        result["message"] = f"Document generated successfully. Output: {generated_path}"

    # PASS-specific footer only on a PASS gate (parity with the Cicero path); never on
    # BLOCKED/ESCALATED, so --json/API consumers never get a disclaimer contradicting the gate.
    if validation.gate_result == "PASS":
        result["disclaimer"] = LEGAL_DISCLAIMER

    # T6: surface the captured-but-not-rendered disputeForum warning to callers
    # (parity with the cicero path); the same text is already in the audit row's
    # notes. Key present only when non-empty.
    if _forum_warnings:
        result["warnings"] = list(_forum_warnings)

    if violations_dicts:
        result["violations"] = violations_dicts

    return result


def get_audit_log(
    db_path: str = "data/contract_drafting.db",
    *,
    doc_id: int | None = None,
    limit: int = 10,
) -> list[dict]:
    """Retrieve audit log entries."""
    conn = _init_audit_db(db_path)
    try:
        if doc_id:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE id = ?", (doc_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()

        columns = [d[0] for d in conn.execute("SELECT * FROM audit_log LIMIT 0").description]
        return [dict(zip(columns, row)) for row in rows]
    finally:
        conn.close()
