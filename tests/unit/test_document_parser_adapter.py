from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from blackholememory import document_parser_adapter
from blackholememory.document_parser_adapter import (
    DocumentParserAdapterError,
    inspect_document_parser_candidate,
    verify_document_parser_receipt,
)


def _receipt(source: Path, root: Path, **overrides: object) -> dict[str, object]:
    return inspect_document_parser_candidate(
        source,
        project="blackholememory",
        allowed_roots=[root],
        operator_confirmed=True,
        **overrides,
    )


def test_plaintext_candidate_is_digest_only_quarantine_with_deterministic_replay(tmp_path: Path) -> None:
    root = tmp_path / "admitted"
    root.mkdir()
    secret = "api_" + ("to" + "ken") + "=" + "redacted-test-input"
    source = root / "notes.txt"
    source.write_text(secret, encoding="utf-8")

    first = _receipt(source, root)
    second = _receipt(source, root)

    assert first == second
    assert first["status"] == "quarantined"
    assert first["reason"] == "review_required"
    assert first["content_sha256"] == hashlib.sha256(secret.encode("utf-8")).hexdigest()
    assert first["quarantine"] == {
        "required": True,
        "persisted": False,
        "raw_content_retained": False,
        "operator_handoff_required": True,
    }
    assert first["execution"] == {
        "dry_run": True,
        "offline_only": True,
        "url_fetch": False,
        "sqlite_mutation": False,
        "outbox_mutation": False,
        "mem0_mutation": False,
        "qdrant_mutation": False,
        "graph_mutation": False,
        "direct_authority_write": False,
    }
    assert secret not in repr(first)
    assert str(source) not in repr(first)
    assert verify_document_parser_receipt(first)


def test_confirmation_root_type_size_page_and_encoding_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "admitted"
    root.mkdir()
    source = root / "notes.txt"
    source.write_text("safe", encoding="utf-8")
    blocked = inspect_document_parser_candidate(source, project="blackholememory", allowed_roots=[root])
    assert blocked["reason"] == "operator_confirmation_required"

    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    assert _receipt(outside, root)["reason"] == "root_not_allowlisted"
    assert _receipt(root / ".." / "outside.txt", root)["reason"] == "root_not_allowlisted"
    wrong_type = root / "notes.pdf"
    wrong_type.write_bytes(b"not-a-pdf")
    assert _receipt(wrong_type, root)["reason"] == "type_not_allowlisted"
    oversized = root / "oversized.txt"
    oversized.write_text("x" * 12, encoding="utf-8")
    assert _receipt(oversized, root, max_bytes=8)["reason"] == "byte_budget_exceeded"
    multipage = root / "pages.txt"
    multipage.write_text("first\fsecond", encoding="utf-8")
    assert _receipt(multipage, root, max_pages=1)["reason"] == "page_budget_exceeded"
    malformed = root / "bad.txt"
    malformed.write_bytes(b"\xff")
    assert _receipt(malformed, root)["reason"] == "unsupported_encoding"


def test_uri_escape_reparse_and_hardlink_are_rejected_or_quarantined(tmp_path: Path) -> None:
    root = tmp_path / "admitted"
    root.mkdir()
    with pytest.raises(DocumentParserAdapterError, match="URLs"):
        inspect_document_parser_candidate("https://example.invalid/a.txt", project="blackholememory", allowed_roots=[root])

    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "outside.txt"
    target.write_text("outside", encoding="utf-8")
    link = root / "linked.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows host")
    assert _receipt(link, root)["reason"] == "unsafe_filesystem_boundary"

    hardlinked = root / "hardlinked.txt"
    try:
        hardlinked.hardlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("hardlinks are unavailable on this Windows host")
    assert _receipt(hardlinked, root)["reason"] == "unsafe_filesystem_boundary"


def test_time_tamper_and_policy_bounds_fail_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / "admitted"
    root.mkdir()
    source = root / "notes.txt"
    source.write_text("safe", encoding="utf-8")
    moments = iter((100.0, 101.0))
    monkeypatch.setattr(document_parser_adapter.time, "monotonic", lambda: next(moments))
    timed_out = _receipt(source, root, timeout_seconds=0.5)
    assert timed_out["reason"] == "time_budget_exceeded"
    assert verify_document_parser_receipt(timed_out)
    tampered = {**timed_out, "reason": "review_required"}
    assert not verify_document_parser_receipt(tampered)

    with pytest.raises(DocumentParserAdapterError, match="max_bytes"):
        _receipt(source, root, max_bytes=1_048_577)
    with pytest.raises(DocumentParserAdapterError, match="max_pages"):
        _receipt(source, root, max_pages=33)
