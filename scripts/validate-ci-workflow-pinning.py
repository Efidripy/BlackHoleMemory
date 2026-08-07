"""Fail closed when GitHub Actions workflows use mutable action references."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


USES_RE = re.compile(r"^\s*(?:-\s+)?uses:\s*([^\s#]+)")
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def _workflow_files(root: Path) -> list[Path]:
    workflow_root = root / ".github" / "workflows"
    if not workflow_root.is_dir():
        return []
    return sorted(path for path in workflow_root.rglob("*") if path.suffix.casefold() in {".yml", ".yaml"})


def validate(root: Path) -> dict[str, object]:
    failures: list[dict[str, object]] = []
    action_count = 0
    files = _workflow_files(root)
    for path in files:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            match = USES_RE.match(line)
            if not match:
                continue
            action_count += 1
            reference = match.group(1)
            if reference.startswith("./"):
                continue
            if "@" not in reference:
                failures.append(
                    {"file": path.relative_to(root).as_posix(), "line": line_number, "reference": reference, "reason": "missing_ref"}
                )
                continue
            _action, revision = reference.rsplit("@", 1)
            if not SHA_RE.fullmatch(revision):
                failures.append(
                    {"file": path.relative_to(root).as_posix(), "line": line_number, "reference": reference, "reason": "mutable_ref"}
                )
    return {
        "schema_version": "bhm.ci.workflow-pinning.v1",
        "workflow_files": len(files),
        "actions_checked": action_count,
        "failures": failures,
        "ok": not failures,
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
