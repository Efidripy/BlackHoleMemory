"""Portable, deterministic BHM operating guidance for MCP agents.

The module is intentionally pure.  It contains no project content and never
reads or writes SQLite, Qdrant, runtime state, files, HTTP, or MCP transports.
It only tells an already attached agent which existing read-only BHM tools may
be relevant to a declared task kind and where the authority boundaries are.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any


SCHEMA_VERSION = "bhm.agent-playbook.v1"


class AgentTaskKind(StrEnum):
    """Declared work shape used to select a small relevant tool set."""

    EXPLORATION = "exploration"
    IMPLEMENTATION = "implementation"
    CODE_CHANGE = "code_change"
    INCIDENT = "incident"
    RELEASE = "release"
    HANDOFF = "handoff"


class PlaybookLanguage(StrEnum):
    """Supported language for human-readable recommendations."""

    EN = "en"
    RU = "ru"


_TOOL_PLANS: dict[AgentTaskKind, tuple[tuple[str, str], ...]] = {
    AgentTaskKind.EXPLORATION: (
        ("bhm_context_compile", "Compile bounded project context with citations."),
        ("bhm_search", "Search current project memory for directly relevant facts."),
        ("bhm_agent_context", "Read the bounded current task package when a project is known."),
    ),
    AgentTaskKind.IMPLEMENTATION: (
        ("bhm_context_compile", "Compile cited context before selecting an implementation slice."),
        ("bhm_change_impact_preview", "Preview affected paths and tests without changing the worktree."),
        ("bhm_check_index_coverage", "Check whether graph conclusions are fresh enough to use."),
    ),
    AgentTaskKind.CODE_CHANGE: (
        ("bhm_change_impact_preview", "Preview source and test impact without changing the worktree."),
        ("bhm_context_compile", "Compile cited project context for the exact change."),
        ("bhm_check_index_coverage", "Check index freshness before relying on graph metadata."),
    ),
    AgentTaskKind.INCIDENT: (
        ("bhm_health", "Read current BHM readiness and declared authority state."),
        ("bhm_diagnostics", "Read compact dependency diagnostics before any operator action."),
        ("bhm_trace_path", "Trace a bounded graph path when graph freshness supports the claim."),
    ),
    AgentTaskKind.RELEASE: (
        ("bhm_context_compile", "Compile cited release context and recorded constraints."),
        ("bhm_change_impact_preview", "Preview affected paths and test impact without mutation."),
        ("bhm_check_index_coverage", "Treat stale graph evidence as unavailable for release claims."),
    ),
    AgentTaskKind.HANDOFF: (
        ("bhm_context_compile", "Compile a bounded cited handoff context."),
        ("bhm_get_architecture", "Read a metadata-only architecture summary."),
        ("bhm_change_impact", "Describe graph impact with explicit provenance and limits."),
    ),
}

_RU_PURPOSES = {
    "Compile bounded project context with citations.": "Собрать ограниченный контекст проекта с цитатами.",
    "Search current project memory for directly relevant facts.": "Найти в текущей памяти проекта напрямую относящиеся факты.",
    "Read the bounded current task package when a project is known.": "Прочитать ограниченный пакет текущей задачи, когда известен проект.",
    "Compile cited context before selecting an implementation slice.": "Собрать контекст с цитатами перед выбором implementation-slice.",
    "Preview affected paths and tests without changing the worktree.": "Предпросмотреть затронутые пути и тесты без изменения worktree.",
    "Check whether graph conclusions are fresh enough to use.": "Проверить, достаточно ли свеж graph для выводов.",
    "Preview source and test impact without changing the worktree.": "Предпросмотреть влияние на source и тесты без изменения worktree.",
    "Compile cited project context for the exact change.": "Собрать контекст с цитатами для точного изменения.",
    "Check index freshness before relying on graph metadata.": "Проверить свежесть индекса до использования graph metadata.",
    "Read current BHM readiness and declared authority state.": "Прочитать текущую готовность BHM и заявленное authority-состояние.",
    "Read compact dependency diagnostics before any operator action.": "Прочитать компактную диагностику зависимостей до operator-действий.",
    "Trace a bounded graph path when graph freshness supports the claim.": "Проследить ограниченный graph-путь, если свежесть графа позволяет вывод.",
    "Compile cited release context and recorded constraints.": "Собрать контекст релиза с цитатами и зафиксированными ограничениями.",
    "Treat stale graph evidence as unavailable for release claims.": "Считать stale graph evidence недоступным для release-выводов.",
    "Compile a bounded cited handoff context.": "Собрать ограниченный handoff-контекст с цитатами.",
    "Read a metadata-only architecture summary.": "Прочитать metadata-only summary архитектуры.",
    "Describe graph impact with explicit provenance and limits.": "Описать graph impact с явными provenance и ограничениями.",
}

_COPY = {
    PlaybookLanguage.EN: {
        "catalog": "Inspect the actual native MCP catalog; configured client state and REST health do not prove this chat has attached tools.",
        "health": "When native BHM tools are attached, call bhm_health once as the first BHM action.",
        "no_repair": "If BHM is unavailable, do not self-repair MCP attachment, configuration, credentials, or runtime.",
        "freshness": "Git, source, tests, and canonical documentation outrank stale or incomplete BHM index evidence.",
        "persistent_default": "forbidden_without_explicit_authorization",
        "persistent_requires": "Persistent lifecycle, feedback, slot, task, or memory actions require an explicit Task Brief and owner authorization.",
        "fallback": "Continue with Git, source, tests, and canonical documentation; record that BHM evidence was unavailable.",
        "evidence": "Record used BHM evidence, stale or unavailable signals, and independent source/test evidence in the handoff.",
    },
    PlaybookLanguage.RU: {
        "catalog": "Проверьте actual native MCP catalog: configured client state и REST health не доказывают, что tools attached именно к этому чату.",
        "health": "Если native BHM tools attached, вызовите bhm_health ровно один раз первым BHM-действием.",
        "no_repair": "Если BHM недоступна, не чините самостоятельно MCP attach, config, credentials или runtime.",
        "freshness": "Git, source, tests и canonical documentation сильнее stale или incomplete BHM index evidence.",
        "persistent_default": "запрещено_без_явного_разрешения",
        "persistent_requires": "Persistent lifecycle, feedback, slot, task или memory actions требуют explicit Task Brief и owner authorization.",
        "fallback": "Продолжайте по Git, source, tests и canonical documentation; зафиксируйте, что BHM evidence недоступен.",
        "evidence": "Зафиксируйте в handoff использованный BHM evidence, stale/unavailable signals и независимый source/test evidence.",
    },
}


def _canonical_contract() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "task_kinds": {
            kind.value: [{"tool": tool, "purpose": purpose} for tool, purpose in plan]
            for kind, plan in sorted(_TOOL_PLANS.items(), key=lambda item: item[0].value)
        },
        "languages": {
            language.value: dict(sorted(copy.items()))
            for language, copy in sorted(_COPY.items(), key=lambda item: item[0].value)
        },
        "execution": {
            "writes_sqlite": False,
            "writes_qdrant": False,
            "writes_runtime": False,
            "writes_client_files": False,
        },
        "persistent_actions": {
            "default": "forbidden_without_explicit_authorization",
            "requires": ["explicit Task Brief", "owner authorization"],
        },
    }


def playbook_contract_digest() -> str:
    """Return the stable digest of the entire public guidance contract."""

    encoded = json.dumps(_canonical_contract(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_agent_playbook(
    *,
    task_kind: AgentTaskKind | str = AgentTaskKind.IMPLEMENTATION,
    language: PlaybookLanguage | str = PlaybookLanguage.EN,
) -> dict[str, Any]:
    """Build portable guidance without inspecting BHM state or project content."""

    normalized_kind = AgentTaskKind(task_kind)
    normalized_language = PlaybookLanguage(language)
    copy = _COPY[normalized_language]
    recommendations = []
    for tool, english_purpose in _TOOL_PLANS[normalized_kind]:
        recommendations.append(
            {
                "tool": tool,
                "purpose": _RU_PURPOSES[english_purpose]
                if normalized_language is PlaybookLanguage.RU
                else english_purpose,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "contract_digest": playbook_contract_digest(),
        "task_kind": normalized_kind.value,
        "language": normalized_language.value,
        "supported_task_kinds": [item.value for item in AgentTaskKind],
        "supported_languages": [item.value for item in PlaybookLanguage],
        "execution": {
            "writes_sqlite": False,
            "writes_qdrant": False,
            "writes_runtime": False,
            "writes_client_files": False,
        },
        "native_attach_rule": {
            "catalog_primary": True,
            "health_once_if_attached": True,
            "no_self_repair": True,
            "steps": [copy["catalog"], copy["health"], copy["no_repair"]],
        },
        "recommended_read_only_tools": recommendations,
        "freshness_boundary": copy["freshness"],
        "persistent_actions": {
            "default": copy["persistent_default"],
            "requires": ["explicit Task Brief", "owner authorization"],
            "examples": ["bhm_memory_used", "bhm_task_open", "bhm_task_close", "bhm_slot_set"],
        },
        "evidence_output_template": {
            "used_bhm_evidence": [],
            "stale_or_unavailable_signals": [],
            "source_and_test_evidence": [],
            "instruction": copy["evidence"],
        },
        "fallback_when_mcp_unavailable": [copy["fallback"]],
    }


__all__ = [
    "AgentTaskKind",
    "PlaybookLanguage",
    "SCHEMA_VERSION",
    "build_agent_playbook",
    "playbook_contract_digest",
]
