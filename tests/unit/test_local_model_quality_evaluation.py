from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

import blackholememory.local_model_quality_evaluation as quality

FIXTURE = Path(__file__).parents[1] / "fixtures" / "local_model_quality" / "russian-grounded-v1.json"


def _response(payload: Any) -> httpx.Response:
    return httpx.Response(200, request=httpx.Request("GET", "http://127.0.0.1:13666"), json=payload)


def test_fixture_is_bounded_and_has_a_frozen_contract() -> None:
    dataset = quality.load_quality_dataset(FIXTURE)

    assert dataset["prompt_contract"] == quality.PROMPT_CONTRACT
    assert dataset["model"] == {"key": "qwen/qwen3.5-9b", "selected_variant": "qwen/qwen3.5-9b@q6_k"}
    assert len(dataset["splits"]["core"]) == len(dataset["splits"]["holdout"]) == 10
    assert dataset["thresholds"]["minimum_accuracy"] == 1.0


def test_local_endpoint_policy_rejects_remote_or_pathful_base_urls() -> None:
    assert quality._local_base_url("http://127.0.0.1:13666") == "http://127.0.0.1:13666"
    for value in ("https://example.test", "http://10.0.0.2:13666", "http://127.0.0.1:13666/v1"):
        with pytest.raises(quality.LocalModelQualityError):
            quality._local_base_url(value)


def test_quality_gate_uses_only_inventory_and_chat_and_emits_content_free_receipt(monkeypatch) -> None:
    dataset = quality.load_quality_dataset(FIXTURE)
    expected = {case["question"]: case["expected"] for split in dataset["splits"].values() for case in split}
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
            return _response(
                {
                    "models": [
                        {
                            "type": "llm",
                            "key": "qwen/qwen3.5-9b",
                            "selected_variant": "qwen/qwen3.5-9b@q6_k",
                            "loaded_instances": [{"config": {"context_length": 8192, "parallel": 1}}],
                        }
                    ]
                }
            )

        def post(self, path: str, *, json: dict[str, Any]) -> httpx.Response:
            calls.append(("POST", path))
            assert json["temperature"] == 0
            assert json["reasoning"] == "off"
            assert json["store"] is False
            assert json["max_output_tokens"] == 20
            content = json["input"]
            question = next(item for item in expected if f"Вопрос: {item}" in content)
            return _response(
                {
                    "output": [{"content": expected[question]}],
                    "stats": {"time_to_first_token_seconds": 0.02, "tokens_per_second": 30, "model_load_time_seconds": 0},
                }
            )

    monkeypatch.setattr(quality.httpx, "Client", FakeClient)
    report = quality.run_quality_evaluation(dataset, repeats=2)
    rendered = json.dumps(report, ensure_ascii=False)

    assert report["gate_passed"] is True
    assert report["splits"]["core"]["accuracy"] == report["splits"]["holdout"]["accuracy"] == 1.0
    assert calls[0] == ("GET", "/api/v1/models")
    assert set(calls[1:]) == {("POST", "/api/v1/chat")}
    assert "Какой ключ?" not in rendered
    assert "янтарь" not in rendered


def test_quality_gate_fails_closed_on_unexpected_model_load_event(monkeypatch) -> None:
    dataset = quality.load_quality_dataset(FIXTURE)

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def get(self, path: str) -> httpx.Response:
            return _response(
                {
                    "models": [
                        {
                            "type": "llm",
                            "key": "qwen/qwen3.5-9b",
                            "selected_variant": "qwen/qwen3.5-9b@q6_k",
                            "loaded_instances": [{"config": {"context_length": 8192, "parallel": 1}}],
                        }
                    ]
                }
            )

        def post(self, path: str, *, json: dict[str, Any]) -> httpx.Response:
            return _response(
                {
                    "output": [{"content": "НЕИЗВЕСТНО"}],
                    "stats": {"model_load_time_seconds": 0.1},
                }
            )

    monkeypatch.setattr(quality.httpx, "Client", FakeClient)
    report = quality.run_quality_evaluation(dataset)

    assert report["checks"]["zero_model_load_events"] is False
    assert report["gate_passed"] is False
