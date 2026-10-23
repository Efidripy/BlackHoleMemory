"""Event-ledger and digest helpers for the live semantic-assurance adapter."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


def _event_record(
    *,
    sequence: int,
    repeat_number: int,
    case_id: str,
    request_digest: str,
    response_digest: str,
    previous_event_digest: str | None,
) -> dict[str, Any]:
    """Create one ordered, content-free runner event.

    The predecessor is deliberately not emitted.  It is reconstructed by the
    verifier from the preceding public event digest, so an individual record
    has exactly the small public schema promised by the receipt contract.
    """

    core = {
        "sequence": sequence,
        "repeat_number": repeat_number,
        "case_id": case_id,
        "request_digest": request_digest,
        "response_digest": response_digest,
        "previous_event_digest": previous_event_digest,
    }
    return {
        "sequence": sequence,
        "repeat_number": repeat_number,
        "case_id": case_id,
        "request_digest": request_digest,
        "response_digest": response_digest,
        "event_digest": _digest(core),
    }


def _event_chain_seed(before_inventory: Mapping[str, Any] | None) -> str:
    """Return a domain-separated seed that binds the first event to inventory."""

    before_digest = _digest(before_inventory) if before_inventory is not None else None
    return _digest({
        "domain": "bhm.semantic-assurance-live-event-chain.v3",
        "inventory_before_digest": before_digest,
    })


def _event_chain(
    events: Sequence[Mapping[str, Any]],
    before_inventory: Mapping[str, Any] | None,
    after_inventory: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return a public event chain bound to model inventories before and after.

    Request and response values never enter the receipt: only SHA-256 values
    are retained.  ``chain_root`` binds the final linked event to both exact
    model-inventory snapshots, preventing a ledger from being transplanted to
    a differently observed model boundary.
    """

    before_digest = _digest(before_inventory) if before_inventory is not None else None
    after_digest = _digest(after_inventory) if after_inventory is not None else None
    copied_events = [dict(event) for event in events]
    event_count = len(copied_events)
    first_digest = copied_events[0].get("event_digest") if copied_events else None
    last_digest = copied_events[-1].get("event_digest") if copied_events else None
    root_core = {
        "domain": "bhm.semantic-assurance-live-event-chain.v3",
        "inventory_before_digest": before_digest,
        "inventory_after_digest": after_digest,
        "event_count": event_count,
        "first_event_digest": first_digest,
        "last_event_digest": last_digest,
    }
    return {
        "events": copied_events,
        "event_count": event_count,
        "first_event_digest": first_digest,
        "last_event_digest": last_digest,
        "chain_root": _digest(root_core),
        "inventory_before_digest": before_digest,
        "inventory_after_digest": after_digest,
    }


def _event_chain_is_valid(value: object, expected_event_count: int) -> bool:
    """Verify the event ledger without recovering prompts or model responses."""

    expected_keys = {
        "events", "event_count", "first_event_digest", "last_event_digest", "chain_root",
        "inventory_before_digest", "inventory_after_digest",
    }
    event_keys = {
        "sequence", "repeat_number", "case_id", "request_digest", "response_digest", "event_digest",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != expected_keys
        or not isinstance(value.get("events"), list)
        or not isinstance(value.get("event_count"), int)
        or isinstance(value.get("event_count"), bool)
        or value["event_count"] != len(value["events"])
        or value["event_count"] != expected_event_count
        or not isinstance(value.get("inventory_before_digest"), str)
        or not isinstance(value.get("inventory_after_digest"), str)
        or not _is_sha256(value["inventory_before_digest"])
        or not _is_sha256(value["inventory_after_digest"])
    ):
        return False
    events = value["events"]
    if not events:
        return False
    if value.get("first_event_digest") != events[0].get("event_digest") or value.get("last_event_digest") != events[-1].get("event_digest"):
        return False
    previous_digest = _digest({
        "domain": "bhm.semantic-assurance-live-event-chain.v3",
        "inventory_before_digest": value["inventory_before_digest"],
    })
    for expected_sequence, event in enumerate(events, start=1):
        if (
            not isinstance(event, Mapping)
            or set(event) != event_keys
            or event.get("sequence") != expected_sequence
            or not isinstance(event.get("repeat_number"), int)
            or isinstance(event.get("repeat_number"), bool)
            or event["repeat_number"] < 1
            or not isinstance(event.get("case_id"), str)
            or not event["case_id"]
            or any(not isinstance(event.get(key), str) or not _is_sha256(event[key]) for key in ("request_digest", "response_digest", "event_digest"))
        ):
            return False
        expected_digest = _digest({
            "sequence": event["sequence"],
            "repeat_number": event["repeat_number"],
            "case_id": event["case_id"],
            "request_digest": event["request_digest"],
            "response_digest": event["response_digest"],
            "previous_event_digest": previous_digest,
        })
        if event["event_digest"] != expected_digest:
            return False
        previous_digest = event["event_digest"]
    expected_root = _digest({
        "domain": "bhm.semantic-assurance-live-event-chain.v3",
        "inventory_before_digest": value["inventory_before_digest"],
        "inventory_after_digest": value["inventory_after_digest"],
        "event_count": value["event_count"],
        "first_event_digest": value["first_event_digest"],
        "last_event_digest": value["last_event_digest"],
    })
    return value.get("chain_root") == expected_root


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
