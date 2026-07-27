from __future__ import annotations

import pytest

from blackholememory.project_registry import ProjectDefinition
from blackholememory.project_registry import ProjectRegistry
from blackholememory.project_registry import ProjectRegistryError
from blackholememory.project_registry import get_default_project_registry
from blackholememory.project_registry import normalize_project_key
from blackholememory import bhm_mcp


def test_default_registry_resolves_confirmed_aliases_without_rewriting_storage():
    registry = get_default_project_registry()

    resolution = registry.resolve("BlackHoleMemory")

    assert resolution.canonical == "blackholememory"
    assert resolution.known is True
    assert "BlackHoleMemory" in resolution.accepted_values
    assert registry.canonicalize("BlackHoleMemory") == "blackholememory"


def test_unknown_project_is_normalized_but_not_collapsed_into_known_project():
    registry = get_default_project_registry()

    resolution = registry.resolve("Team Alpha / Service")

    assert resolution.known is False
    assert resolution.canonical == "team-alpha-service"
    assert resolution.canonical != "blackholememory"


def test_registry_rejects_ambiguous_aliases():
    with pytest.raises(ProjectRegistryError, match="ambiguous project alias"):
        ProjectRegistry(
            (
                ProjectDefinition("one", "One", ("shared",)),
                ProjectDefinition("two", "Two", ("shared",)),
            )
        )


def test_project_key_normalization_is_deterministic():
    assert normalize_project_key(" Local Service ") == "local-service"
    assert normalize_project_key("BlackHoleMemory") == "blackholememory"


def test_project_resolve_mcp_wrapper_uses_canonical_rest_route(monkeypatch):
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        bhm_mcp,
        "_get",
        lambda path, params: calls.append((path, params)) or {"ok": True},
    )

    assert bhm_mcp.bhm_project_resolve("BlackHoleMemory") == {"ok": True}
    assert calls == [("/bhm/project/resolve", {"project": "BlackHoleMemory"})]
