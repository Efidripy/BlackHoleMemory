from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from blackholememory.code_graph import build_code_graph
from blackholememory.convention_memory import ConventionMemoryError
from blackholememory.convention_memory import ConventionMemoryInjectedFailure
from blackholememory.convention_memory import SQLiteConventionMemoryStore
from blackholememory.convention_memory import build_convention_memory
from blackholememory.convention_memory import explain_convention_card
from blackholememory.convention_memory import preview_convention_memory
from blackholememory.repository_index import RepositorySourceProvenance
from blackholememory.repository_index import index_repository
from blackholememory.repository_index import probe_repository_state


def _fixture(tmp_path: Path) -> tuple[Path, Path, str, str]:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "docs" / "adr").mkdir(parents=True)
    (root / "config").mkdir()
    (root / "scripts").mkdir()
    (root / "main.py").write_text(
        "from service import Service\nfrom fastapi import APIRouter\nrouter = APIRouter()\n\n@router.get('/items')\ndef get_items():\n    return Service().run()\n",
        encoding="utf-8",
    )
    (root / "service.py").write_text(
        "class Service:\n    def run(self):\n        return validate_value()\n\ndef validate_value():\n    return 1\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_main.py").write_text(
        "from main import get_items\n\ndef test_get_items():\n    assert get_items() == 1\n",
        encoding="utf-8",
    )
    (root / "docs" / "adr" / "0001-example.md").write_text("# ADR-0001\n", encoding="utf-8")
    (root / "config" / "settings.json").write_text("{}\n", encoding="utf-8")
    (root / "scripts" / "run.ps1").write_text("Write-Output ok\n", encoding="utf-8")
    database = tmp_path / "graph.sqlite3"
    source = RepositorySourceProvenance(owner="fixture", source_url="local://wi04", license="MIT", evidence_class="E0")
    indexed = index_repository(root, database, project="demo", source=source)
    state = probe_repository_state(root, project="demo")
    graph = build_code_graph(database, project="demo", root_id=state.root_id, repository_snapshot_id=indexed["snapshot_id"])
    return database, str(state.root_id), str(graph["graph_snapshot_id"]), str(indexed["snapshot_id"])


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_extract_convention_cards_are_deterministic_and_provenance_bound(tmp_path: Path) -> None:
    database, root_id, graph_snapshot_id, _repository_snapshot_id = _fixture(tmp_path)
    preview = preview_convention_memory(database, project="demo", root_id=root_id, graph_snapshot_id=graph_snapshot_id)
    repeated = preview_convention_memory(database, project="demo", root_id=root_id, graph_snapshot_id=graph_snapshot_id)

    assert preview["convention_digest"] == repeated["convention_digest"]
    assert preview["cards"]
    assert {card["status"] for card in preview["cards"]} == {"proposal"}
    assert {card["card_kind"] for card in preview["cards"]} >= {"naming", "test_layout", "api_routes", "architecture_authority"}
    assert all(card["evidence"]["graph_digest"] for card in preview["cards"])
    assert all(example["source_ref"] or example["path"] == "" for card in preview["cards"] for example in card["examples"])
    assert preview["execution"]["writes_sqlite_state"] is False
    assert preview["execution"]["raw_source_returned"] is False


def test_publish_review_and_last_known_good_are_bounded(tmp_path: Path) -> None:
    database, root_id, graph_snapshot_id, _repository_snapshot_id = _fixture(tmp_path)
    with pytest.raises(ConventionMemoryInjectedFailure):
        build_convention_memory(database, project="demo", root_id=root_id, graph_snapshot_id=graph_snapshot_id, fail_before_publish=True)
    store = SQLiteConventionMemoryStore(database)
    assert store.current_snapshot("demo", root_id) is None

    built = build_convention_memory(database, project="demo", root_id=root_id, graph_snapshot_id=graph_snapshot_id)
    assert built["ok"] is True
    assert built["summary"]["proposal_count"] == built["summary"]["card_count"]
    card_id = built["cards"][0]["card_id"]
    accepted = store.review_card(project="demo", root_id=root_id, card_id=card_id, decision="accepted", reviewer="operator", reason="Evidence reviewed against graph snapshot.")
    accepted_card = next(card for card in accepted["cards"] if card["card_id"] == card_id)
    assert accepted_card["status"] == "accepted"
    assert accepted_card["review"]["reviewer"] == "operator"
    assert accepted["summary"]["accepted_count"] == 1
    assert accepted["summary"]["proposal_count"] == accepted["summary"]["card_count"] - 1
    rolled_back = store.review_card(project="demo", root_id=root_id, card_id=card_id, decision="proposal", reviewer="", reason="")
    assert next(card for card in rolled_back["cards"] if card["card_id"] == card_id)["status"] == "proposal"
    assert rolled_back["summary"]["accepted_count"] == 0
    assert rolled_back["summary"]["proposal_count"] == rolled_back["summary"]["card_count"]
    with pytest.raises(ConventionMemoryError):
        store.review_card(project="demo", root_id=root_id, card_id=card_id, decision="accepted", reviewer="", reason="")


def test_stale_card_explain_is_explicit_and_preview_is_read_only(tmp_path: Path) -> None:
    database, root_id, graph_snapshot_id, _repository_snapshot_id = _fixture(tmp_path)
    built = build_convention_memory(database, project="demo", root_id=root_id, graph_snapshot_id=graph_snapshot_id)
    card_id = built["cards"][0]["card_id"]
    before = _digest(database)
    explain = explain_convention_card(database, project="demo", root_id=root_id, card_id=card_id)
    preview = preview_convention_memory(database, project="demo", root_id=root_id)
    after = _digest(database)
    assert explain["stale"] is False
    assert explain["card"]["freshness"]["state"] == "fresh"
    assert preview["execution"]["writes_sqlite_state"] is False
    assert before == after
