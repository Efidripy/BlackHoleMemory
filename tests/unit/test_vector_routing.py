from __future__ import annotations

from blackholememory.vector_routing import normalize_vector_targets
from blackholememory.vector_routing import route_vector_targets


def _record(**overrides):
    record = {
        "project": "blackholememory",
        "memory_type": "workflow",
        "content": "A short project note.",
        "tags": [],
        "files": [],
        "metadata": {},
    }
    record.update(overrides)
    return record


def test_normalize_vector_targets_keeps_only_supported_contours():
    assert normalize_vector_targets("local+global|unknown") == ("local", "global")
    assert normalize_vector_targets(["GLOBAL", "local", "global"]) == ("global", "local")
    assert set(normalize_vector_targets({"local", "global"})) == {"local", "global"}
    assert normalize_vector_targets(None) == ()


def test_explicit_routing_metadata_is_authoritative_and_local_first():
    decision = route_vector_targets(
        _record(
            content="A path C:\\repo\\src\\app.py should stay project-local.",
            metadata={"vector_scope": "global"},
        )
    )

    assert decision.targets == ("local", "global")
    assert decision.explicit is True
    assert decision.reason_codes == ("explicit_vector_targets",)


def test_global_scope_routes_reusable_system_knowledge():
    decision = route_vector_targets(
        _record(
            memory_type="architecture",
            content="Qdrant and Mem0 form a shared invariant for all projects.",
            metadata={"scope": "global", "domain": "infra", "semantic_type": "architecture"},
        )
    )

    assert decision.targets == ("local", "global")
    assert "scope_global" in decision.reason_codes
    assert "global_confident" in decision.reason_codes


def test_reusable_taxonomy_and_content_can_route_without_scope_keyword():
    decision = route_vector_targets(
        _record(
            memory_type="architecture",
            content="Cross-project reusable guidance: keep the shared invariant stable.",
            metadata={"domain": "general", "semantic_type": "knowledge"},
        )
    )

    assert decision.targets == ("local", "global")
    assert decision.global_score >= 3.0


def test_project_code_and_source_refs_remain_local_even_with_system_terms():
    decision = route_vector_targets(
        _record(
            memory_type="bugfix",
            content="Fix def _sync_mem0_record() in src/blackholememory/app.py after a Qdrant timeout.",
            files=["src/blackholememory/app.py"],
            metadata={"domain": "backend", "semantic_type": "bugfix", "source_refs": ["src/blackholememory/app.py"]},
        )
    )

    assert decision.targets == ("local",)
    assert "source_local_path" in decision.reason_codes
    assert "source_refs_present" in decision.reason_codes


def test_ambiguous_records_fail_safe_to_local():
    decision = route_vector_targets(_record(content="A note from today's meeting."))

    assert decision.targets == ("local",)
    assert decision.reason_codes[-1] == "local_safe_default"
