"""validate_templates.py — static linter for CiceroMark templates.

Scans every template under data/templates/cicero/*, extracts its partial anchors
({{> clause-name}}), and cross-checks them against data/templates/shared-clauses/.
Also verifies (RT8) that any template declaring @Display-decorated enums in its
.cto (or enum-backed @Abstainable fields in abstain-policy.json) ships a generated
enum-displays.map.json covering those enums — jurisdiction_map.to_display_enum is
FAIL-OPEN (verbatim pass-through) when the map is missing, so a missing/stale map
would silently render raw enum identifiers into contracts.
Reports any broken anchors. Exit 0 on success, 1 on any error.

Usage (CLI):
    venv/bin/python -m contract_drafting.validate_templates

Usage (programmatic, e.g. pytest):
    from contract_drafting.validate_templates import validate_all
    exit_code, errors = validate_all()
    assert exit_code == 0, errors
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

from contract_drafting.cicero_bridge import _TEMPLATES_BASE, _ANCHOR_RE, _scan_anchors

_ENUM_DECL_RE = re.compile(r"^\s*enum\s+([A-Za-z_]\w*)\s*\{")
_ENUM_MEMBER_RE = re.compile(r"^\s*o\s+([A-Za-z_]\w*)\s*$")


def _scan_anchors_with_counts(template_text: str) -> Counter:
    """Return a Counter of anchor occurrences (not deduplicated)."""
    return Counter(_ANCHOR_RE.findall(template_text))


def _display_enums_in_cto(cto_text: str) -> set[str]:
    """Names of the enums in a .cto that carry at least one @Display member decorator
    (the decorators concerto-helper.js extracts into enum-displays.map.json)."""
    found: set[str] = set()
    current: str | None = None
    saw_display = False
    for ln in cto_text.splitlines():
        m = _ENUM_DECL_RE.match(ln)
        if m:
            current, saw_display = m.group(1), False
            continue
        if current is None:
            continue
        if "@Display(" in ln:
            saw_display = True
        if ln.strip() == "}":
            if saw_display:
                found.add(current)
            current, saw_display = None, False
    return found


def _enum_members_in_cto(cto_text: str) -> dict[str, set[str]]:
    """Every enum declared in a .cto -> the set of its member identifiers (C11:
    member-level source of truth for the enum-displays map lint)."""
    members: dict[str, set[str]] = {}
    current: str | None = None
    for ln in cto_text.splitlines():
        m = _ENUM_DECL_RE.match(ln)
        if m:
            current = m.group(1)
            members[current] = set()
            continue
        if current is None:
            continue
        if ln.strip() == "}":
            current = None
            continue
        mm = _ENUM_MEMBER_RE.match(ln)
        if mm:
            members[current].add(mm.group(1))
    return members


def _check_enum_display_map(tpl_dir: Path) -> list[str]:
    """RT8 lint: if a template declares @Display-decorated enums (model/model.cto) or
    enum-backed abstain fields (abstain-policy.json), its generated
    enum-displays.map.json MUST exist and cover those enums. to_display_enum is
    fail-open without the map (verbatim pass-through), so a missing/stale map would
    silently render raw enum identifiers — error here instead.

    C11: the check is MEMBER-level, not just name-level — for each required enum that
    the .cto declares, the map's member keys must equal the .cto member set. A member
    added to the enum but missing from the map (fail-open verbatim render) or a map
    key no longer in the enum (stale codegen artifact) are both lint errors."""
    required: set[str] = set()
    cto_members: dict[str, set[str]] = {}

    cto = tpl_dir / "model" / "model.cto"
    if cto.is_file():
        cto_text = cto.read_text(encoding="utf-8")
        required |= _display_enums_in_cto(cto_text)
        cto_members = _enum_members_in_cto(cto_text)

    policy_path = tpl_dir / "abstain-policy.json"
    if policy_path.is_file():
        try:
            policies = json.loads(policy_path.read_text(encoding="utf-8")).get("policies")
        except json.JSONDecodeError as e:
            return [f"{tpl_dir.name}: abstain-policy.json is not valid JSON ({e})"]
        if isinstance(policies, dict):
            required |= {
                p["enum"] for p in policies.values()
                if isinstance(p, dict) and isinstance(p.get("enum"), str) and p["enum"]
            }

    if not required:
        return []  # no @Display enums and no enum-backed abstain fields: nothing to check

    map_path = tpl_dir / "enum-displays.map.json"
    if not map_path.is_file():
        return [
            f"{tpl_dir.name}: declares @Display/abstain-backed enum(s) "
            f"{sorted(required)} but enum-displays.map.json is MISSING — "
            f"to_display_enum would fail open and render raw enum identifiers. "
            f"Run `npm run generate` to regenerate the map."
        ]
    try:
        doc = json.loads(map_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return [f"{tpl_dir.name}: enum-displays.map.json is not valid JSON ({e})"]
    missing = sorted(
        name for name in required
        if not isinstance(doc.get(name), dict) or not doc.get(name)
    )
    if missing:
        return [
            f"{tpl_dir.name}: enum-displays.map.json is STALE — missing enum(s) "
            f"{missing} (declared in model.cto/abstain-policy.json). "
            f"Run `npm run generate` to regenerate the map."
        ]

    # C11: member-level drift, per required enum the .cto actually declares.
    # (Enums required only via abstain-policy.json but absent from the .cto keep the
    # name-level check above — there is no member set to compare against.)
    errors: list[str] = []
    for name in sorted(required):
        declared = cto_members.get(name)
        if not declared:
            continue
        mapped = set(doc.get(name) or {})
        missing_members = sorted(declared - mapped)
        stale_members = sorted(mapped - declared)
        if missing_members:
            errors.append(
                f"{tpl_dir.name}: enum-displays.map.json is STALE — enum {name} is "
                f"missing member(s) {missing_members} declared in model.cto "
                f"(to_display_enum would fail closed or render raw identifiers). "
                f"Run `npm run generate` to regenerate the map."
            )
        if stale_members:
            errors.append(
                f"{tpl_dir.name}: enum-displays.map.json is STALE — enum {name} carries "
                f"member(s) {stale_members} that are no longer in model.cto. "
                f"Run `npm run generate` to regenerate the map."
            )
    return errors


def validate_all() -> tuple[int, list[str]]:
    """Scan every template, cross-check anchors against shared-clauses/.

    Checks performed per template:
    1. All {{> X}} anchors resolve to a file in shared-clauses/
    2. No anchor is used more than once in the same template (addressing ambiguity:
       "refine 'X'" is ambiguous if there are two instances of {{> X}})
    3. RT8: any @Display-decorated enum (model.cto) or enum-backed abstain field
       (abstain-policy.json) is covered by a generated enum-displays.map.json

    Returns (exit_code, list_of_messages). Exit code 0 if clean, 1 if any errors.
    """
    errors: list[str] = []
    shared_dir = _TEMPLATES_BASE.parent / "shared-clauses"
    if not shared_dir.is_dir():
        return 1, [f"shared-clauses directory not found: {shared_dir}"]

    available = {p.stem for p in shared_dir.glob("*.md")}
    n_templates = 0
    n_anchors = 0
    overlays_dir = shared_dir / "_overlays"
    n_overlays = sum(
        1
        for clause_dir in (overlays_dir.iterdir() if overlays_dir.is_dir() else [])
        if clause_dir.is_dir()
        for _ in clause_dir.glob("*.md")
    )

    for tpl_dir in sorted(_TEMPLATES_BASE.iterdir()):
        if not tpl_dir.is_dir():
            continue
        grammar = tpl_dir / "text" / "grammar.tem.md"
        if not grammar.exists():
            continue

        n_templates += 1
        text = grammar.read_text(encoding="utf-8")
        anchor_counts = _scan_anchors_with_counts(text)
        anchors = set(anchor_counts.keys())
        n_anchors += len(anchors)

        missing = anchors - available
        if missing:
            errors.append(
                f"{tpl_dir.name}: references missing shared clauses: {sorted(missing)}"
            )

        duplicates = [name for name, n in anchor_counts.items() if n > 1]
        if duplicates:
            errors.append(
                f"{tpl_dir.name}: duplicate anchor(s) {sorted(duplicates)} — "
                f"clause-by-name addressing is ambiguous when the same partial "
                f"is included more than once"
            )

        errors.extend(_check_enum_display_map(tpl_dir))

    if errors:
        return 1, errors
    msg = (
        f"OK: {n_templates} templates scanned, {n_anchors} anchors resolved, "
        f"{len(available)} shared clauses available"
    )
    if n_overlays:
        msg += f", {n_overlays} overlay(s) promoted"
    return 0, [msg]


def main() -> None:
    exit_code, messages = validate_all()
    for msg in messages:
        if exit_code == 0:
            print(msg)
        else:
            print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
