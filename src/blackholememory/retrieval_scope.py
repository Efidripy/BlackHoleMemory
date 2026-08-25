"""Shared history-scope normalization for memory retrieval surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from typing import cast

HistoryScope = Literal["current", "recent", "all"]
DEFAULT_RECENT_HISTORY_DAYS = 30


@dataclass(frozen=True)
class ResolvedHistoryScope:
    """Effective, backward-compatible history and freshness filters."""

    scope: HistoryScope
    include_historical: bool
    freshness_days: int | None


def resolve_history_scope(
    history_scope: str | None,
    *,
    include_historical: bool = False,
    freshness_days: int | None = None,
) -> ResolvedHistoryScope:
    """Normalize the explicit scope and legacy boolean into one contract.

    ``include_historical`` remains an alias for ``all`` when no explicit scope
    is supplied. An explicit ``history_scope`` always wins, which lets clients
    migrate without accidentally widening a current-memory query.
    """

    if freshness_days is not None and not 1 <= int(freshness_days) <= 3650:
        raise ValueError("freshness_days must be between 1 and 3650")
    requested = str(history_scope or "").strip().casefold()
    if requested not in {"", "current", "recent", "all"}:
        raise ValueError("history_scope must be one of: current, recent, all")
    scope: HistoryScope = (
        cast(HistoryScope, requested)
        if requested
        else ("all" if include_historical else "current")
    )
    if scope == "current":
        return ResolvedHistoryScope(scope=scope, include_historical=False, freshness_days=freshness_days)
    if scope == "recent":
        return ResolvedHistoryScope(
            scope=scope,
            include_historical=True,
            freshness_days=freshness_days or DEFAULT_RECENT_HISTORY_DAYS,
        )
    return ResolvedHistoryScope(scope=scope, include_historical=True, freshness_days=freshness_days)


__all__ = ["DEFAULT_RECENT_HISTORY_DAYS", "HistoryScope", "ResolvedHistoryScope", "resolve_history_scope"]
