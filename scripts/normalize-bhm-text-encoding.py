"""Safely normalize BHM text files to UTF-8.

Dry-run is the default. ``--apply`` requires ``--backup-root`` and writes a
SHA-256 manifest before replacing files. The repair heuristic only changes a
file when a reversible CP1252/UTF-8 round-trip materially reduces mojibake
markers; it never guesses from an invalid byte stream.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

from blackholememory.filesystem_boundaries import assert_safe_path
from blackholememory.filesystem_boundaries import replace_bytes_safely

_AUDIT_PATH = Path(__file__).with_name("audit-bhm-cleanup.py")
_SPEC = importlib.util.spec_from_file_location("bhm_cleanup_audit", _AUDIT_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"unable to load {_AUDIT_PATH}")
_AUDIT = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _AUDIT
_SPEC.loader.exec_module(_AUDIT)
MOJIBAKE_MARKERS = _AUDIT.MOJIBAKE_MARKERS
iter_text_files = _AUDIT.iter_text_files
KNOWN_FIXTURE_FILES = _AUDIT.KNOWN_FIXTURE_FILES


def score(text: str) -> int:
    return sum(text.count(marker) for marker in MOJIBAKE_MARKERS)


def repair(text: str) -> str:
    current = text
    for _ in range(4):
        try:
            candidate = current.encode("cp1252").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            break
        if score(candidate) >= score(current):
            break
        current = candidate
    return current


def _safe_root(root: Path) -> Path:
    safe_root = assert_safe_path(root, reject_hardlink_target=False)
    if not safe_root.is_dir():
        raise ValueError(f"normalization root is not a directory: {safe_root}")
    return safe_root


def _contained_path(root: Path, relative: object, *, label: str) -> Path:
    if not isinstance(relative, str) or not relative.strip():
        raise ValueError(f"{label} path is invalid")
    candidate = assert_safe_path(root / Path(relative))
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} path escapes its approved root") from exc
    return candidate


def _writable_target(path: Path, *, label: str) -> Path:
    safe_path = assert_safe_path(path)
    if safe_path.exists() and not safe_path.is_file():
        raise ValueError(f"{label} target is not a regular file: {safe_path}")
    return safe_path


def plan(root: Path, *, include_vendor: bool = False) -> list[dict]:
    root = _safe_root(root)
    changes: list[dict] = []
    for path in iter_text_files(root, include_vendor=include_vendor):
        path = assert_safe_path(path)
        if path.relative_to(root).as_posix() in KNOWN_FIXTURE_FILES:
            continue
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            continue
        repaired = repair(text)
        changed = repaired != text or raw.startswith(b"\xef\xbb\xbf")
        if changed:
            changes.append({"path": path.relative_to(root).as_posix(), "before_sha256": hashlib.sha256(raw).hexdigest(), "reason": "mojibake-repair" if repaired != text else "remove-utf8-bom"})
    return changes


def apply(root: Path, changes: list[dict], backup_root: Path) -> None:
    root = _safe_root(root)
    backup_root = assert_safe_path(backup_root, reject_hardlink_target=False)
    try:
        backup_root.relative_to(root)
    except ValueError:
        pass
    else:
        raise ValueError("normalization backup root must not be inside source root")
    backup_root.mkdir(parents=True, exist_ok=True)
    assert_safe_path(backup_root, reject_hardlink_target=False)
    manifest_path = _writable_target(backup_root / "manifest.json", label="manifest")
    prepared: list[tuple[dict, Path, Path]] = []
    for change in changes:
        source = _contained_path(root, change.get("path"), label="source")
        backup = _writable_target(
            _contained_path(backup_root, change.get("path"), label="backup"),
            label="backup",
        )
        if not source.is_file():
            raise ValueError(f"normalization source is not a regular file: {source}")
        assert_safe_path(source)
        assert_safe_path(backup)
        prepared.append((change, source, backup))

    # Validate every mutable target before the first backup/source write.
    assert_safe_path(manifest_path)
    manifest: list[dict] = []
    for change, source, backup in prepared:
        backup.parent.mkdir(parents=True, exist_ok=True)
        assert_safe_path(backup.parent, reject_hardlink_target=False)
        raw = source.read_bytes()
        expected_before = str(change.get("before_sha256") or "").lower()
        if expected_before and hashlib.sha256(raw).hexdigest() != expected_before:
            raise RuntimeError(f"normalization source changed since plan: {source}")
        replace_bytes_safely(backup, raw)
        text = raw.decode("utf-8-sig")
        repaired = repair(text)
        replace_bytes_safely(source, repaired.encode("utf-8"))
        manifest.append({**change, "backup": str(backup), "after_sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
    replace_bytes_safely(
        manifest_path,
        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--include-vendor", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-root", type=Path)
    args = parser.parse_args()
    root = _safe_root(args.root)
    changes = plan(root, include_vendor=args.include_vendor)
    print(json.dumps({"root": str(root), "apply": args.apply, "changes": changes}, ensure_ascii=False, indent=2))
    if not args.apply:
        return 0
    if args.backup_root is None:
        parser.error("--apply requires --backup-root")
    apply(root, changes, args.backup_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
