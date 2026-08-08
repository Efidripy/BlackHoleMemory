"""Restore crosswalk evidence from a verified snapshot into public/local zones."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from pathlib import PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from blackholememory.filesystem_boundaries import assert_safe_path
from blackholememory.filesystem_boundaries import replace_bytes_safely
from blackholememory.filesystem_boundaries import write_bytes_exclusive


WINDOWS_USER = re.compile(r"(?i)[A-Z]:\\Users\\[^\\\s`\"'<>]+")
WINDOWS_WORKSPACE = re.compile(r"(?i)[A-Z]:\\GitHub(?=[\\/\s`\"'<>.,)\]]|$)")
UNIX_USER = re.compile(r"(?i)/(?:Users|home)/[^/\s`\"'<>]+")
BEARER_VALUE = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]{12,}")


def _safe_evidence_relative(value: str) -> str:
    """Return a traversal-free evidence path or fail closed."""

    candidate = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(candidate)
    if path.is_absolute() or candidate != path.as_posix() or ".." in path.parts:
        raise ValueError(f"unsafe evidence path: {value!r}")
    if not candidate.startswith(".docs/ops/") or len(path.parts) < 3:
        raise ValueError(f"evidence path is outside .docs/ops: {value!r}")
    return candidate


def _contained_path(root: Path, relative: str) -> Path:
    root = assert_safe_path(root, reject_hardlink_target=False)
    target = root / relative
    target = assert_safe_path(target)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path escapes approved root: {target}") from exc
    return target


def evidence_paths(crosswalk: dict) -> list[str]:
    paths: set[str] = set()

    def visit(value: object) -> None:
        if isinstance(value, str) and value.strip().startswith(".docs/ops/"):
            paths.add(_safe_evidence_relative(value))
        elif isinstance(value, dict):
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(crosswalk)
    return sorted(paths)


def sanitize(text: str) -> str:
    text = WINDOWS_USER.sub("<user-profile>", text)
    text = WINDOWS_WORKSPACE.sub("<workspace-root>", text)
    text = UNIX_USER.sub("<user-home>", text)
    return BEARER_VALUE.sub(r"\1<redacted>", text)


def restore(repo: Path, source: Path, *, raw_root: Path, missing_only: bool) -> dict[str, int]:
    repo = assert_safe_path(repo, reject_hardlink_target=False)
    source = assert_safe_path(source, reject_hardlink_target=False)
    raw_root = _contained_path(repo / ".local" / "evidence", raw_root.relative_to(repo / ".local" / "evidence").as_posix()) if raw_root.is_relative_to(repo / ".local" / "evidence") else None
    if raw_root is None:
        raise ValueError("raw_root must be under repository .local/evidence")
    paths = evidence_paths(json.loads((repo / ".docs/config/cbm-bhm-capability-crosswalk.json").read_text(encoding="utf-8")))
    restored = 0
    skipped = 0
    for relative in paths:
        source_path = _contained_path(source, relative)
        public_path = _contained_path(repo / ".docs" / "ops", relative.removeprefix(".docs/ops/"))
        raw_path = _contained_path(raw_root, relative)
        if not source_path.is_file():
            raise FileNotFoundError(f"recovery source missing: {relative}")
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        if not raw_path.exists():
            write_bytes_exclusive(raw_path, source_path.read_bytes())
        if missing_only and public_path.exists():
            skipped += 1
            continue
        public_path.parent.mkdir(parents=True, exist_ok=True)
        replace_bytes_safely(public_path, sanitize(source_path.read_text(encoding="utf-8")).encode("utf-8"))
        restored += 1
    return {"referenced": len(paths), "restored": restored, "skipped_existing": skipped}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--sanitize-existing-ops", action="store_true")
    args = parser.parse_args()
    repo = args.repo.resolve()
    raw_root = (args.raw_root or repo / ".local/evidence/recovery-20260725").resolve()
    result = restore(repo, args.source.resolve(), raw_root=raw_root, missing_only=not args.force)
    if args.sanitize_existing_ops:
        changed = 0
        for path in sorted((repo / ".docs/ops").glob("*")):
            if not path.is_file() or path.suffix.lower() not in {".md", ".json", ".txt"}:
                continue
            current = path.read_text(encoding="utf-8")
            normalized = sanitize(current)
            if normalized != current:
                path.write_text(normalized, encoding="utf-8", newline="\n")
                changed += 1
        result["sanitized_existing_ops"] = changed
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
