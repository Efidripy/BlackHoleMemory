"""FIX-017 size ratchet: god-modules must not grow back.

The baseline lists every first-party module with its line count. A module may not exceed its baseline; a new
module may not exceed MAX_NEW_MODULE_LINES. When a module shrinks by more than SHRINK_SLACK lines the baseline must be
lowered (regenerate it with the command printed in the failure message).
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "tests" / "fixtures" / "module-size-baseline.json"
MAX_NEW_MODULE_LINES = 1500
SHRINK_SLACK = 50
REGENERATE = (
    "python -c \"import json,pathlib;r=pathlib.Path('.');print(json.dumps({p.as_posix():len(p.read_text(encoding='utf-8').splitlines()) "
    "for d in ('src/blackholememory','scripts') for p in sorted(r.glob(d+'/**/*.py'))},indent=2,sort_keys=True))\" "
    "> tests/fixtures/module-size-baseline.json"
)


def _measure() -> dict[str, int]:
    sizes: dict[str, int] = {}
    for directory in ("src/blackholememory", "scripts"):
        for path in sorted((ROOT / directory).glob("**/*.py")):
            if "__pycache__" in path.parts:
                continue
            sizes[path.relative_to(ROOT).as_posix()] = len(path.read_text(encoding="utf-8").splitlines())
    return sizes


def test_modules_do_not_grow_past_their_baseline() -> None:
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    current = _measure()
    grown = [f"{path}: {baseline[path]} -> {size}" for path, size in current.items() if path in baseline and size > baseline[path]]
    oversized_new = [f"{path}: {size} lines" for path, size in current.items() if path not in baseline and size > MAX_NEW_MODULE_LINES]
    assert not grown, "modules grew past the size baseline (split them instead of growing): " + "; ".join(grown)
    assert not oversized_new, f"new modules above {MAX_NEW_MODULE_LINES} lines: " + "; ".join(oversized_new)


def test_baseline_is_lowered_when_modules_shrink() -> None:
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    current = _measure()
    shrunk = [f"{path}: {baseline[path]} -> {size}" for path, size in current.items() if path in baseline and size < baseline[path] - SHRINK_SLACK]
    assert not shrunk, "module shrank: regenerate tests/fixtures/module-size-baseline.json (" + REGENERATE + ") for: " + "; ".join(shrunk)
