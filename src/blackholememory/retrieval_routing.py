"""Query routing classifier for BHM memory search.

Determines whether a user query is best answered by BHM memory,
code graph / structural index, git history, or documentation.
Pure deterministic regex-based classifier without network or LLM dependencies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

SOURCE_CODE_GRAPH = "code_graph"
SOURCE_GIT = "git"
SOURCE_DOCS = "docs"
SOURCE_MEMORY = "memory"

INTENT_CODE_GRAPH = "code_structure"
INTENT_GIT = "git_history"
INTENT_DOCS = "documentation"
INTENT_MEMORY = "memory_search"

CONFIDENCE_HIGH = "high"
CONFIDENCE_LOW = "low"

CODE_GRAPH_TOOLS = [
    "bhm_get_architecture",
    "bhm_search_code",
    "bhm_search_graph",
    "bhm_trace_path",
]

GIT_TOOLS = [
    "git log",
    "git blame",
]

DOCS_TOOLS = [
    "bhm_search_code (mode docs)",
    "чтение docs репозитория",
]

MEMORY_TOOLS = [
    "bhm_search",
]

HINT_CODE_GRAPH = (
    "Вопрос про структуру кода: используйте bhm_get_architecture или "
    "bhm_search_code, память отвечает про решения и грабли"
)

HINT_GIT = (
    "Вопрос про историю изменений: используйте git log или git blame, "
    "память отвечает про решения и грабли"
)

HINT_DOCS = (
    "Вопрос по документации: используйте bhm_search_code (mode docs) или "
    "чтение docs репозитория, память отвечает про решения и грабли"
)

_FILE_EXT_RE = re.compile(
    r"(?i)\b[\w\-]+(?:\.[\w\-]+)*\.(?:py|cs|ps1|js|ts|json)\b"
)

_CODE_WORDS_RE = re.compile(
    r"(?i)\b(?:функци\w*|класс\w*|метод\w*|модул\w*|файл\w*|functions?|class(?:es)?|methods?|modules?|files?)\b"
)

_SNAKE_CASE_RE = re.compile(
    r"(?i)\b(?=[a-z0-9_]*[a-z])[a-z0-9]+(?:_[a-z0-9]+)+\b"
)

_CAMEL_CASE_RE = re.compile(
    r"\b(?:[a-zA-Z0-9]*[a-z][A-Z][a-zA-Z0-9]*|[A-Z]{2,}[a-z][a-zA-Z0-9]*)\b"
)

_DOTTED_NAME_RE = re.compile(
    r"\b[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]+)+\b"
)

_CODE_GRAPH_PATTERNS = [
    re.compile(r"(?i)\b(?:структур\w*\s+(?:у\s+|в\s+)?(?:проекта|кода)|(?:project|code)\s+structure)\b"),
    re.compile(r"(?i)\b(?:архитектур\w*|architectur(?:e|al))\b"),
    re.compile(r"(?i)\b(?:как\s+устроен\w*(?:\s+\w+){0,3}\s+проект\w*|как\s+устроен\w*\s+код\b|how\s+(?:is\s+)?(?:the\s+)?project\s+(?:structured|organized|built))\b"),
    re.compile(r"(?i)\b(?:где\s+в\s+коде|where\s+in\s+(?:the\s+)?code)\b"),
    re.compile(r"(?i)\b(?:где\s+реализован\w*|where\s+(?:is|are)\b.*?\b(?:implemented|realized)\b)"),
    re.compile(r"(?i)\b(?:в\s+как(?:ом|их)\s+файл\w*|(?:in\s+)?which\s+file\b|\bwhat\s+file\b)"),
    re.compile(r"(?i)\b(?:как(?:ая|ой|ие|ую|ом)\s+(?:функци\w*|класс\w*|метод\w*)|(?:which|what)\s+(?:function|class|method)\b)"),
    re.compile(r"(?i)\b(?:кто\s+вызывает|who\s+calls\b)"),
    re.compile(r"(?i)\b(?:что\s+вызывает|what\s+calls\b)"),
    re.compile(r"(?i)\b(?:(?:зависимост\w*|импорт\w*)\s+(?:модул\w*|проекта)|(?:модул\w*)\s+(?:зависимост\w*|импорт\w*)|module\s+(?:dependencies|imports)|(?:dependencies|imports)\s+of\s+(?:the\s+)?module)\b"),
    re.compile(r"(?i)\b(?:граф\w*\s+вызовов|call\s*graphs?)\b"),
]

_GIT_PATTERNS = [
    re.compile(r"(?i)\b(?:последн\w*\s+коммит\w*|(?:last|latest)\s+commit)\b"),
    re.compile(r"(?i)\b(?:кто\s+(?:изменил\w*|менял\w*|поменял\w*|редактировал\w*|правил\w*)|who\s+(?:changed|modified|edited|touched))\b"),
    re.compile(r"(?i)\b(?:когда\s+(?:мы\s+)?(?:последний\s+раз\s+)?(?:изменили|изменил\w*|добавили|добавил\w*|поменяли|поменял\w*|менял\w*)|when\s+(?:was|were)\b.*?\b(?:changed|modified|added)\b|when\s+did\b.*?\b(?:change|modify|add)\b)"),
    re.compile(r"(?i)\b(?:истори\w*\s+изменений|(?:change|commit)\s+history|history\s+of\s+changes)\b"),
    re.compile(r"(?i)\bgit\s+log\b"),
    re.compile(r"(?i)\b(?:git\s+)?blame\b"),
    re.compile(r"(?i)\b(?:git\s+)?diff\b"),
    re.compile(r"(?i)\b(?:как(?:ой|ом|ого|им)\s+коммит\w*|(?:which|what)\s+commit)\b"),
    re.compile(r"(?i)\b(?:в\s+как(?:ой|ом)\s+(?:верси\w*|релиз\w*)|когда\s+(?:\w+\s+){0,4}появил\w*\s+в\s+(?:верси\w*|релиз\w*)|(?:in\s+)?(?:which|what)\s+(?:version|release)\b.*?\b(?:introduced|added|released)\b|(?:which|what)\s+(?:version|release)\b)"),
]

_DOCS_PATTERNS = [
    re.compile(r"(?i)\breadme(?:\.md)?\b"),
    re.compile(r"(?i)\brunbooks?\b"),
    re.compile(r"(?i)\badr(?:s|-\d+)?\b"),
    re.compile(r"(?i)\b(?:документац\w*|documentation|docs)\b"),
    re.compile(r"(?i)\b(?:инструкци\w*(?:\s+\w+){0,3}\s+из\s+репозитори\w*|инструкци\w*|instructions?\s+(?:from|in)\s+(?:the\s+)?repo(?:sitory)?)\b"),
    re.compile(r"(?i)\bchangelogs?\b"),
]


@dataclass(frozen=True)
class QueryRoutingResult:
    suggested_source: str
    intent: str
    confidence: str
    reason: str
    tools: list[str]
    hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "suggested_source": self.suggested_source,
            "intent": self.intent,
            "confidence": self.confidence,
            "reason": self.reason,
            "tools": list(self.tools),
        }
        if self.hint:
            result["hint"] = self.hint
        return result


def _matches_code_graph(query: str) -> bool:
    if _FILE_EXT_RE.search(query):
        return True
    if any(pattern.search(query) for pattern in _CODE_GRAPH_PATTERNS):
        return True
    if _CODE_WORDS_RE.search(query) and (
        _SNAKE_CASE_RE.search(query)
        or _CAMEL_CASE_RE.search(query)
        or _DOTTED_NAME_RE.search(query)
    ):
        return True
    return False


def _matches_git(query: str) -> bool:
    return any(pattern.search(query) for pattern in _GIT_PATTERNS)


def _matches_docs(query: str) -> bool:
    return any(pattern.search(query) for pattern in _DOCS_PATTERNS)


def classify_query(query: str) -> dict[str, Any]:
    """Classify user query into suggested retrieval source and intent metadata."""
    text = (query or "").strip()
    if not text:
        return QueryRoutingResult(
            suggested_source=SOURCE_MEMORY,
            intent=INTENT_MEMORY,
            confidence=CONFIDENCE_LOW,
            reason="empty_query",
            tools=MEMORY_TOOLS,
            hint="",
        ).to_dict()

    matched: list[str] = []
    if _matches_code_graph(text):
        matched.append(SOURCE_CODE_GRAPH)
    if _matches_git(text):
        matched.append(SOURCE_GIT)
    if _matches_docs(text):
        matched.append(SOURCE_DOCS)

    if not matched:
        return QueryRoutingResult(
            suggested_source=SOURCE_MEMORY,
            intent=INTENT_MEMORY,
            confidence=CONFIDENCE_LOW,
            reason="default_fallback",
            tools=MEMORY_TOOLS,
            hint="",
        ).to_dict()

    if len(matched) == 1:
        source = matched[0]
        confidence = CONFIDENCE_HIGH
        reason = f"{source}_match"
    else:
        # Precedence: code_graph > git > docs
        for candidate in (SOURCE_CODE_GRAPH, SOURCE_GIT, SOURCE_DOCS):
            if candidate in matched:
                source = candidate
                break
        confidence = CONFIDENCE_LOW
        reason = "multi_source_conflict"

    intents = {
        SOURCE_CODE_GRAPH: INTENT_CODE_GRAPH,
        SOURCE_GIT: INTENT_GIT,
        SOURCE_DOCS: INTENT_DOCS,
    }
    tools_map = {
        SOURCE_CODE_GRAPH: CODE_GRAPH_TOOLS,
        SOURCE_GIT: GIT_TOOLS,
        SOURCE_DOCS: DOCS_TOOLS,
    }
    hints_map = {
        SOURCE_CODE_GRAPH: HINT_CODE_GRAPH,
        SOURCE_GIT: HINT_GIT,
        SOURCE_DOCS: HINT_DOCS,
    }

    return QueryRoutingResult(
        suggested_source=source,
        intent=intents[source],
        confidence=confidence,
        reason=reason,
        tools=tools_map[source],
        hint=hints_map[source],
    ).to_dict()
