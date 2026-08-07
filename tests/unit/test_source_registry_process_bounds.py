from __future__ import annotations

from pathlib import Path

from blackholememory import source_registry
from blackholememory.resource_limits import (
    PROCESS_EXECUTION_SOURCE_REGISTRY_CLONE_TIMEOUT_SECONDS,
    PROCESS_EXECUTION_SOURCE_REGISTRY_FETCH_TIMEOUT_SECONDS,
    PROCESS_EXECUTION_SOURCE_REGISTRY_GIT_TIMEOUT_SECONDS,
)


REVISION = "0123456789abcdef0123456789abcdef01234567"


def _source() -> dict[str, object]:
    return {
        "id": "fixture.source",
        "slug": "fixture-source",
        "name": "fixture/source",
        "source_url": "https://example.invalid/fixture.git",
        "source_type": "git",
        "revision": REVISION,
        "license": "MIT",
        "license_status": "permissive",
        "attribution": "fixture",
        "purpose": "test fixture",
        "evidence_class": "reference",
        "disposition": "reference-only",
        "allowed_use": "tests",
        "reviewer": "test",
        "recheck_date": "2099-01-01",
    }


def test_source_registry_local_git_probe_uses_registry_timeout(monkeypatch, tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    class Result:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(*_args: object, **kwargs: object) -> Result:
        calls.append(kwargs)
        return Result()

    monkeypatch.setattr(source_registry.subprocess, "run", fake_run)
    assert source_registry._run_git(["status"], cwd=tmp_path) == "ok"
    assert calls[-1]["timeout"] == PROCESS_EXECUTION_SOURCE_REGISTRY_GIT_TIMEOUT_SECONDS


def test_source_registry_clone_and_fetch_use_operation_specific_bounds(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[list[str], int]] = []
    head_calls = 0

    def fake_git(
        args: list[str], *, cwd: Path | None = None, timeout: int = PROCESS_EXECUTION_SOURCE_REGISTRY_GIT_TIMEOUT_SECONDS
    ) -> str:
        nonlocal head_calls
        calls.append((args, timeout))
        if args[0] == "clone":
            checkout = Path(args[-1])
            (checkout / ".git").mkdir(parents=True)
            (checkout / "LICENSE").write_text("MIT License\n", encoding="utf-8")
            return ""
        if args[:2] == ["rev-parse", "HEAD"]:
            head_calls += 1
            return REVISION
        if args[0] in {"ls-tree", "ls-files"}:
            return "LICENSE\0"
        return ""

    monkeypatch.setattr(source_registry, "_run_git", fake_git)
    manifest = source_registry.sync_git_source(_source(), tmp_path)

    assert manifest["acquisition_status"] == "acquired"
    assert next(timeout for args, timeout in calls if args and args[0] == "clone") == PROCESS_EXECUTION_SOURCE_REGISTRY_CLONE_TIMEOUT_SECONDS
    assert all(timeout == PROCESS_EXECUTION_SOURCE_REGISTRY_GIT_TIMEOUT_SECONDS for args, timeout in calls if args[0] not in {"clone", "fetch"})

    checkout = tmp_path / "fixture-source" / "source"
    calls.clear()
    head_calls = 0
    (checkout / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    manifest = source_registry.sync_git_source(_source(), tmp_path, refresh=True)
    assert manifest["acquisition_status"] == "acquired"
    assert any(args[0] == "fetch" and timeout == PROCESS_EXECUTION_SOURCE_REGISTRY_FETCH_TIMEOUT_SECONDS for args, timeout in calls)
