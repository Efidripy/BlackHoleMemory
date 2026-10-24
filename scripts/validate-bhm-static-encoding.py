"""Validate UTF-8 and reject common mojibake in user-facing static assets."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


TEXT_SUFFIXES = {".css", ".html", ".js", ".svg"}
EXCLUDED_NAMES = {"redoc.standalone.js"}
MOJIBAKE_MARKERS = ("Ð", "Ñ", "�", "Ã", "Â", "â€", "ðŸ")


def iter_static_assets(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if path.name in EXCLUDED_NAMES or path.name.endswith(".min.js"):
            continue
        yield path


def scan_asset(path: Path, root: Path) -> list[str]:
    relative = path.relative_to(root).as_posix()
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        return [f"{relative}: invalid UTF-8 ({exc})"]
    failures = [f"{relative}: mojibake marker {marker!r}" for marker in MOJIBAKE_MARKERS if marker in text]
    if path.name == "galaxy.html":
        if '<meta charset="UTF-8">' not in text:
            failures.append(f"{relative}: missing explicit UTF-8 meta charset")
        if '<html lang="en">' not in text:
            failures.append(f"{relative}: missing explicit document language")
    return failures


def validate(root: Path) -> dict[str, object]:
    root = root.resolve()
    if not root.is_dir():
        return {"ok": False, "root": str(root), "files": 0, "failures": [f"missing static root: {root}"]}
    files = list(iter_static_assets(root))
    failures = [failure for path in files for failure in scan_asset(path, root)]
    return {"ok": not failures, "root": str(root), "files": len(files), "failures": failures}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("src/blackholememory/static"))
    args = parser.parse_args()
    result = validate(args.root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
