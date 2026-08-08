import hashlib
import json
import os
import socket
from pathlib import Path
from urllib.request import Request

from blackholememory.hook_queue import HookJobQueue
from blackholememory.llm_job_queue import LLMJobQueue
from blackholememory.observation_store import ObservationStore
from blackholememory.provenance_attestation import build_provenance_attestation_report
from blackholememory.qdrant_catalog import build_qdrant_catalog
from blackholememory.qdrant_retention import run_qdrant_restore_drill
from blackholememory.repository_index import RepositorySourceProvenance
from blackholememory.repository_index import SQLiteRepositoryIndexStore
from blackholememory.repository_index import index_repository
from blackholememory import local_endpoint_policy as endpoint_policy


def test_untracked_digest_is_recomputed_after_same_stat_metadata(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("a = 1\n", encoding="utf-8")
    target = root / "b.py"
    target.write_text("b = 1\n", encoding="utf-8")
    database = tmp_path / "index.sqlite3"
    source = RepositorySourceProvenance(owner="pytest", source_url="local://stream-e", license="MIT", evidence_class="E0")

    index_repository(root, database, project="stream-e", source=source)
    old_stat = target.stat()
    target.write_text("c = 1\n", encoding="utf-8")
    os.utime(target, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))

    index_repository(root, database, project="stream-e", source=source, force_refresh=True, max_files_per_run=1)
    resumed = index_repository(root, database, project="stream-e", source=source)
    store = SQLiteRepositoryIndexStore(database)
    root_id = resumed["state"]["root_id"]
    snapshot = store.current_snapshot("stream-e", root_id, include_files=True)
    indexed = {item["path"]: item for item in snapshot["files"]}
    assert indexed["b.py"]["content_sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()


def test_provenance_attestation_rejects_traversal_slug(monkeypatch, tmp_path: Path):
    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True)
    (root / ".src").mkdir()
    (root / "outside").mkdir()
    (root / "outside" / "SOURCE-MANIFEST.json").write_text("{}", encoding="utf-8")
    (root / "config" / "source-registry.json").write_text(
        json.dumps({"sources": [{"id": "evil", "slug": "../outside"}]}),
        encoding="utf-8",
    )
    envelope = root / "envelope.json"
    envelope.write_text(
        json.dumps({"schema_version": "bhm.p28.provenance-attestation.v1", "source_id": "evil"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "blackholememory.provenance_attestation.build_provenance_boundary_report",
        lambda *args, **kwargs: {"failures": [], "package_boundary": {"artifacts": []}},
    )
    report = build_provenance_attestation_report(root, envelope)
    assert report["state"] == "blocked"
    assert "source manifest unavailable or invalid" in report["failures"]


class _Collection:
    def __init__(self, name: str):
        self.name = name


class _Details:
    points_count = 1


class _Qdrant:
    def get_collections(self):
        return type("Collections", (), {"collections": [_Collection("bhm_quarantine_escape")]})()

    def get_collection(self, *, collection_name: str):
        return _Details()


def test_qdrant_manifest_never_reads_backup_outside_root(tmp_path: Path):
    backup_root = tmp_path / "backup"
    backup_root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text('{"points": []}', encoding="utf-8")
    (backup_root / "quarantine-manifest.json").write_text(
        json.dumps(
            {
                "quarantineCollection": "bhm_quarantine_escape",
                "backupPath": str(outside),
                "backupSha256": hashlib.sha256(outside.read_bytes()).hexdigest(),
                "status": "completed",
                "candidateCount": 0,
            }
        ),
        encoding="utf-8",
    )
    catalog = build_qdrant_catalog(_Qdrant(), backup_root=backup_root)
    item = catalog["collections"][0]
    assert item["backup_status"] != "verified_completed"
    drill = run_qdrant_restore_drill(backup_root)
    assert drill["restore_ready_count"] == 0


def test_llm_idempotency_is_project_scoped(tmp_path: Path):
    queue = LLMJobQueue(tmp_path / "llm.sqlite3")
    first = queue.enqueue(idempotency_key="same", job_type="summary", payload={"x": 1}, project="alpha")
    second = queue.enqueue(idempotency_key="same", job_type="summary", payload={"x": 1}, project="beta")
    assert first.inserted and second.inserted
    assert first.job_id != second.job_id


def test_hook_idempotency_is_project_scoped(tmp_path: Path):
    queue = HookJobQueue(tmp_path / "hooks.sqlite3")
    first = queue.enqueue("compact", {"eventId": "same", "project": "alpha"}, priority=1)
    second = queue.enqueue("compact", {"eventId": "same", "project": "beta"}, priority=1)
    assert first.inserted and second.inserted
    assert first.job_id != second.job_id


def test_observation_idempotency_is_project_scoped(tmp_path: Path):
    store = ObservationStore(tmp_path / "observations.sqlite3")
    first = store.append({"eventId": "same", "project": "alpha", "data": {"v": 1}})
    second = store.append({"eventId": "same", "project": "beta", "data": {"v": 1}})
    assert first.inserted and second.inserted
    assert {item["project"] for item in store.load()} == {"alpha", "beta"}


def test_local_hostname_is_pinned_before_connect(monkeypatch):
    captured: dict[str, str] = {}

    def fake_getaddrinfo(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port or 80))]

    class _Opener:
        def open(self, request, *, timeout):
            captured["url"] = request.full_url
            return object()

    monkeypatch.setattr(endpoint_policy.socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(endpoint_policy.urllib.request, "build_opener", lambda *args: _Opener())
    endpoint_policy.open_local_url(Request("http://localhost:8123/health"), timeout=1)
    assert endpoint_policy.urlsplit(captured["url"]).hostname == "127.0.0.1"
