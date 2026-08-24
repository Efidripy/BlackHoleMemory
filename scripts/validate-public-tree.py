"""Fail-closed validation of the BHM public/local repository boundary."""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
from pathlib import Path


GIT_PROBE_TIMEOUT_SECONDS = 30
PUBLIC_SCRIPT_MANIFEST = "config/public-script-manifest.json"


def load_manifest(repo: Path) -> dict:
    return json.loads((repo / "config/public-tree-manifest.json").read_text(encoding="utf-8"))


def load_public_script_manifest(repo: Path) -> dict:
    return json.loads((repo / PUBLIC_SCRIPT_MANIFEST).read_text(encoding="utf-8"))


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


def validate_public_script_manifest(repo: Path) -> dict[str, object]:
    """Require the public-source registry to classify every tracked root script.

    The registry distinguishes public source from the launcher payload.  A
    script may remain visible for contributors and CI with ``release: false``;
    it must still be classified instead of silently becoming an undocumented
    public file.
    """

    failures: list[str] = []
    try:
        document = load_public_script_manifest(repo)
    except (OSError, json.JSONDecodeError) as exc:
        return {"failures": [f"public script manifest unreadable: {exc}"], "tracked": 0, "listed": 0}
    entries = document.get("entries") if isinstance(document, dict) else None
    if not isinstance(document, dict) or document.get("schema_version") != "bhm.public-script-manifest.v1":
        return {"failures": ["public script manifest schema is invalid"], "tracked": 0, "listed": 0}
    if not isinstance(entries, list) or not entries:
        return {"failures": ["public script manifest contains no entries"], "tracked": 0, "listed": 0}

    listed: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            failures.append("public script manifest contains a non-object entry")
            continue
        path = str(entry.get("path") or "").replace("\\", "/")
        pure = Path(path)
        if (
            not path.startswith("scripts/")
            or path.endswith("/")
            or path != pure.as_posix()
            or ".." in pure.parts
            or pure.is_absolute()
        ):
            failures.append(f"public script manifest has unsafe path: {path!r}")
            continue
        if not path.endswith((".py", ".ps1")):
            failures.append(f"public script manifest has unsupported script type: {path}")
        if path in listed:
            failures.append(f"public script manifest has duplicate path: {path}")
        listed.add(path)
        if not isinstance(entry.get("role"), str) or not str(entry["role"]).strip():
            failures.append(f"public script manifest has no role: {path}")
        if not isinstance(entry.get("release"), bool):
            failures.append(f"public script manifest has invalid release flag: {path}")

    probe = _run_git(repo, "ls-files", "--", "scripts")
    if probe is None or probe.returncode != 0:
        failures.append("tracked-script Git probe unavailable")
        tracked: set[str] = set()
    else:
        tracked = {line.replace("\\", "/") for line in probe.stdout.splitlines() if line.strip()}
    missing = sorted(tracked - listed)
    extra = sorted(listed - tracked)
    failures.extend(f"tracked script absent from public script manifest: {path}" for path in missing)
    failures.extend(f"public script manifest lists untracked script: {path}" for path in extra)
    return {"failures": failures, "tracked": len(tracked), "listed": len(listed)}


def validate(repo: Path, *, staged: bool = False) -> dict:
    repo = repo.resolve()
    manifest = load_manifest(repo)
    failures: list[str] = []
    script_manifest = validate_public_script_manifest(repo)
    failures.extend(script_manifest["failures"])
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
    return {"ok": not failures, "repo": str(repo), "mode": "staged" if staged else "worktree", "checked_public_files": checked, "skipped_local_files": local_skipped, "required_files": len(required), "script_manifest": {"tracked": script_manifest["tracked"], "listed": script_manifest["listed"]}, "failures": failures}


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
