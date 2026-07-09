"""
template_registry.py -- Auto-discover contract template types from directory scan.

Scans data/templates/cicero/ for directories containing package.json + model/model.cto.
Each directory is a registered template type with metadata from package.json.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_TEMPLATES_BASE = Path(__file__).resolve().parent.parent / "data" / "templates" / "cicero"

# Standard doc_type → template directory mappings
_DOC_TYPE_MAP = {
    "nda": "nda-mutual",
    "jv": "joint-venture",
    "joint-venture": "joint-venture",
    "intermediary": "intermediary",
    "finder": "intermediary",
    "cooperation": "strategic-cooperation",
    "strategic-cooperation": "strategic-cooperation",
    "partnership": "partnership",
    "consulting": "consulting",
    "consultant": "consulting",
}


@dataclass
class TemplateInfo:
    """Metadata about a registered contract template."""
    name: str
    version: str
    description: str
    path: Path
    model_path: Path
    schema_path: Optional[Path]
    has_grammar: bool


@dataclass
class TemplateRegistry:
    """Registry of all available contract template types."""
    templates: dict[str, TemplateInfo] = field(default_factory=dict)

    @classmethod
    def scan(cls, base_dir: Path | str | None = None) -> "TemplateRegistry":
        """Scan directory for valid Cicero templates."""
        base = Path(base_dir) if base_dir else _TEMPLATES_BASE
        registry = cls()

        if not base.is_dir():
            log.warning(f"Template base directory not found: {base}")
            return registry

        for child in sorted(base.iterdir()):
            if not child.is_dir():
                continue
            pkg_path = child / "package.json"
            model_path = child / "model" / "model.cto"
            if not pkg_path.exists() or not model_path.exists():
                continue

            try:
                pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                log.warning(f"Skipping {child.name}: {e}")
                continue

            schema_path = child / "schema.json"
            grammar_path = child / "text" / "grammar.tem.md"

            registry.templates[child.name] = TemplateInfo(
                name=child.name,
                version=pkg.get("version", "0.0.0"),
                description=pkg.get("description", ""),
                path=child,
                model_path=model_path,
                schema_path=schema_path if schema_path.exists() else None,
                has_grammar=grammar_path.exists(),
            )

        return registry

    def get(self, name: str) -> Optional[TemplateInfo]:
        return self.templates.get(name)

    def get_for_doc_type(self, doc_type: str) -> Optional[TemplateInfo]:
        """Map a document type to a template."""
        template_name = _DOC_TYPE_MAP.get(doc_type, doc_type)
        return self.templates.get(template_name)

    def list_types(self) -> list[str]:
        return list(self.templates.keys())


_registry: Optional[TemplateRegistry] = None


def get_registry(*, refresh: bool = False) -> TemplateRegistry:
    """Get or create the template registry singleton."""
    global _registry
    if _registry is None or refresh:
        _registry = TemplateRegistry.scan()
    return _registry
