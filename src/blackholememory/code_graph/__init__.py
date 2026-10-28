"""SQLite-authoritative canonical code graph package."""

from ._core import (
    CODE_GRAPH_EXTRACTOR_VERSION,
    CODE_GRAPH_SCHEMA_VERSION,
    CODE_GRAPH_STORE_SCHEMA_VERSION,
    COVERAGE_POLICY_SKIP_REASONS,
    LANGUAGE_INVENTORY_DIGEST,  # noqa: F401
    PARSER_CAPABILITY_SCHEMA_VERSION,  # noqa: F401
    PARSER_REGISTRY,
    PARSER_REGISTRY_DIGEST,
    CodeGraphError,
    CodeGraphInputChangedError,
    CodeGraphLimitError,
    CodeGraphInjectedFailure,
    build_code_graph,
    extract_code_graph,
    parser_capability_matrix,  # noqa: F401
    publish_code_graph,
    verify_code_graph_snapshot,
    _LANGUAGE_BY_SUFFIX,  # noqa: F401
    _SPECIAL_TEXT_NAMES,  # noqa: F401
)  # noqa: F401
from .store import SQLiteCodeGraphStore

__all__ = [
    "CODE_GRAPH_EXTRACTOR_VERSION", "CODE_GRAPH_SCHEMA_VERSION", "CODE_GRAPH_STORE_SCHEMA_VERSION",
    "COVERAGE_POLICY_SKIP_REASONS",
    "PARSER_REGISTRY", "PARSER_REGISTRY_DIGEST", "CodeGraphError", "CodeGraphInputChangedError",
    "CodeGraphLimitError", "CodeGraphInjectedFailure", "SQLiteCodeGraphStore", "build_code_graph",
    "extract_code_graph", "publish_code_graph", "verify_code_graph_snapshot",
]
