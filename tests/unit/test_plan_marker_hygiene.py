from __future__ import annotations

from pathlib import Path
import re


REPO_ROOT = Path(__file__).parents[2]
LOCAL_PLAN = REPO_ROOT / ".docs" / "plan" / "bhm-only-cutover-master-plan.md"
PUBLIC_PLAN = REPO_ROOT / "docs" / "plan" / "bhm-only-cutover-master-plan.md"
SUPERSEDED_IDS = (
    [f"P27.{index}" for index in range(2, 7)]
    + [f"P28.{index}" for index in range(1, 16)]
)
MARKER = re.compile(r"^- \[([ ~xX])\] \*\*(P(?:27|28)\.\d+)", re.MULTILINE)


def _canonical_plan_text() -> str | None:
    if LOCAL_PLAN.is_file():
        return LOCAL_PLAN.read_text(encoding="utf-8")
    assert not PUBLIC_PLAN.exists(), "internal canonical plan must remain outside the public docs tree"
    return None


def test_historical_umbrella_markers_are_explicitly_closed() -> None:
    text = _canonical_plan_text()
    if text is None:
        return
    rows = {identifier: marker for marker, identifier in MARKER.findall(text)}

    assert set(SUPERSEDED_IDS).issubset(rows)
    assert all(rows[identifier].lower() == "x" for identifier in SUPERSEDED_IDS)
    for identifier in SUPERSEDED_IDS:
        heading = next(line for line in text.splitlines() if f"**{identifier} /" in line)
        assert "umbrella decomposition/superseded" in heading
        assert "residual status governed by CAP crosswalk and later P28.72+ slices" in heading


def test_historical_marker_hygiene_does_not_claim_parity() -> None:
    text = _canonical_plan_text()
    if text is None:
        return
    section = text[text.index("### P28 execution workstreams") : text.index("### P28 transfer lanes")]

    assert "P28.81 / WI-152" in section
    assert "does not claim full CBM parity" in section
    assert "acceptance readiness" in section
