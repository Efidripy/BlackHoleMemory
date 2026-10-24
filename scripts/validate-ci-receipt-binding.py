"""Create a fail-closed CI receipt bound to the checked Git revision.

The receipt is intentionally small and metadata-only. It does not inspect or
copy source contents, and it never changes the repository except for the
explicit output path requested by CI.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from blackholememory.filesystem_boundaries import replace_bytes_safely


SCHEMA_VERSION = "bhm.ci.receipt-binding.v1"
GIT_TIMEOUT_SECONDS = 30
TOOL_TIMEOUT_SECONDS = 15


class ReceiptBindingError(RuntimeError):
    """Raised when CI metadata cannot be bound to the checked revision."""


def _write_receipt(path: Path, receipt: Mapping[str, Any]) -> None:
    rendered = json.dumps(receipt, ensure_ascii=False, indent=2) + "\n"
    replace_bytes_safely(path, rendered.encode("utf-8"))


def _run(command: list[str], *, timeout: float) -> str:
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReceiptBindingError(f"command failed: {command[0]}") from exc
    return completed.stdout.strip()


def _git_state(root: Path) -> dict[str, Any]:
    return {
        "head_sha": _run(["git", "-C", str(root), "rev-parse", "HEAD"], timeout=GIT_TIMEOUT_SECONDS),
        "tree_sha": _run(["git", "-C", str(root), "rev-parse", "HEAD^{tree}"], timeout=GIT_TIMEOUT_SECONDS),
        "status_porcelain": _run(["git", "-C", str(root), "status", "--porcelain"], timeout=GIT_TIMEOUT_SECONDS),
    }


def _tool_version(command: list[str]) -> str:
    try:
        return _run(command, timeout=TOOL_TIMEOUT_SECONDS)
    except ReceiptBindingError:
        return "unavailable"


def _validate_sha(value: str, label: str) -> str:
    normalized = str(value or "").strip().lower()
    if len(normalized) != 40 or any(char not in "0123456789abcdef" for char in normalized):
        raise ReceiptBindingError(f"{label} must be a full 40-character hexadecimal Git SHA")
    return normalized


def build_receipt(
    *,
    expected_sha: str,
    observed_sha: str,
    tree_sha: str,
    dirty: bool,
    tool_versions: Mapping[str, str],
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    expected = _validate_sha(expected_sha, "expected_sha")
    observed = _validate_sha(observed_sha, "observed_sha")
    tree = str(tree_sha or "").strip().lower()
    if len(tree) != 40 or any(char not in "0123456789abcdef" for char in tree):
        raise ReceiptBindingError("tree_sha must be a full 40-character hexadecimal Git tree SHA")
    environment = environment or {}
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "binding": {
            "expected_sha": expected,
            "observed_sha": observed,
            "sha_match": expected == observed,
            "tree_sha": tree,
            "worktree_clean": not dirty,
        },
        "workflow": {
            "repository": str(environment.get("GITHUB_REPOSITORY") or ""),
            "workflow": str(environment.get("GITHUB_WORKFLOW") or ""),
            "run_id": str(environment.get("GITHUB_RUN_ID") or ""),
            "event": str(environment.get("GITHUB_EVENT_NAME") or ""),
        },
        "tool_versions": dict(sorted((str(key), str(value)) for key, value in tool_versions.items())),
        "execution": {
            "writes_sqlite_state": False,
            "writes_qdrant": False,
            "writes_source": False,
            "network_used": False,
        },
    }
    return receipt


def validate_receipt(receipt: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    if receipt.get("schema_version") != SCHEMA_VERSION:
        failures.append("schema_version mismatch")
    binding = receipt.get("binding") if isinstance(receipt.get("binding"), Mapping) else {}
    if binding.get("sha_match") is not True:
        failures.append("expected and observed Git SHA differ")
    if binding.get("worktree_clean") is not True:
        failures.append("worktree is not clean")
    execution = receipt.get("execution") if isinstance(receipt.get("execution"), Mapping) else {}
    for key in ("writes_sqlite_state", "writes_qdrant", "writes_source", "network_used"):
        if execution.get(key) is not False:
            failures.append(f"execution.{key} must be false")
    tool_versions = receipt.get("tool_versions") if isinstance(receipt.get("tool_versions"), Mapping) else {}
    for key in ("python", "ruff", "uv"):
        value = str(tool_versions.get(key) or "").strip()
        if not value or value.casefold() == "unavailable":
            failures.append(f"tool_versions.{key} is unavailable")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--expected-sha", default=os.environ.get("GITHUB_SHA") or os.environ.get("CI_COMMIT_SHA"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    try:
        state = _git_state(args.repo.resolve())
        if not args.expected_sha:
            raise ReceiptBindingError("expected SHA is required; pass --expected-sha or set GITHUB_SHA")
        receipt = build_receipt(
            expected_sha=args.expected_sha,
            observed_sha=state["head_sha"],
            tree_sha=state["tree_sha"],
            dirty=bool(state["status_porcelain"]),
            tool_versions={
                "python": _tool_version([sys.executable, "--version"]),
                "ruff": _tool_version(["ruff", "--version"]),
                "uv": _tool_version(["uv", "--version"]),
            },
            environment=os.environ,
        )
        failures = validate_receipt(receipt)
        if failures:
            raise ReceiptBindingError("; ".join(failures))
        _write_receipt(args.output.resolve(), receipt)
    except ReceiptBindingError as exc:
        print(f"CI receipt binding failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
