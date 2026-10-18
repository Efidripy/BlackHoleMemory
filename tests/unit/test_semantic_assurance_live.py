from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

import blackholememory.semantic_assurance_live as live


FIXTURE = Path(__file__).parents[1] / "fixtures" / "semantic-assurance-v1.json"


def _response(payload: Any) -> httpx.Response:
    return httpx.Response(200, request=httpx.Request("GET", "http://127.0.0.1:13666"), json=payload)


def _inventory() -> dict[str, object]:
    return {
        "models": [{
            "type": "llm",
            "key": live.DEFAULT_MODEL_ID,
            "selected_variant": live.DEFAULT_MODEL_VARIANT,
            "loaded_instances": [{"config": {"context_length": 8192, "parallel": 1}}],
        }]
    }


def _valid_result(case_id: str) -> dict[str, object]:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    case = next(item for item in raw["cases"] if item["case_id"] == case_id)
    return dict(case["expected"])


def test_live_adapter_uses_only_inventory_and_chat_and_emits_content_free_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            assert kwargs["trust_env"] is False
            assert kwargs["follow_redirects"] is False

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def get(self, path: str) -> httpx.Response:
            calls.append(("GET", path))
            return _response(_inventory())

        def post(self, path: str, *, json: dict[str, Any]) -> httpx.Response:
            calls.append(("POST", path))
            assert json["model"] == live.DEFAULT_MODEL_ID
            assert json["temperature"] == 0
            assert json["reasoning"] == "off"
            assert json["store"] is False
            assert json["max_output_tokens"] == 400
            case_id = next(case_id for case_id in _holdout_ids() if f"Task: { _prompt_for(case_id) }" in json["input"])
            return _response({
                "output": [{"content": json_module_dumps(_valid_result(case_id))}],
                "stats": {"model_load_time_seconds": 0.0},
            })

    monkeypatch.setattr(live.httpx, "Client", FakeClient)
    report = live.run_live_semantic_assurance(FIXTURE, repeats=2)
    rendered = json.dumps(report, ensure_ascii=False)

    assert report["gate"]["mechanical_valid"] is True
    assert report["gate"]["automatic_apply_decision"] == "no_go"
    assert report["execution"]["partition"] == "sealed_holdout"
    assert calls[:1] == [("GET", "/api/v1/models")]
    assert calls[-1:] == [("GET", "/api/v1/models")]
    assert set(calls[1:-1]) == {("POST", "/api/v1/chat")}
    assert "Synthetic projection work" not in rendered
    assert "support_id=s1" not in rendered


def test_live_adapter_fails_closed_on_malformed_model_classification(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def get(self, path: str) -> httpx.Response:
            return _response(_inventory())

        def post(self, path: str, *, json: dict[str, Any]) -> httpx.Response:
            return _response({"output": [{"content": "not json"}], "stats": {"model_load_time_seconds": 0.0}})

    monkeypatch.setattr(live.httpx, "Client", FakeClient)
    report = live.run_live_semantic_assurance(FIXTURE)

    assert report["gate"]["mechanical_valid"] is False
    assert report["failure"] == {"stage": "parser", "code": "malformed_classification", "repeat": 1, "case_id": "holdout-supported-separation"}
    assert report["execution"]["chat_call_count"] == 1


def test_live_adapter_reports_missing_model_load_telemetry_without_inventing_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def get(self, path: str) -> httpx.Response:
            return _response(_inventory())

        def post(self, path: str, *, json: dict[str, Any]) -> httpx.Response:
            case_id = next(case_id for case_id in _holdout_ids() if f"Task: { _prompt_for(case_id) }" in json["input"])
            return _response({"output": [{"content": json_module_dumps(_valid_result(case_id))}]})

    monkeypatch.setattr(live.httpx, "Client", FakeClient)
    report = live.run_live_semantic_assurance(FIXTURE)

    assert report["gate"]["mechanical_valid"] is True
    assert report["model_load_events"] is None
    assert report["model_load_telemetry"] == "unavailable"
    assert report["execution"]["zero_model_load_events_when_observed"] is None


def test_live_adapter_fails_closed_if_a_second_or_changed_model_is_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    inventory = _inventory()
    inventory["models"].append({
        "type": "llm", "key": "another/local-model", "selected_variant": "another/local-model@q4", "loaded_instances": [{}],
    })

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def get(self, path: str) -> httpx.Response:
            return _response(inventory)

        def post(self, path: str, *, json: dict[str, Any]) -> httpx.Response:
            raise AssertionError("chat must not be called when inventory is unsafe")

    monkeypatch.setattr(live.httpx, "Client", FakeClient)
    report = live.run_live_semantic_assurance(FIXTURE)

    assert report["gate"]["mechanical_valid"] is False
    assert report["failure"] == {"stage": "inventory_before", "code": "additional_loaded_llm"}
    assert report["execution"]["chat_call_count"] == 0


def test_live_adapter_rejects_non_loopback_and_other_model_contracts() -> None:
    with pytest.raises(live.SemanticAssuranceLiveError):
        live.run_live_semantic_assurance(FIXTURE, base_url="https://example.test")
    with pytest.raises(live.SemanticAssuranceLiveError):
        live.run_live_semantic_assurance(FIXTURE, model_id="another/local-model")


def _holdout_ids() -> list[str]:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return [str(case["case_id"]) for case in raw["cases"] if case["partition"] == "sealed_holdout"]


def _prompt_for(case_id: str) -> str:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return str(next(case["prompt"] for case in raw["cases"] if case["case_id"] == case_id))


def json_module_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
