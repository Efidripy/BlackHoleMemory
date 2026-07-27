from blackholememory.trace_graph import TRACE_GRAPH_CONFIDENCE_CAP
from blackholememory.trace_graph import TraceGraphError
from blackholememory.trace_graph import build_trace_graph
from blackholememory.trace_graph import validate_trace_graph


def _record(event_id: str, source: str = "web", target: str = "payments") -> dict:
    return {
        "eventId": event_id,
        "hookType": "http_trace",
        "project": "sojmieblo",
        "timestamp": "2026-07-22T10:00:00Z",
        "data": {
            "sourceService": source,
            "targetService": target,
            "protocol": "http",
            "route": "/charge",
        },
    }


def test_trace_graph_is_bounded_and_explicitly_untrusted() -> None:
    graph = build_trace_graph([_record("evt-1"), _record("evt-2")], project="sojmieblo")
    reversed_graph = build_trace_graph([_record("evt-2"), _record("evt-1")], project="sojmieblo")
    assert graph["graph_digest"] == reversed_graph["graph_digest"]
    assert graph["authority"] == "none"
    assert graph["evidence_class"] == "untrusted-runtime-observation"
    assert graph["edges"][0]["edge_kind"] == "trace_observed"
    assert graph["edges"][0]["trusted"] is False
    assert graph["edges"][0]["confidence"] == TRACE_GRAPH_CONFIDENCE_CAP
    assert len(graph["edges"][0]["evidence"]) == 2
    assert validate_trace_graph(graph)["ok"] is True


def test_trace_graph_rejects_non_trace_observations_and_project_mismatch() -> None:
    ordinary = {"eventId": "evt-ordinary", "hookType": "memory_update", "project": "sojmieblo", "data": {"sourceService": "a", "targetService": "b"}}
    other_project = _record("evt-other")
    other_project["project"] = "other-project"
    graph = build_trace_graph([ordinary, other_project], project="sojmieblo")
    assert graph["summary"]["trace_events"] == 0
    assert graph["summary"]["skipped_project"] == 1
    assert graph["edges"] == []


def test_trace_graph_validation_blocks_trust_or_confidence_escalation() -> None:
    graph = build_trace_graph([_record("evt-1")])
    graph["edges"][0]["trusted"] = True
    graph["edges"][0]["confidence"] = 0.9
    result = validate_trace_graph(graph)
    assert result["ok"] is False
    assert "edge_not_untrusted" in result["errors"]
    assert "confidence_cap_exceeded" in result["errors"]


def test_trace_graph_limits_are_fail_closed() -> None:
    try:
        build_trace_graph([], max_events=999)
    except TraceGraphError as exc:
        assert "max_events" in str(exc)
    else:
        raise AssertionError("expected TraceGraphError")


def test_trace_graph_redacts_credentials_in_service_labels() -> None:
    record = _record("evt-secret", source="https://alice:pw@example.test?token=secret", target="payments")
    graph = build_trace_graph([record])
    assert "pw" not in graph["nodes"][0]["name"]
    assert "secret" not in graph["nodes"][0]["name"]
    assert "[REDACTED]" in graph["nodes"][0]["name"]
