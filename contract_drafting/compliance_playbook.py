"""
compliance_playbook.py — Playbook engine for compliance drafting and review.

Parses the same legal.local.md format used by Anthropic's Legal Plugin
(knowledge-work-plugins/legal). Single source of truth for both our
drafting pipeline and the plugin's /review-contract and /triage-nda.

Playbook format (markdown):
    ### Limitation of Liability
    - Standard position: Mutual cap at 12 months of fees paid/payable
    - Acceptable range: 6-24 months of fees
    - Escalation trigger: Uncapped liability, consequential damages inclusion

    ## NDA Defaults
    - Mutual obligations required
    - Term: 2-3 years standard, 5 years for trade secrets
    ...
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

import yaml

log = logging.getLogger(__name__)

# Default playbook search paths (same as Legal Plugin)
_DEFAULT_PATHS = [
    ".claude/legal.local.md",
    "data/templates/playbook_default.yaml",
]


@dataclass
class PlaybookRule:
    """A single clause-level rule from the playbook."""
    clause_type: str                  # e.g. "Limitation of Liability"
    standard_position: str = ""       # preferred terms
    acceptable_range: str = ""        # negotiable without escalation
    escalation_trigger: str = ""      # requires senior counsel


@dataclass
class NDADefaults:
    """NDA-specific defaults from the playbook."""
    mutual_required: bool = True
    term_years_standard: str = "2-3"
    term_years_trade_secrets: str = "5"
    carveouts: list[str] = field(default_factory=lambda: [
        "independently developed",
        "publicly available",
        "rightfully received from third party",
        "required by law",
    ])
    prohibited_provisions: list[str] = field(default_factory=lambda: [
        "non-solicitation",
        "non-compete",
        "broad residuals clause",
    ])


@dataclass
class PlaybookViolation:
    """A single validation failure."""
    clause_type: str
    rule_field: str           # "standard_position", "acceptable_range", "escalation_trigger"
    description: str
    severity: Literal["critical", "high", "medium", "low"] = "medium"


@dataclass
class PlaybookValidationResult:
    """Result of validating draft fields against the playbook."""
    gate_result: Literal["PASS", "BLOCKED", "ESCALATED"]
    violations: list[PlaybookViolation] = field(default_factory=list)
    playbook_version: str = "1.0.0"


class Playbook:
    """Loads and validates against the organizational legal playbook.

    Reads legal.local.md (Anthropic Legal Plugin format) or YAML.
    """

    def __init__(
        self,
        rules: list[PlaybookRule] | None = None,
        nda_defaults: NDADefaults | None = None,
        version: str = "1.0.0",
    ) -> None:
        self.rules = rules or []
        self.nda_defaults = nda_defaults or NDADefaults()
        self.version = version

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Playbook":
        """Load playbook from markdown or YAML file.

        If no path given, searches default locations.
        """
        if path:
            p = Path(path)
        else:
            p = None
            for default in _DEFAULT_PATHS:
                candidate = Path(default)
                if candidate.exists():
                    p = candidate
                    break

        if p is None or not p.exists():
            log.warning("No playbook found, using built-in defaults")
            return cls()

        if p.suffix in (".yaml", ".yml"):
            return cls._load_yaml(p)
        return cls._load_markdown(p)

    @classmethod
    def _load_markdown(cls, path: Path) -> "Playbook":
        """Parse legal.local.md format into PlaybookRule list."""
        text = path.read_text(encoding="utf-8")
        rules: list[PlaybookRule] = []
        nda_defaults = NDADefaults()

        # Split into sections by ### headers (clause types)
        # The Legal Plugin uses ### for individual clause types
        sections = re.split(r"^###\s+", text, flags=re.MULTILINE)

        for section in sections[1:]:  # skip preamble before first ###
            lines = section.strip().split("\n")
            clause_type = lines[0].strip()

            standard = ""
            acceptable = ""
            escalation = ""

            for line in lines[1:]:
                line_stripped = line.strip().lstrip("- ")
                lower = line_stripped.lower()

                if lower.startswith("standard position:"):
                    standard = line_stripped.split(":", 1)[1].strip()
                elif lower.startswith("acceptable range:") or lower.startswith("acceptable:"):
                    acceptable = line_stripped.split(":", 1)[1].strip()
                elif lower.startswith("escalation trigger:") or lower.startswith("escalation:"):
                    escalation = line_stripped.split(":", 1)[1].strip()

            if standard or acceptable or escalation:
                rules.append(PlaybookRule(
                    clause_type=clause_type,
                    standard_position=standard,
                    acceptable_range=acceptable,
                    escalation_trigger=escalation,
                ))

        # Parse ## NDA Defaults section
        nda_match = re.search(r"^## NDA Defaults\s*\n(.*?)(?=^##|\Z)", text, re.MULTILINE | re.DOTALL)
        if nda_match:
            nda_text = nda_match.group(1)
            if "mutual" in nda_text.lower():
                nda_defaults.mutual_required = "required" in nda_text.lower()

            term_match = re.search(r"Term:\s*(\d+)-(\d+)\s*years?\s*standard", nda_text, re.I)
            if term_match:
                nda_defaults.term_years_standard = f"{term_match.group(1)}-{term_match.group(2)}"

        # Extract version from frontmatter or heading
        ver_match = re.search(r"version:\s*(\S+)", text, re.I)
        version = ver_match.group(1) if ver_match else "1.0.0"

        return cls(rules=rules, nda_defaults=nda_defaults, version=version)

    @classmethod
    def _load_yaml(cls, path: Path) -> "Playbook":
        """Parse YAML playbook."""
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return cls()

        version = str(data.get("version", "1.0.0"))
        rules = []
        for rule_data in data.get("rules", []):
            rules.append(PlaybookRule(
                clause_type=rule_data.get("clause_type", ""),
                standard_position=rule_data.get("standard_position", ""),
                acceptable_range=rule_data.get("acceptable_range", ""),
                escalation_trigger=rule_data.get("escalation_trigger", ""),
            ))

        nda_data = data.get("nda_defaults", {})
        defaults = NDADefaults()  # get default values from instance
        nda_defaults = NDADefaults(
            mutual_required=nda_data.get("mutual_required", True),
            term_years_standard=str(nda_data.get("term_years_standard", "2-3")),
            term_years_trade_secrets=str(nda_data.get("term_years_trade_secrets", "5")),
            carveouts=nda_data.get("carveouts", defaults.carveouts),
            prohibited_provisions=nda_data.get("prohibited_provisions", defaults.prohibited_provisions),
        )

        return cls(rules=rules, nda_defaults=nda_defaults, version=version)

    def get_rule(self, clause_type: str) -> PlaybookRule | None:
        """Find a rule by clause type (case-insensitive partial match)."""
        clause_lower = clause_type.lower()
        for rule in self.rules:
            if clause_lower in rule.clause_type.lower() or rule.clause_type.lower() in clause_lower:
                return rule
        return None

    def validate_nda(self, fields: dict) -> PlaybookValidationResult:
        """Validate NDA-specific fields against playbook rules.

        Fields dict may contain:
            - term_months: int
            - mutual: bool
            - governing_law: str
            - has_non_compete: bool
            - has_non_solicitation: bool
            - has_residuals_clause: bool
            - liability_cap: str
        """
        violations: list[PlaybookViolation] = []
        defaults = self.nda_defaults

        # Check mutual requirement
        if defaults.mutual_required and not fields.get("mutual", True):
            violations.append(PlaybookViolation(
                clause_type="NDA Structure",
                rule_field="standard_position",
                description="Playbook requires mutual NDA but unilateral was specified",
                severity="high",
            ))

        # Check term length
        term_months = fields.get("term_months")
        if term_months is not None:
            try:
                standard_parts = defaults.term_years_standard.split("-")
                max_standard_months = int(standard_parts[-1]) * 12
                if term_months > max_standard_months:
                    violations.append(PlaybookViolation(
                        clause_type="Term",
                        rule_field="acceptable_range",
                        description=f"Term {term_months} months exceeds playbook standard ({defaults.term_years_standard} years)",
                        severity="high",
                    ))
            except (ValueError, IndexError):
                pass

        # Check prohibited provisions
        if fields.get("has_non_compete"):
            violations.append(PlaybookViolation(
                clause_type="Non-Compete",
                rule_field="escalation_trigger",
                description="Non-compete provisions not permitted in NDA per playbook",
                severity="critical",
            ))

        if fields.get("has_non_solicitation"):
            violations.append(PlaybookViolation(
                clause_type="Non-Solicitation",
                rule_field="escalation_trigger",
                description="Non-solicitation provisions not permitted in NDA per playbook",
                severity="critical",
            ))

        if fields.get("has_residuals_clause"):
            violations.append(PlaybookViolation(
                clause_type="Residuals",
                rule_field="escalation_trigger",
                description="Broad residuals clause flagged for counsel review",
                severity="high",
            ))

        # Check clause-specific rules
        for rule in self.rules:
            if not rule.escalation_trigger:
                continue
            field_value = fields.get(rule.clause_type.lower().replace(" ", "_"), "")
            if not field_value:
                continue
            # Simple keyword check against escalation triggers
            triggers = [t.strip().lower() for t in rule.escalation_trigger.split(",")]
            for trigger in triggers:
                if trigger and trigger in str(field_value).lower():
                    violations.append(PlaybookViolation(
                        clause_type=rule.clause_type,
                        rule_field="escalation_trigger",
                        description=f"Escalation trigger matched: {trigger}",
                        severity="critical",
                    ))

        # Determine gate result
        has_critical = any(v.severity == "critical" for v in violations)
        has_high = any(v.severity == "high" for v in violations)

        if has_critical:
            gate_result = "BLOCKED"
        elif has_high:
            gate_result = "ESCALATED"
        elif violations:
            gate_result = "PASS"  # medium/low violations pass with warnings
        else:
            gate_result = "PASS"

        return PlaybookValidationResult(
            gate_result=gate_result,
            violations=violations,
            playbook_version=self.version,
        )
