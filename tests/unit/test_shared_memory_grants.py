from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from blackholememory.governed_shared_memory import SharedMemoryGrant
from blackholememory.governed_shared_memory import SharedMemoryPolicyError
from blackholememory.shared_memory_grants import SharedGrantRevocation
from blackholememory.shared_memory_grants import build_grant_artifact
from blackholememory.shared_memory_grants import build_revocation_artifact
from blackholememory.shared_memory_grants import grant_digest
from blackholememory.shared_memory_grants import resolve_effective_grants
from blackholememory.memory_service import SQLiteMemoryService


def _grant(**overrides: object) -> SharedMemoryGrant:
    values: dict[str, object] = {"grant_id":"g1","project":"blackholememory","owner_id":"owner","grantee_id":"agent","visibility":"project","operations":["read"],"issued_at":"2026-08-23T12:00:00Z"}
    values.update(overrides)
    return SharedMemoryGrant.model_validate(values)


def test_immutable_grant_and_matching_revocation_materialize_one_revoked_grant() -> None:
    grant = _grant()
    revocation = SharedGrantRevocation(grant_id="g1", project="blackholememory", grant_digest=grant_digest(grant), revoked_at="2026-08-23T12:01:00Z", revocation_receipt_digest="a" * 64)

    effective = resolve_effective_grants(
        [build_grant_artifact(grant).to_record()],
        [build_revocation_artifact(revocation).to_record()],
        project="blackholememory",
    )

    assert len(effective) == 1
    assert effective[0].revoked_at == "2026-08-23T12:01:00Z"
    assert effective[0].revocation_receipt_digest == "a" * 64


def test_ledger_fails_closed_on_duplicate_or_mismatched_revision() -> None:
    grant = _grant()
    duplicate = build_grant_artifact(grant).to_record()
    with pytest.raises(SharedMemoryPolicyError, match="ambiguous"):
        resolve_effective_grants([duplicate, duplicate], [], project="blackholememory")
    invalid = SharedGrantRevocation(grant_id="g1", project="blackholememory", grant_digest="b" * 64, revoked_at="2026-08-23T12:01:00Z", revocation_receipt_digest="a" * 64)
    with pytest.raises(SharedMemoryPolicyError, match="does not match"):
        resolve_effective_grants([duplicate], [build_revocation_artifact(invalid).to_record()], project="blackholememory")


def test_concurrent_conflicting_grant_appends_are_visible_and_fail_closed(tmp_path) -> None:
    database = tmp_path / "memories.sqlite3"
    SQLiteMemoryService(database, allow_create=True).repository.initialize()
    artifacts = [
        build_grant_artifact(_grant(owner_id="owner-a")),
        build_grant_artifact(_grant(owner_id="owner-b")),
    ]
    barrier = threading.Barrier(2)

    def append(artifact):
        service = SQLiteMemoryService(database, allow_create=True)
        barrier.wait(timeout=5)
        return service.append_artifact(artifact)[1]

    with ThreadPoolExecutor(max_workers=2) as pool:
        inserted = list(pool.map(append, artifacts))

    assert inserted == [True, True]
    records = SQLiteMemoryService(database, allow_create=True).list_artifact_records(
        artifact_type="shared_memory_grant", project="blackholememory", limit=None,
    )
    with pytest.raises(SharedMemoryPolicyError, match="ambiguous"):
        resolve_effective_grants(records, [], project="blackholememory")
