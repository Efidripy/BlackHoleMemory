from __future__ import annotations

import hashlib
from pathlib import Path
import re


REPO_ROOT = Path(__file__).parents[2]
LOCAL_PLAN = (
    REPO_ROOT
    / ".docs"
    / "archive"
    / "2026-08-21-pre-consolidation"
    / "plan"
    / "bhm-only-cutover-master-plan.md"
)
PUBLIC_PLAN = REPO_ROOT / "docs" / "plan" / "bhm-only-cutover-master-plan.md"
LOCAL_DOCS = REPO_ROOT / ".docs"
ARCHIVE_ROOT = LOCAL_DOCS / "archive" / "2026-08-21-pre-consolidation"
ARCHIVE_EXPECTED = {
    "NEXT-SESSION.md": (
        85715,
        "4ed16e312cd65cabe0885f4be6754727665d30671ec7cac96b7fd211b7ddb6f3",
    ),
    "WORKLIST.md": (
        291507,
        "f5058274b979fb7f08af0dc452b57f31799a56d745dbc32922eef63ad1e3e234",
    ),
    "plan/bhm-only-cutover-master-plan.md": (
        478848,
        "6e4de281873853ece770a98210fff2c06775c51883f2529a4eec0576be4a22a1",
    ),
    "planning/wl-295-metronix-inspired-adoption-plan-2026-08-21.md": (
        8386,
        "f6fb650f6da49a20166860631c52b78d83fd678c46e5788db8f32cf91ad516d8",
    ),
    "pre-consolidation/AGENTS.md": (
        8007,
        "e0b1a3ba4ce2a8b628ad403bd85436bc88296c668911faeab4614dc204e5f6c2",
    ),
    "pre-consolidation/PROJECT.md": (
        18426,
        "b61e934f021609de4d497289e1c4e036629c4abfd59440b201c35ad5c7163278",
    ),
    "pre-consolidation/README.md": (
        1262,
        "fc8b54630065d0507929284a9dde6dd6df57efd7fdd8325e31261238de06c3d9",
    ),
    "research/bhm-integration-master-plan-2.md": (
        163383,
        "efc5f0303585a3bf2bded67ba86c3ac7dbbbf96083433bea38b51ea09ae6be1c",
    ),
}
ARCHIVE_INVENTORY_DIGEST = (
    "1c30467d901eafa9526207706b3d56a0f3bca1f0955393e55f6573f9be000a87"
)
SUPERSEDED_IDS = [f"P27.{index}" for index in range(2, 7)] + [
    f"P28.{index}" for index in range(1, 16)
]
MARKER = re.compile(r"^- \[([ ~xX])\] \*\*(P(?:27|28)\.\d+)", re.MULTILINE)


def _canonical_plan_text() -> str | None:
    if LOCAL_PLAN.is_file():
        return LOCAL_PLAN.read_text(encoding="utf-8")
    assert not PUBLIC_PLAN.exists(), (
        "internal canonical plan must remain outside the public docs tree"
    )
    return None


def test_local_status_canon_has_no_sibling_backlog() -> None:
    if not LOCAL_DOCS.is_dir():
        return

    assert (LOCAL_DOCS / "DONE.md").is_file()
    assert (LOCAL_DOCS / "TODO.md").is_file()
    forbidden_names = ("worklist", "roadmap", "next-session", "master-plan")
    sibling_status_files = [
        path.name
        for path in LOCAL_DOCS.glob("*.md")
        if any(marker in path.name.lower() for marker in forbidden_names)
    ]
    assert sibling_status_files == []
    assert not (LOCAL_DOCS / "plan").exists()
    assert not (LOCAL_DOCS / "research").exists()


def test_local_preconsolidation_archive_preserves_exact_sources() -> None:
    if not ARCHIVE_ROOT.is_dir():
        return

    inventory_rows = []
    for relative_path, (expected_bytes, expected_sha256) in sorted(
        ARCHIVE_EXPECTED.items(), key=lambda item: item[0].casefold()
    ):
        path = ARCHIVE_ROOT / relative_path
        payload = path.read_bytes()
        actual_sha256 = hashlib.sha256(payload).hexdigest()
        assert len(payload) == expected_bytes, relative_path
        assert actual_sha256 == expected_sha256, relative_path
        inventory_rows.append(f"{relative_path}|{len(payload)}|{actual_sha256}\n")

    inventory_digest = hashlib.sha256(
        "".join(inventory_rows).encode("utf-8")
    ).hexdigest()
    assert inventory_digest == ARCHIVE_INVENTORY_DIGEST


def test_historical_umbrella_markers_are_explicitly_closed() -> None:
    text = _canonical_plan_text()
    if text is None:
        return
    rows = {identifier: marker for marker, identifier in MARKER.findall(text)}

    assert set(SUPERSEDED_IDS).issubset(rows)
    assert all(rows[identifier].lower() == "x" for identifier in SUPERSEDED_IDS)
    for identifier in SUPERSEDED_IDS:
        heading = next(
            line for line in text.splitlines() if f"**{identifier} /" in line
        )
        assert "umbrella decomposition/superseded" in heading
        assert (
            "residual status governed by CAP crosswalk and later P28.72+ slices"
            in heading
        )


def test_historical_marker_hygiene_does_not_claim_parity() -> None:
    text = _canonical_plan_text()
    if text is None:
        return
    section = text[
        text.index("### P28 execution workstreams") : text.index(
            "### P28 transfer lanes"
        )
    ]

    assert "P28.81 / WI-152" in section
    assert "does not claim full CBM parity" in section
    assert "acceptance readiness" in section


def test_cap12_cap13_cap14_are_retired_from_current_plan_scope() -> None:
    text = _canonical_plan_text()
    if text is None:
        return

    assert "Retirement override (2026-07-31)" in text
    override = text[
        text.index("Retirement override (2026-07-31)") : text.index("Closed history:")
    ]
    assert "permanently excluded" in override
    assert "every active backlog" in override

    backlog = text[text.index("## 7. Execution backlog") : text.index("## 7A.")]
    assert all(capability not in backlog for capability in ("CAP12", "CAP13", "CAP14"))
