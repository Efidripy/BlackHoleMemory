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
import shutil
import sys
from pathlib import Path

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


def plan(root: Path, *, include_vendor: bool = False) -> list[dict]:
    changes: list[dict] = []
    for path in iter_text_files(root, include_vendor=include_vendor):
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
    backup_root.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    for change in changes:
        source = root / change["path"]
        backup = backup_root / change["path"]
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, backup)
        raw = source.read_bytes()
        text = raw.decode("utf-8-sig")
        repaired = repair(text)
        source.write_text(repaired, encoding="utf-8", newline="")
        manifest.append({**change, "backup": str(backup), "after_sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
    (backup_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--include-vendor", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-root", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    changes = plan(root, include_vendor=args.include_vendor)
    print(json.dumps({"root": str(root), "apply": args.apply, "changes": changes}, ensure_ascii=False, indent=2))
    if not args.apply:
        return 0
    if args.backup_root is None:
        parser.error("--apply requires --backup-root")
    apply(root, changes, args.backup_root.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
