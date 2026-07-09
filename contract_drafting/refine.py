"""
refine.py — clause-level refinement + library promotion.

Primitives:
- refine_clause()  : LLM rewrites one clause against instruction + playbook, returns diff
- diff_clause()    : structured word-level diff
- validate_refined_clause() : 3-layer check (Mustache valid, vars allowed, conditionals preserved)
- promote_clause() : atomic O_EXCL write to _overlays/{clause}/{overlay_id}.md + meta.json

Architectural invariant: Cicero stays LLM-free at render time. The LLM only runs here.
Refined text is a drop-in Mustache partial that the deterministic renderer consumes
exactly like any other partial.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

import chevron

from contract_drafting.cicero_bridge import (
    _TEMPLATES_BASE,
    _scan_anchors,
    _resolve_template_dir,
    _read_template,
)
from contract_drafting.llm import call_llm
from contract_drafting.compliance_playbook import Playbook

log = logging.getLogger(__name__)

SHARED_CLAUSES_DIR = _TEMPLATES_BASE.parent / "shared-clauses"
OVERLAYS_DIR = SHARED_CLAUSES_DIR / "_overlays"

# Pattern for Mustache variable refs: {{var}}, skipping {{#cond}}, {{/cond}}, {{^cond}}, {{> partial}}
_VAR_RE = re.compile(r"\{\{\s*(?![#/^>])([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
# Pattern for conditional section opens: {{#name}} or {{^name}}
_COND_OPEN_RE = re.compile(r"\{\{\s*[#^]\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")

# Overlay id naming: lowercase alphanumeric with hyphens, matches anchor naming
_OVERLAY_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,50}$")

# P1.2b: Mustache constructs that must NEVER appear in LLM-refined clause text.
# _VAR_RE cannot see any of them, so before this check they passed all three
# validation layers and then misbehaved at render time:
# - {{> partial}}  injects an arbitrary partial (or silently renders as empty
#   text when the renderer has no such partial) -- silent-drop injection;
# - {{&var}} / {{{var}}}  bypass the allowed-vars check entirely (an unknown
#   ref silently renders empty);
# - {{=| |=}}  rebinding delimiters would let every LATER tag evade _VAR_RE,
#   _COND_OPEN_RE, and this check itself.
# No canonical shared clause uses any of these, so rejecting them fail-closed
# costs nothing legitimate.
_FORBIDDEN_TAG_RE = re.compile(r"\{\{\s*(?:[>&=]|\{)")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ClauseDiff:
    """Structured word-level diff for display + audit."""
    unified: str              # difflib.unified_diff output as single string
    added_words: list[str]    # words present in after but not before
    removed_words: list[str]  # words present in before but not after
    changed: bool             # True iff before != after


@dataclass
class ValidationResult:
    """Result of validating a refined clause against 3 layers."""
    ok: bool
    errors: list[str] = field(default_factory=list)
    unknown_vars: set[str] = field(default_factory=set)
    dropped_conditionals: set[str] = field(default_factory=set)


@dataclass
class RefinedClause:
    """Result of refine_clause()."""
    clause_name: str
    before: str               # original clause text
    after: str                # LLM-produced revised text (or empty on BLOCKED)
    instruction: str          # the user instruction that prompted the rewrite
    diff: ClauseDiff
    validation: ValidationResult
    status: Literal["PASS", "BLOCKED"]
    reason: str = ""          # populated on BLOCKED
    sha256_before: str = ""
    sha256_after: str = ""


@dataclass
class PromotedClause:
    """Result of promote_clause()."""
    clause_name: str
    overlay_id: str
    overlay_path: str
    meta_path: str
    sha256: str


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_canonical_clause(clause_name: str) -> str:
    """Read the canonical shared clause text."""
    path = SHARED_CLAUSES_DIR / f"{clause_name}.md"
    if not path.is_file():
        raise FileNotFoundError(
            f"Shared clause not found: {clause_name} "
            f"(expected at {path}). Available: "
            f"{sorted(p.stem for p in SHARED_CLAUSES_DIR.glob('*.md'))}"
        )
    return path.read_text(encoding="utf-8")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _extract_variables(text: str) -> set[str]:
    """Return the set of {{variable}} references in a Mustache template.

    Skips partial anchors {{> X}} and section opens/closes/inversions.
    """
    return set(_VAR_RE.findall(text))


def _extract_conditional_opens(text: str) -> set[str]:
    """Return the set of {{#X}} or {{^X}} section opens in a template."""
    return set(_COND_OPEN_RE.findall(text))


def _collect_allowed_vars(data: dict, template_name: str) -> set[str]:
    """Collect the variable names available to a refinement.

    Union of:
    - keys in the data dict
    - variant flags declared in the template's composition.json
    """
    allowed = set(data.keys())
    comp_path = _resolve_template_dir(template_name) / "composition.json"
    if comp_path.is_file():
        comp = json.loads(comp_path.read_text(encoding="utf-8"))
        allowed.update(comp.get("variants", {}).keys())
    return allowed


def _build_refine_prompt(
    *,
    clause_name: str,
    original: str,
    instruction: str,
    allowed_vars: set[str],
    playbook_context: str,
) -> tuple[str, str]:
    """Construct (system_prompt, user_prompt) for the refinement LLM call."""
    system = (
        "You are rewriting ONE CLAUSE of a legal contract. Return only the revised "
        "clause text in CiceroMark (Mustache) syntax. No commentary, no code fences, "
        "no markdown headings unless the original had them.\n\n"
        "RULES:\n"
        "- Only use Mustache variables from the allowed list. If the user's change "
        "requires a new variable, DO NOT invent one. Instead, respond with a single "
        "line starting with 'NEEDS_NEW_VAR: <name>' and stop.\n"
        "- Preserve every {{#section}}..{{/section}} and {{^section}}..{{/section}} "
        "conditional present in the original, unless the instruction explicitly says "
        "to remove one.\n"
        "- If the instruction would violate the playbook rules, respond with a single "
        "line starting with 'PLAYBOOK_VIOLATION: <which rule>' and stop.\n"
        "- Return bare Mustache text. No triple-backtick fences. No ``` anywhere."
    )
    user = (
        f"ORIGINAL CLAUSE ({clause_name}):\n{original}\n\n"
        f"PLAYBOOK RULES for {clause_name}:\n{playbook_context or '(no specific rules)'}\n\n"
        f"AVAILABLE VARIABLES IN THIS CONTRACT:\n{sorted(allowed_vars)}\n\n"
        f"USER INSTRUCTION:\n{instruction}\n\n"
        "REVISED CLAUSE:"
    )
    return system, user


def _strip_code_fences(text: str) -> str:
    """Strip leading/trailing ``` fences the LLM might wrap around output."""
    t = text.strip()
    if t.startswith("```"):
        # Drop first line (``` or ```markdown etc) and the trailing fence
        first_nl = t.find("\n")
        if first_nl != -1:
            t = t[first_nl + 1:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3].rstrip()
    return t


def _call_refine_llm(system_prompt: str, user_prompt: str) -> str:
    """Thin wrapper around call_llm for easy test mocking."""
    return call_llm(
        question=user_prompt,
        context="",
        system_prompt=system_prompt,
    )


def _playbook_context_for(clause_name: str) -> str:
    """Look up playbook rules relevant to a clause. Returns markdown-formatted text."""
    try:
        pb = Playbook.load()
    except Exception as e:
        log.warning(f"Playbook load failed: {e}")
        return ""

    # Try direct match first (e.g., "confidentiality" → matches "Confidentiality" rule)
    rule = pb.get_rule(clause_name.replace("-", " "))
    if rule is None:
        return ""
    parts = [f"- **{rule.clause_type}**"]
    if rule.standard_position:
        parts.append(f"  - Standard position: {rule.standard_position}")
    if rule.acceptable_range:
        parts.append(f"  - Acceptable range: {rule.acceptable_range}")
    if rule.escalation_trigger:
        parts.append(f"  - Escalation trigger: {rule.escalation_trigger}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Public primitives
# ---------------------------------------------------------------------------

def diff_clause(before: str, after: str) -> ClauseDiff:
    """Produce a structured diff between two clause texts."""
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    unified = "".join(difflib.unified_diff(
        before_lines, after_lines,
        fromfile="before", tofile="after", n=3,
    ))

    before_words = before.split()
    after_words = after.split()
    before_set = set(before_words)
    after_set = set(after_words)
    return ClauseDiff(
        unified=unified,
        added_words=[w for w in after_words if w not in before_set],
        removed_words=[w for w in before_words if w not in after_set],
        changed=(before != after),
    )


def validate_refined_clause(
    text: str,
    *,
    allowed_vars: set[str],
    original_text: str,
) -> ValidationResult:
    """Run 4 validation checks on a refined clause:
    0. No forbidden Mustache constructs ({{> partial}}, {{&var}}, {{{var}}},
       {{=delim=}}) -- these evade the variable extractor and render as silent
       injection/drops (P1.2b)
    1. Mustache syntactically valid (chevron renders without raising)
    2. All {{var}} references are in allowed_vars
    3. All {{#cond}} and {{^cond}} sections from the original are preserved
    """
    errors: list[str] = []
    unknown_vars: set[str] = set()
    dropped: set[str] = set()

    # 0. Forbidden construct check (fail-closed; see _FORBIDDEN_TAG_RE)
    forbidden = sorted({m.group(0).strip() for m in _FORBIDDEN_TAG_RE.finditer(text)})
    if forbidden:
        errors.append(
            f"Refined clause contains forbidden Mustache syntax {forbidden}: "
            f"partials ({{{{> ...}}}}), unescaped-variable tags ({{{{& ...}}}} / "
            f"{{{{{{ ...}}}}}}), and delimiter changes ({{{{=...=}}}}) are not "
            f"allowed in refined clauses."
        )

    # 1. Mustache syntax check: render with empty-ish data; chevron raises on malformed markup
    try:
        # Provide truthy values so sections render and invalid closing tags trigger errors
        dummy_data = {v: "X" for v in allowed_vars}
        # Also set any conditional names from the refined text to True so they expand
        for cond in _extract_conditional_opens(text):
            dummy_data[cond] = True
        chevron.render(text, dummy_data)
    except Exception as e:
        errors.append(f"Invalid Mustache syntax: {e}")

    # 2. Variable reference check
    refined_vars = _extract_variables(text)
    unknown_vars = refined_vars - allowed_vars
    if unknown_vars:
        errors.append(
            f"Refined clause references undefined variables: {sorted(unknown_vars)}"
        )

    # 3. Conditional preservation check
    original_conds = _extract_conditional_opens(original_text)
    refined_conds = _extract_conditional_opens(text)
    dropped = original_conds - refined_conds
    if dropped:
        errors.append(
            f"Refined clause dropped conditional sections from original: {sorted(dropped)}"
        )

    return ValidationResult(
        ok=(not errors),
        errors=errors,
        unknown_vars=unknown_vars,
        dropped_conditionals=dropped,
    )


def refine_clause(
    clause_name: str,
    instruction: str,
    *,
    data: dict,
    template_name: str,
    overlay_id: Optional[str] = None,
    db_path: Optional[str] = None,
    user: str = "agent",
) -> RefinedClause:
    """Refine one clause via LLM and validate the result.

    Args:
        clause_name: Mustache partial stem (e.g. "governing-law", "dispute-resolution").
        instruction: Natural-language instruction from the user.
        data: The contract data dict. Used to derive allowed variables.
        template_name: Contract template name. Used to scope allowed variants + load canonical.
        overlay_id: If provided, start from this existing overlay instead of the canonical.
        db_path: If provided, log the refinement event to this SQLite audit DB.
        user: User/agent identifier for audit trail.

    Returns RefinedClause. status="PASS" when validation succeeded; "BLOCKED" otherwise.
    Does not write to disk (except audit, if db_path provided). Does not mutate data.
    """
    # Load the starting clause text
    if overlay_id:
        overlay_path = OVERLAYS_DIR / clause_name / f"{overlay_id}.md"
        if not overlay_path.is_file():
            raise FileNotFoundError(
                f"Overlay not found: {clause_name}/{overlay_id} "
                f"(expected at {overlay_path})"
            )
        before = overlay_path.read_text(encoding="utf-8")
    else:
        before = _load_canonical_clause(clause_name)

    allowed_vars = _collect_allowed_vars(data, template_name)
    playbook_context = _playbook_context_for(clause_name)

    system_prompt, user_prompt = _build_refine_prompt(
        clause_name=clause_name,
        original=before,
        instruction=instruction,
        allowed_vars=allowed_vars,
        playbook_context=playbook_context,
    )

    try:
        raw_response = _call_refine_llm(system_prompt, user_prompt)
    except Exception as e:
        return RefinedClause(
            clause_name=clause_name, before=before, after="",
            instruction=instruction,
            diff=diff_clause(before, ""),
            validation=ValidationResult(ok=False, errors=[f"LLM call failed: {e}"]),
            status="BLOCKED",
            reason=f"LLM_ERROR: {e}",
            sha256_before=_sha256(before),
        )

    stripped = _strip_code_fences(raw_response)

    # Parse escape-hatch prefixes
    first_line = stripped.split("\n", 1)[0].strip()
    if first_line.startswith("NEEDS_NEW_VAR:"):
        return RefinedClause(
            clause_name=clause_name, before=before, after="",
            instruction=instruction,
            diff=diff_clause(before, ""),
            validation=ValidationResult(
                ok=False,
                errors=[first_line],
            ),
            status="BLOCKED",
            reason=first_line,
            sha256_before=_sha256(before),
        )
    if first_line.startswith("PLAYBOOK_VIOLATION:"):
        return RefinedClause(
            clause_name=clause_name, before=before, after="",
            instruction=instruction,
            diff=diff_clause(before, ""),
            validation=ValidationResult(ok=False, errors=[first_line]),
            status="BLOCKED",
            reason=first_line,
            sha256_before=_sha256(before),
        )

    after = stripped
    validation = validate_refined_clause(
        after, allowed_vars=allowed_vars, original_text=before,
    )
    diff = diff_clause(before, after)
    status: Literal["PASS", "BLOCKED"] = "PASS" if validation.ok else "BLOCKED"
    reason = "" if validation.ok else "VALIDATION_FAILED: " + "; ".join(validation.errors)

    refined = RefinedClause(
        clause_name=clause_name,
        before=before,
        after=after,
        instruction=instruction,
        diff=diff,
        validation=validation,
        status=status,
        reason=reason,
        sha256_before=_sha256(before),
        sha256_after=_sha256(after) if after else "",
    )

    if db_path:
        _log_refinement_event(db_path, user=user, refined=refined,
                              template_name=template_name, overlay_id=overlay_id)

    return refined


def _log_refinement_event(
    db_path: str,
    *,
    user: str,
    refined: RefinedClause,
    template_name: str,
    overlay_id: Optional[str],
) -> None:
    """Record a refinement event to the shared audit_log table.

    Uses `doc_type='refinement'` to distinguish from drafts. The free-text `notes`
    field carries a JSON payload with the full refinement details.
    """
    try:
        from contract_drafting.compliance_draft import _log_audit
        pb_version = Playbook.load().version
    except Exception as e:
        log.warning(f"Refinement audit skipped (import/playbook failed): {e}")
        return

    notes_payload = {
        "event": "clause_refinement",
        "clause_name": refined.clause_name,
        "instruction": refined.instruction,
        "source_overlay_id": overlay_id,  # None if refining from canonical
        "status": refined.status,
        "reason": refined.reason,
        "sha256_before": refined.sha256_before,
        "sha256_after": refined.sha256_after,
        "diff_summary": {
            "added_words_n": len(refined.diff.added_words),
            "removed_words_n": len(refined.diff.removed_words),
            "changed": refined.diff.changed,
        },
        "validation_errors": refined.validation.errors,
    }

    try:
        _log_audit(
            db_path=db_path,
            user=user,
            doc_type="refinement",
            template_id=template_name,
            playbook_ver=pb_version,
            slot_values={"clause_name": refined.clause_name},
            gate_result=refined.status,  # PASS or BLOCKED
            notes=json.dumps(notes_payload),
        )
    except Exception as e:
        log.warning(f"Refinement audit write failed: {e}")


def promote_clause(
    overlay_id: str,
    clause_name: str,
    text: str,
    *,
    meta: Optional[dict] = None,
    db_path: Optional[str] = None,
    user: str = "agent",
) -> PromotedClause:
    """Atomically write a promoted clause overlay + its meta.json.

    - Path: data/templates/shared-clauses/_overlays/{clause_name}/{overlay_id}.md
    - Errors with FileExistsError if (clause_name, overlay_id) already exists.
    - Writes meta.json with sha256, created_at, playbook_version, and any passed meta.

    Raises:
        ValueError: overlay_id fails naming rules (lowercase alphanumeric + hyphens).
        FileNotFoundError: canonical clause for clause_name doesn't exist (reject
            promoting unknown clause names).
        FileExistsError: (clause_name, overlay_id) already exists.
    """
    if not _OVERLAY_ID_RE.fullmatch(overlay_id):
        raise ValueError(
            f"Invalid overlay_id '{overlay_id}'. Must be lowercase alphanumeric with "
            f"hyphens only, 1-51 chars, starting with a letter."
        )

    # Enforce: clause_name must correspond to an existing canonical clause.
    # This prevents typos and guards against the library growing overlays with
    # no canonical counterpart.
    canonical_path = SHARED_CLAUSES_DIR / f"{clause_name}.md"
    if not canonical_path.is_file():
        raise FileNotFoundError(
            f"Cannot promote overlay for unknown clause '{clause_name}'. "
            f"Canonical file expected at {canonical_path}."
        )

    clause_dir = OVERLAYS_DIR / clause_name
    clause_dir.mkdir(parents=True, exist_ok=True)
    overlay_path = clause_dir / f"{overlay_id}.md"
    meta_path = clause_dir / f"{overlay_id}.meta.json"

    # Atomic O_EXCL create for the .md — fails if already exists.
    # Do this BEFORE writing meta.json so collisions are detected before side effects.
    try:
        fd = os.open(str(overlay_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        raise FileExistsError(
            f"Overlay already exists: {clause_name}/{overlay_id}. "
            f"Choose a different overlay_id."
        )

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        # If the .md write failed after O_EXCL succeeded, clean up to keep promotion atomic.
        try:
            overlay_path.unlink()
        except Exception:
            pass
        raise

    # Build meta.json. If meta.json write fails, also clean up the .md.
    try:
        pb_version = Playbook.load().version
    except Exception:
        pb_version = "unknown"
    full_meta: dict = {
        "overlay_id": overlay_id,
        "clause_name": clause_name,
        "sha256": _sha256(text),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "playbook_version": pb_version,
    }
    if meta:
        # Caller-provided meta cannot overwrite the enforced fields
        for k, v in meta.items():
            if k not in full_meta:
                full_meta[k] = v

    try:
        meta_path.write_text(json.dumps(full_meta, indent=2, default=str), encoding="utf-8")
    except Exception:
        # Clean up the .md so the library never has orphan overlays
        try:
            overlay_path.unlink()
        except Exception:
            pass
        raise

    promoted = PromotedClause(
        clause_name=clause_name,
        overlay_id=overlay_id,
        overlay_path=str(overlay_path),
        meta_path=str(meta_path),
        sha256=full_meta["sha256"],
    )

    if db_path:
        try:
            from contract_drafting.compliance_draft import _log_audit
            notes_payload = {
                "event": "clause_promotion",
                "clause_name": clause_name,
                "overlay_id": overlay_id,
                "overlay_path": str(overlay_path),
                "sha256": promoted.sha256,
                "meta": full_meta,
            }
            _log_audit(
                db_path=db_path,
                user=user,
                doc_type="promotion",
                template_id=clause_name,  # reuse column for clause identity
                playbook_ver=pb_version,
                slot_values={"overlay_id": overlay_id},
                gate_result="PASS",
                notes=json.dumps(notes_payload, default=str),
            )
        except Exception as e:
            log.warning(f"Promotion audit write failed: {e}")

    return promoted


def list_overlays(clause_name: Optional[str] = None) -> dict[str, list[str]]:
    """Enumerate overlays on disk. Keys are clause_names, values are overlay_ids.

    If clause_name is given, returns only that clause's overlays.
    """
    result: dict[str, list[str]] = {}
    if not OVERLAYS_DIR.is_dir():
        return result

    targets = [OVERLAYS_DIR / clause_name] if clause_name else sorted(OVERLAYS_DIR.iterdir())
    for clause_dir in targets:
        if not clause_dir.is_dir():
            continue
        ids = sorted(p.stem for p in clause_dir.glob("*.md"))
        if ids:
            result[clause_dir.name] = ids
    return result
