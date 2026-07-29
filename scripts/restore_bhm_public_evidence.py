"""Restore crosswalk evidence from a verified snapshot into public/local zones."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


WINDOWS_USER = re.compile(r"(?i)[A-Z]:\\Users\\[^\\\s`\"'<>]+")
WINDOWS_WORKSPACE = re.compile(r"(?i)[A-Z]:\\GitHub(?=[\\/\s`\"'<>.,)\]]|$)")
UNIX_USER = re.compile(r"(?i)/(?:Users|home)/[^/\s`\"'<>]+")
BEARER_VALUE = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]{12,}")


def evidence_paths(crosswalk: dict) -> list[str]:
    paths: set[str] = set()

    def visit(value: object) -> None:
        if isinstance(value, str) and value.strip().startswith(".docs/ops/"):
            paths.add(value.strip())
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
    paths = evidence_paths(json.loads((repo / ".docs/config/cbm-bhm-capability-crosswalk.json").read_text(encoding="utf-8")))
    restored = 0
    skipped = 0
    for relative in paths:
        source_path = source / relative
        public_path = repo / relative
        raw_path = raw_root / relative
        if not source_path.is_file():
            raise FileNotFoundError(f"recovery source missing: {relative}")
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        if not raw_path.exists():
            raw_path.write_bytes(source_path.read_bytes())
        if missing_only and public_path.exists():
            skipped += 1
            continue
        public_path.parent.mkdir(parents=True, exist_ok=True)
        public_path.write_text(sanitize(source_path.read_text(encoding="utf-8")), encoding="utf-8", newline="\n")
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
