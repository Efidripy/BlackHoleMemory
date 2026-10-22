"""Snapshot delta calculations, file digest calculation, and snapshot verification helpers."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .repository_index_types import (
    REPOSITORY_INDEX_SCHEMA_VERSION,
    _coverage_relevant_skip,
    _sha256_json,
)


def _build_delta(previous_files: Sequence[Mapping[str, Any]], files: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    previous = {str(item["path"]): dict(item) for item in previous_files}
    current = {str(item["path"]): dict(item) for item in files}
    removed = sorted(set(previous) - set(current))
    added = sorted(set(current) - set(previous))
    changed = sorted(
        path
        for path in set(previous) & set(current)
        if previous[path]["content_sha256"] != current[path]["content_sha256"]
    )
    previous_by_hash: dict[str, list[str]] = {}
    current_by_hash: dict[str, list[str]] = {}
    for path in removed:
        previous_by_hash.setdefault(str(previous[path]["content_sha256"]), []).append(path)
    for path in added:
        current_by_hash.setdefault(str(current[path]["content_sha256"]), []).append(path)
    renamed: list[dict[str, str]] = []
    renamed_from: set[str] = set()
    renamed_to: set[str] = set()
    for digest in sorted(set(previous_by_hash) & set(current_by_hash)):
        old_paths = sorted(previous_by_hash[digest])
        new_paths = sorted(current_by_hash[digest])
        if len(old_paths) == 1 and len(new_paths) == 1:
            renamed.append({"from": old_paths[0], "to": new_paths[0], "content_sha256": digest})
            renamed_from.add(old_paths[0])
            renamed_to.add(new_paths[0])
    added = [path for path in added if path not in renamed_to]
    removed = [path for path in removed if path not in renamed_from]
    unchanged_count = sum(
        previous[path]["content_sha256"] == current[path]["content_sha256"]
        for path in set(previous) & set(current)
    )
    return {
        "added": added,
        "changed": changed,
        "removed": removed,
        "renamed": renamed,
        "unchanged_count": unchanged_count,
        "changed_file_count": len(added) + len(changed) + len(removed) + len(renamed),
    }


def _snapshot_digest_files(files: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            key: item.get(key)
            for key in (
                "path",
                "content_sha256",
                "size_bytes",
                "line_count",
                "language",
                "file_kind",
                "origin",
            )
        }
        for item in files
    ]


def _build_snapshot_summary(files: Sequence[Mapping[str, Any]], skips: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    languages: dict[str, int] = {}
    kinds: dict[str, int] = {}
    skip_reasons: dict[str, int] = {}
    relevant_skip_reasons: dict[str, int] = {}
    for item in files:
        language = str(item["language"])
        kind = str(item["file_kind"])
        languages[language] = languages.get(language, 0) + 1
        kinds[kind] = kinds.get(kind, 0) + 1
    for item in skips:
        reason = str(item["reason"])
        skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
        if _coverage_relevant_skip(str(item.get("path") or ""), reason):
            relevant_skip_reasons[reason] = relevant_skip_reasons.get(reason, 0) + 1
    relevant_skipped_count = sum(relevant_skip_reasons.values())
    repository_file_count = len(files) + relevant_skipped_count
    return {
        "file_count": len(files),
        "repository_file_count": repository_file_count,
        "indexed_relevant_file_count": len(files),
        "relevant_skipped_count": relevant_skipped_count,
        "coverage_ratio": round(len(files) / max(repository_file_count, 1), 6),
        "coverage_complete": relevant_skipped_count == 0,
        "relevant_skip_reasons": dict(sorted(relevant_skip_reasons.items())),
        "total_bytes": sum(int(item["size_bytes"]) for item in files),
        "total_lines": sum(int(item["line_count"]) for item in files),
        "skipped_count": len(skips),
        "languages": dict(sorted(languages.items())),
        "file_kinds": dict(sorted(kinds.items())),
        "skip_reasons": dict(sorted(skip_reasons.items())),
    }


def verify_repository_snapshot(snapshot: Mapping[str, Any]) -> bool:
    """Verify a snapshot loaded with ``include_files=True``."""

    files = snapshot.get("files")
    skips = snapshot.get("skips")
    source = snapshot.get("source")
    if not isinstance(files, list) or not isinstance(skips, list) or not isinstance(source, dict):
        return False
    core = {
        "schema_version": REPOSITORY_INDEX_SCHEMA_VERSION,
        "project": snapshot.get("project"),
        "root_id": snapshot.get("root_id"),
        "config_digest": snapshot.get("config_digest"),
        "git_head": snapshot.get("git_head"),
        "git_branch": snapshot.get("git_branch"),
        "dirty": bool(snapshot.get("dirty")),
        "git_status_sha256": snapshot.get("git_status_sha256"),
        "state_digest": snapshot.get("state_digest"),
        "candidate_digest": snapshot.get("candidate_digest"),
        "source": source,
        "files": _snapshot_digest_files(files),
        "skips": [
            {key: item.get(key) for key in ("path", "reason", "size_bytes")}
            for item in skips
        ],
    }
    return str(snapshot.get("snapshot_digest") or "") == _sha256_json(core)

