"""Fail-closed validation of the BHM public/local repository boundary."""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
from pathlib import Path


GIT_PROBE_TIMEOUT_SECONDS = 30


def load_manifest(repo: Path) -> dict:
    return json.loads((repo / "config/public-tree-manifest.json").read_text(encoding="utf-8"))


def is_local(relative: str, manifest: dict) -> bool:
    if set(Path(relative).parts) & set(manifest["local_roots"]):
        return True
    return any(fnmatch.fnmatch(relative, pattern) for pattern in manifest["local_globs"])


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=GIT_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def validate(repo: Path, *, staged: bool = False) -> dict:
    repo = repo.resolve()
    manifest = load_manifest(repo)
    failures: list[str] = []
    required = [str(path) for path in manifest["required_files"]]
    failures.extend(f"missing required public file: {path}" for path in required if not (repo / path).is_file())
    if staged:
        result = _run_git(repo, "diff", "--cached", "--name-only")
        if result is None or result.returncode != 0:
            failures.append("staged public-tree Git probe unavailable")
            paths = []
        else:
            paths = [line.replace("\\", "/") for line in result.stdout.splitlines() if line.strip()]
    else:
        paths = [path.relative_to(repo).as_posix() for path in repo.rglob("*") if path.is_file()]
    checked = 0
    local_skipped = 0
    for relative in sorted(set(paths)):
        if relative == ".git" or relative.startswith(".git/"):
            continue
        if is_local(relative, manifest):
            local_skipped += 1
            continue
        parts = Path(relative).parts
        if len(parts) > 1 and parts[0] not in set(manifest["public_roots"]):
            failures.append(f"unclassified public path: {relative}")
            continue
        checked += 1
        if any(fnmatch.fnmatch(relative, pattern) for pattern in manifest["forbidden_public_globs"]):
            failures.append(f"forbidden public artifact: {relative}")
        if (repo / relative).is_symlink():
            failures.append(f"symlink in public tree: {relative}")
    for local_root in manifest["local_roots"]:
        probe = f"{local_root}/__public_boundary_probe__"
        check = _run_git(repo, "check-ignore", "--no-index", "--quiet", "--", probe)
        if check is None or check.returncode != 0:
            failures.append(f"local root is not ignored: {local_root}")
    return {"ok": not failures, "repo": str(repo), "mode": "staged" if staged else "worktree", "checked_public_files": checked, "skipped_local_files": local_skipped, "required_files": len(required), "failures": failures}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--staged", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = validate(args.repo, staged=args.staged)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
