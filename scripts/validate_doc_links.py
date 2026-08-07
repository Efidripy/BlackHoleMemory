"""Validate bounded active Markdown links without touching runtime state.

Historical audit/ops/research/benchmark documents intentionally retain
immutable references to archived receipts. The default gate therefore checks
the active public documentation surface and excludes those historical trees.
External URLs and application routes are not fetched or interpreted here.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


MARKDOWN_LINK_RE = re.compile(r"!?(?:\[[^\]]*\])\((?:<([^>]+)>|([^\s)]+))(?:\s+[^)]*)?\)")
SKIP_DIRS = {"audits", "ops", "research", "benchmarks"}
SKIP_SCHEMES = ("http://", "https://", "mailto:", "data:", "javascript:")
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")


def _markdown_files(root: Path) -> list[Path]:
    candidates = [root / "README.md", root / "SECURITY.md"]
    candidates.extend((root / "docs").rglob("*.md"))
    candidates.extend((root / ".github").rglob("*.md"))
    return sorted(
        path
        for path in candidates
        if path.is_file() and not any(part in SKIP_DIRS for part in path.relative_to(root).parts)
    )


def _target_path(source: Path, raw_target: str) -> Path | None:
    target = raw_target.strip()
    if not target or target.startswith("/"):
        return None
    if target.casefold().startswith(SKIP_SCHEMES):
        return None
    target = target.split("#", 1)[0].split("?", 1)[0]
    if not target:
        return None
    return (source.parent / target).resolve()


def _anchor_slug(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value).casefold().strip()
    value = re.sub(r"[^\w\s-]", "", value, flags=re.UNICODE)
    return re.sub(r"[\s-]+", "-", value).strip("-")


def _anchors(text: str) -> set[str]:
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    for line in text.splitlines():
        match = HEADING_RE.match(line)
        if not match:
            continue
        base = _anchor_slug(match.group(1))
        if not base:
            continue
        index = counts.get(base, 0)
        anchors.add(base if index == 0 else f"{base}-{index}")
        counts[base] = index + 1
    return anchors


def validate(root: Path) -> dict[str, object]:
    files = _markdown_files(root)
    missing: list[dict[str, str]] = []
    link_count = 0
    anchor_count = 0
    for source in files:
        text = source.read_text(encoding="utf-8")
        source_anchors = _anchors(text)
        for match in MARKDOWN_LINK_RE.finditer(text):
            raw_target = match.group(1) or match.group(2) or ""
            target_text = raw_target.strip()
            path_part, separator, anchor = target_text.partition("#")
            if separator and anchor:
                anchor_count += 1
            target = source if target_text.startswith("#") else _target_path(source, path_part or target_text)
            if target is None:
                continue
            link_count += 1
            if not target.exists():
                missing.append(
                    {
                        "source": source.relative_to(root).as_posix(),
                        "target": raw_target,
                    }
                )
                continue
            if separator and anchor:
                target_anchors = source_anchors if target == source else _anchors(target.read_text(encoding="utf-8"))
                if _anchor_slug(anchor) not in target_anchors:
                    missing.append(
                        {
                            "source": source.relative_to(root).as_posix(),
                            "target": raw_target,
                            "reason": "missing_anchor",
                        }
                    )
    return {
        "schema_version": "bhm.docs.link-gate.v1",
        "scope": "active-public-markdown",
        "excluded_directories": sorted(SKIP_DIRS),
        "files_checked": len(files),
        "links_checked": link_count,
        "anchors_checked": anchor_count,
        "missing": missing,
        "ok": not missing,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    report = validate(args.root.resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
