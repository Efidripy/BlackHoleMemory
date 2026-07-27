"""Canonical project IDs and compatibility aliases."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

DEFAULT_PROJECT_ID = "blackholememory"
PROJECT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")


class ProjectRegistryError(ValueError):
    """Raised when a project registry is malformed or ambiguous."""


def normalize_project_key(value: str) -> str:
    """Normalize a project/alias for deterministic lookup without data loss."""

    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    text = text.replace("\\", "/")
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")[:128]


@dataclass(frozen=True)
class ProjectDefinition:
    id: str
    label: str
    aliases: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "aliases": list(self.aliases)}


@dataclass(frozen=True)
class ProjectResolution:
    input: str
    canonical: str
    matched_alias: str | None
    known: bool
    accepted_values: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "input": self.input,
            "canonical": self.canonical,
            "matched_alias": self.matched_alias,
            "known": self.known,
            "accepted_values": list(self.accepted_values),
        }


class ProjectRegistry:
    def __init__(self, definitions: tuple[ProjectDefinition, ...], *, default_project: str = DEFAULT_PROJECT_ID):
        if not definitions:
            raise ProjectRegistryError("project registry must contain at least one definition")
        by_id: dict[str, ProjectDefinition] = {}
        alias_index: dict[str, str] = {}
        for definition in definitions:
            if not PROJECT_ID_PATTERN.fullmatch(definition.id):
                raise ProjectRegistryError(f"invalid canonical project id: {definition.id!r}")
            if definition.id in by_id:
                raise ProjectRegistryError(f"duplicate canonical project id: {definition.id!r}")
            by_id[definition.id] = definition
            values = (definition.id, *definition.aliases)
            for value in values:
                normalized = normalize_project_key(value)
                if not normalized:
                    raise ProjectRegistryError(f"empty project alias for {definition.id!r}")
                previous = alias_index.get(normalized)
                if previous is not None and previous != definition.id:
                    raise ProjectRegistryError(
                        f"ambiguous project alias {value!r}: {previous!r} vs {definition.id!r}"
                    )
                alias_index[normalized] = definition.id
        if default_project not in by_id:
            raise ProjectRegistryError(f"default project is not registered: {default_project!r}")
        self._definitions = by_id
        self._alias_index = alias_index
        self.default_project = default_project

    @classmethod
    def from_file(cls, path: Path) -> "ProjectRegistry":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProjectRegistryError(f"cannot load project registry {path}: {exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("projects"), list):
            raise ProjectRegistryError("project registry must contain a projects array")
        definitions: list[ProjectDefinition] = []
        for item in payload["projects"]:
            if not isinstance(item, dict):
                raise ProjectRegistryError("project registry entries must be objects")
            aliases = item.get("aliases") or []
            if not isinstance(aliases, list) or not all(isinstance(alias, str) for alias in aliases):
                raise ProjectRegistryError(f"aliases must be a string array for {item.get('id')!r}")
            definitions.append(
                ProjectDefinition(
                    id=str(item.get("id") or "").strip(),
                    label=str(item.get("label") or item.get("id") or "").strip(),
                    aliases=tuple(alias.strip() for alias in aliases if alias.strip()),
                )
            )
        return cls(tuple(definitions), default_project=str(payload.get("default_project") or DEFAULT_PROJECT_ID))

    def resolve(self, value: str | None) -> ProjectResolution:
        raw = str(value or "").strip()
        if not raw:
            definition = self._definitions[self.default_project]
            return ProjectResolution(
                input=raw,
                canonical=definition.id,
                matched_alias=None,
                known=True,
                accepted_values=(definition.id, *definition.aliases),
            )
        normalized = normalize_project_key(raw)
        canonical = self._alias_index.get(normalized)
        if canonical is None:
            fallback = normalized or self.default_project
            return ProjectResolution(
                input=raw,
                canonical=fallback,
                matched_alias=None,
                known=False,
                accepted_values=tuple(dict.fromkeys((raw, fallback))),
            )
        definition = self._definitions[canonical]
        matched_alias = None if normalized == normalize_project_key(canonical) else raw
        return ProjectResolution(
            input=raw,
            canonical=canonical,
            matched_alias=matched_alias,
            known=True,
            accepted_values=tuple(dict.fromkeys((canonical, *definition.aliases))),
        )

    def canonicalize(self, value: str | None) -> str:
        return self.resolve(value).canonical

    def accepted_values(self, value: str | None) -> set[str]:
        return set(self.resolve(value).accepted_values)

    def report(self) -> dict[str, Any]:
        return {
            "version": 1,
            "default_project": self.default_project,
            "projects": [self._definitions[key].as_dict() for key in sorted(self._definitions)],
        }


@lru_cache(maxsize=1)
def get_default_project_registry() -> ProjectRegistry:
    """Load the checked-in registry once for API and Qdrant collection paths."""

    path = Path(__file__).resolve().parents[2] / "config" / "project-registry.json"
    return ProjectRegistry.from_file(path)


def canonical_project_id(value: str | None) -> str:
    return get_default_project_registry().canonicalize(value)
