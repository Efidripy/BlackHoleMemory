"""Capability-contract builder for the canonical code graph.

This module owns only the deterministic parser-vs-inventory projection. The
registry and inventory maps remain owned by ``code_graph`` so parser activation
and digest contracts keep their existing import surface.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping


def build_parser_capability_matrix(
    *,
    schema_version: str,
    parser_registry: Mapping[str, Mapping[str, str]],
    language_by_suffix: Mapping[str, str],
    special_text_names: set[str] | frozenset[str],
) -> dict[str, Any]:
    """Build the truthful structural-parser versus inventory matrix."""

    extensions_by_language: dict[str, list[str]] = defaultdict(list)
    for suffix, language in sorted(language_by_suffix.items()):
        extensions_by_language[str(language)].append(str(suffix))
    for name in sorted(special_text_names):
        inventory_language = {
            "dockerfile": "dockerfile",
            "makefile": "makefile",
            "cmakelists.txt": "cmake",
            "justfile": "justfile",
            "meson.build": "meson",
            "go.mod": "gomod",
            "go.sum": "gomod",
            "kconfig": "kconfig",
            "kconfigfile": "kconfig",
            "docker-bake.hcl": "hcl",
            "build": "starlark",
            "build.bazel": "starlark",
            "workspace": "starlark",
        }.get(str(name).casefold(), "config")
        extensions_by_language.setdefault(inventory_language, []).append(str(name))
    extensions_by_language.setdefault("github-actions", []).extend([".github/workflows/*.yml", ".github/workflows/*.yaml"])
    extensions_by_language.setdefault("hcl", []).append("*.hcl")
    extensions_by_language.setdefault("starlark", []).extend([".bzl", ".star", "BUILD", "BUILD.bazel", "WORKSPACE"])
    extensions_by_language.setdefault("kconfig", []).append("Kconfig.*")
    languages: list[dict[str, Any]] = []
    for language, extensions in sorted(extensions_by_language.items()):
        parser = parser_registry.get(language)
        languages.append(
            {
                "language": language,
                "extensions": sorted(extensions),
                "status": "parsed" if parser else "metadata-only",
                "parser_id": parser.get("parser_id", "") if parser else "",
                "parser_version": parser.get("version", "") if parser else "",
                "structural_edges": bool(parser),
            }
        )
    return {
        "schema_version": schema_version,
        "parser_registry_digest": "",
        "language_inventory_digest": "",
        "parser_backed_count": sum(item["status"] == "parsed" for item in languages),
        "inventory_language_count": len(languages),
        "languages": languages,
        "claim": "parser-backed languages are structural; metadata-only languages are inventory-only",
    }
