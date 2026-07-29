"""Read-only BHM cleanup audit: UTF-8, mojibake, ports and orphan hints.

The audit never deletes or rewrites files. Runtime/cache directories and binary
release artifacts are excluded by default; use ``--include-vendor`` when a
vendored minified bundle must be inspected as well.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Iterable


TEXT_SUFFIXES = {
    ".bat",
    ".cmd",
    ".cfg",
    ".css",
    ".env",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".mjs",
    ".ps1",
    ".psm1",
    ".py",
    ".rst",
    ".sh",
    ".spec",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
EXCLUDED_PARTS = {".git", ".venv", ".pytest_cache", ".ruff_cache", "build", "dist", "runtime", "output", "node_modules", "__pycache__", ".src", ".legacy"}
# Keep the byte-level markers explicit so a shell/editor cannot collapse the
# multi-codepoint signatures into a broad single-character match. A lone
# `Ã`/`Â` in a vendor language table is not mojibake by itself.
MOJIBAKE_MARKERS = (
    "\u00c3\u0192",
    "\u00c3\u201a",
    "\u00c3\u0090",
    "\u00c3\u2011",
    "\u00c3\u00a2",
    "\ufffd",
)
PORT_RE = re.compile(r"(?<!\d)(?:127\.0\.0\.1|localhost|0\.0\.0\.0):\d{2,5}|(?<!\d):\d{4,5}\b")
KNOWN_FIXTURE_FILES = {"scripts/audit-bhm-cleanup.py", "scripts/validate-bhm-static-encoding.py", "tests/unit/test_static_encoding.py"}
VENDORED_NAMES = {"redoc.standalone.js"}


def iter_text_files(root: Path, *, include_vendor: bool = False) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        rel_parts = set(path.relative_to(root).parts)
        if rel_parts & EXCLUDED_PARTS:
            continue
        if (path.name.endswith(".min.js") or path.name in VENDORED_NAMES) and not include_vendor:
            continue
        yield path


def _mojibake_hits(text: str) -> list[str]:
    return [marker for marker in MOJIBAKE_MARKERS if marker in text]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit(root: Path, *, include_vendor: bool = False) -> dict:
    root = root.resolve()
    files: list[dict] = []
    ports: list[dict] = []
    for path in iter_text_files(root, include_vendor=include_vendor):
        relative = path.relative_to(root).as_posix()
        raw = path.read_bytes()
        item = {"path": relative, "bytes": len(raw), "sha256": _sha256(path), "encoding": "utf-8", "bom": False, "mojibake": []}
        if raw.startswith(b"\xef\xbb\xbf"):
            item["bom"] = True
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            item["encoding"] = "utf-16"
            files.append(item)
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            item["encoding"] = "invalid-utf-8"
            item["error"] = str(exc)
            files.append(item)
            continue
        item["mojibake"] = [] if relative in KNOWN_FIXTURE_FILES else _mojibake_hits(text)
        files.append(item)
        if relative not in KNOWN_FIXTURE_FILES:
            for line_no, line in enumerate(text.splitlines(), 1):
                for match in PORT_RE.finditer(line):
                    ports.append({"path": relative, "line": line_no, "match": match.group(0), "text": line.strip()[:240]})

    invalid = [item for item in files if item["encoding"] != "utf-8"]
    bom = [item for item in files if item["bom"]]
    mojibake = [item for item in files if item["mojibake"]]
    return {
        "ok": not invalid and not mojibake and not bom,
        "root": str(root),
        "files": len(files),
        "encoding": {"invalid_utf8": invalid, "bom_utf8": bom},
        "mojibake": mojibake,
        "ports": ports,
        "policy": {
            "text_files_only": True,
            "excluded_parts": sorted(EXCLUDED_PARTS),
            "vendor_included": include_vendor,
            "known_fixture_files": sorted(KNOWN_FIXTURE_FILES),
            "sha256": "per-file read-only evidence",
        },
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--include-vendor", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    report = audit(args.root, include_vendor=args.include_vendor)
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({"ok": report["ok"], "files": report["files"], "invalid_utf8": len(report["encoding"]["invalid_utf8"]), "bom_utf8": len(report["encoding"]["bom_utf8"]), "mojibake_files": len(report["mojibake"]), "port_mentions": len(report["ports"])}, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
