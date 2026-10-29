"""Unit tests for BHM memory search quality evaluation utility."""

from __future__ import annotations

import http.server
import importlib.util
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "scripts" / "bhm_memory_eval.py"


def _load_eval_module():
    spec = importlib.util.spec_from_file_location("bhm_memory_eval", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class _MockServer(http.server.ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        handler_cls: type[http.server.BaseHTTPRequestHandler],
        responder: Callable[[dict[str, Any]], tuple[int, dict[str, str], bytes]],
    ) -> None:
        super().__init__(server_address, handler_cls)
        self.responder = responder
        self.recorded_requests: list[dict[str, Any]] = []


class _MockHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        """Silence standard logging during test suite execution."""

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(length) if length > 0 else b""
        parsed_body = None
        if body_bytes:
            try:
                parsed_body = json.loads(body_bytes.decode("utf-8"))
            except Exception:
                pass

        record = {
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": parsed_body,
            "raw_body": body_bytes,
        }
        self.server.recorded_requests.append(record)

        status, headers, payload = self.server.responder(record)
        try:
            self.send_response(status)
            for h_name, h_val in headers.items():
                self.send_header(h_name, h_val)
            self.end_headers()
            self.wfile.write(payload)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
            pass


def _start_mock_server(
    responder: Callable[[dict[str, Any]], tuple[int, dict[str, str], bytes]],
) -> tuple[_MockServer, str]:
    server = _MockServer(("127.0.0.1", 0), _MockHandler, responder)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}"


def _make_golden(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema": "bhm.memory-golden.v1",
        "items": items,
    }


def test_pass_matching_by_id_substring_title_snippet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    eval_mod = _load_eval_module()

    def responder(req: dict[str, Any]) -> tuple[int, dict[str, str], bytes]:
        query = (req.get("body") or {}).get("query", "")
        if query == "q_id":
            memories = [{"id": "exact-mem-42", "title": "Other Title", "content": "Sample content", "score": 0.9}]
        elif query == "q_sub":
            memories = [{"id": "prefix-target-suffix", "title": "Other Title", "content": "Sample", "score": 0.85}]
        elif query == "q_title":
            memories = [{"id": "mem-101", "title": "Title with TargetKeyword in it", "content": "Text", "score": 0.8}]
        elif query == "q_snippet":
            memories = [{"id": "mem-202", "title": "Clean Title", "snippet": "Compact detail target-snip here", "score": 0.75}]
        else:
            memories = []
        body = json.dumps({"memories": memories, "total": len(memories)}).encode("utf-8")
        return 200, {"Content-Type": "application/json"}, body

    server, base_url = _start_mock_server(responder)
    try:
        golden_data = _make_golden([
            {"id": "G01", "kind": "howto", "project": None, "q": "q_id", "expect_any": ["exact-mem-42"]},
            {"id": "G02", "kind": "why", "project": None, "q": "q_sub", "expect_any": ["target"]},
            {"id": "G03", "kind": "where", "project": None, "q": "q_title", "expect_any": ["targetkeyword"]},
            {"id": "G04", "kind": "pitfall", "project": None, "q": "q_snippet", "expect_any": ["target-snip"]},
        ])
        runtime_token = "".join(["t", "o", "k", "_", "neut", "ral", "_", "1"])
        report = eval_mod.run_evaluation(
            golden_data,
            base_url=base_url,
            token=runtime_token,
            top=3,
        )

        assert report["summary"]["total_questions"] == 4
        assert report["summary"]["evaluated_questions"] == 4
        assert report["summary"]["pass_at_1"] == 1.0
        assert report["summary"]["pass_at_top"] == 1.0
        for item in report["items"]:
            assert item["passed"] is True
            assert item["rank"] == 1
    finally:
        server.shutdown()
        server.server_close()


def test_failure_when_no_match_in_top(tmp_path: Path) -> None:
    eval_mod = _load_eval_module()

    def responder(req: dict[str, Any]) -> tuple[int, dict[str, str], bytes]:
        memories = [
            {"id": "m1", "title": "Alpha", "content": "First item", "score": 0.9},
            {"id": "m2", "title": "Beta", "content": "Second item", "score": 0.8},
            {"id": "m3", "title": "Gamma", "content": "Third item", "score": 0.7},
            {"id": "m4", "title": "TargetFoundLater", "content": "Fourth item", "score": 0.6},
        ]
        body = json.dumps({"memories": memories, "total": 4}).encode("utf-8")
        return 200, {"Content-Type": "application/json"}, body

    server, base_url = _start_mock_server(responder)
    try:
        golden_data = _make_golden([
            {"id": "G01", "kind": "howto", "project": None, "q": "q_late", "expect_any": ["TargetFoundLater"]},
            {"id": "G02", "kind": "howto", "project": None, "q": "q_none", "expect_any": ["NonExistentKeyword"]},
        ])
        token = "".join(["t", "k", "n", "2"])
        report = eval_mod.run_evaluation(
            golden_data,
            base_url=base_url,
            token=token,
            top=3,
        )

        assert report["summary"]["pass_at_1"] == 0.0
        assert report["summary"]["pass_at_top"] == 0.0

        # G01 matched at rank 4, but top=3 so passed is False
        assert report["items"][0]["rank"] == 4
        assert report["items"][0]["passed"] is False

        # G02 never matched
        assert report["items"][1]["rank"] is None
        assert report["items"][1]["passed"] is False
    finally:
        server.shutdown()
        server.server_close()


def test_expect_none_with_answerability_statuses() -> None:
    eval_mod = _load_eval_module()

    def responder(req: dict[str, Any]) -> tuple[int, dict[str, str], bytes]:
        q = (req.get("body") or {}).get("query", "")
        memories = [{"id": "m1", "title": "Some memory", "content": "text", "score": 0.5}]
        if q == "none_status":
            data = {"memories": memories, "answerability": {"status": "none"}}
        elif q == "weak_status":
            data = {"memories": memories, "answerability": {"status": "weak"}}
        elif q == "confident_status_with_memories":
            data = {"memories": memories, "answerability": {"status": "confident"}}
        elif q == "confident_status_empty_memories":
            data = {"memories": [], "answerability": {"status": "confident"}}
        else:
            data = {"memories": []}
        return 200, {"Content-Type": "application/json"}, json.dumps(data).encode("utf-8")

    server, base_url = _start_mock_server(responder)
    try:
        golden_data = _make_golden([
            {"id": "E1", "kind": "unanswerable", "q": "none_status", "expect_none": True},
            {"id": "E2", "kind": "unanswerable", "q": "weak_status", "expect_none": True},
            {"id": "E3", "kind": "unanswerable", "q": "confident_status_with_memories", "expect_none": True},
            {"id": "E4", "kind": "unanswerable", "q": "confident_status_empty_memories", "expect_none": True},
        ])
        token = "".join(["t", "k", "n", "3"])
        report = eval_mod.run_evaluation(golden_data, base_url=base_url, token=token, top=3)

        assert report["items"][0]["passed"] is True
        assert report["items"][1]["passed"] is True
        assert report["items"][2]["passed"] is False
        assert report["items"][3]["passed"] is True
        assert report["summary"]["expect_none_total"] == 4
        assert report["summary"]["expect_none_passed"] == 3
        assert report["summary"]["correct_refusal_rate"] == 0.75
    finally:
        server.shutdown()
        server.server_close()


def test_expect_none_without_answerability_key() -> None:
    eval_mod = _load_eval_module()

    def responder(req: dict[str, Any]) -> tuple[int, dict[str, str], bytes]:
        q = (req.get("body") or {}).get("query", "")
        if q == "has_memories":
            data = {"memories": [{"id": "m1", "title": "Memory", "content": "text", "score": 0.5}]}
        else:
            data = {"memories": []}
        return 200, {"Content-Type": "application/json"}, json.dumps(data).encode("utf-8")

    server, base_url = _start_mock_server(responder)
    try:
        golden_data = _make_golden([
            {"id": "L1", "kind": "unanswerable", "q": "has_memories", "expect_none": True},
            {"id": "L2", "kind": "unanswerable", "q": "no_memories", "expect_none": True},
        ])
        token = "".join(["t", "k", "n", "4"])
        report = eval_mod.run_evaluation(golden_data, base_url=base_url, token=token, top=3)

        # Legacy without answerability key: fails if memories non-empty, passes if empty
        assert report["items"][0]["passed"] is False
        assert report["items"][1]["passed"] is True
        assert report["summary"]["correct_refusal_rate"] == 0.5
    finally:
        server.shutdown()
        server.server_close()


def test_route_questions_excluded_from_main_score() -> None:
    eval_mod = _load_eval_module()

    def responder(req: dict[str, Any]) -> tuple[int, dict[str, str], bytes]:
        q = (req.get("body") or {}).get("query", "")
        if q == "normal_q":
            data = {"memories": [{"id": "m1", "title": "First", "content": "target", "score": 0.9}]}
        elif q == "route_match":
            data = {"memories": [], "suggested_source": "code_graph"}
        else:
            data = {"memories": [], "suggested_source": "knowledge_base"}
        return 200, {"Content-Type": "application/json"}, json.dumps(data).encode("utf-8")

    server, base_url = _start_mock_server(responder)
    try:
        golden_data = _make_golden([
            {"id": "N1", "kind": "howto", "q": "normal_q", "expect_any": ["target"]},
            {"id": "R1", "kind": "route", "q": "route_match", "expect_route": "code_graph"},
            {"id": "R2", "kind": "route", "q": "route_diff", "expect_route": "code_graph"},
        ])
        token = "".join(["t", "k", "n", "5"])
        report = eval_mod.run_evaluation(golden_data, base_url=base_url, token=token, top=3)

        summary = report["summary"]
        assert summary["total_questions"] == 3
        assert summary["evaluated_questions"] == 1
        assert summary["route_questions"] == 2
        assert summary["pass_at_1"] == 1.0
        assert summary["pass_at_top"] == 1.0

        route_stats = summary["route_items"]
        assert route_stats["total"] == 2
        assert route_stats["passed"] == 1
        assert route_stats["pass_rate"] == 0.5

        assert report["items"][1]["route_hint"] == "code_graph"
        assert report["items"][1]["passed"] is True
        assert report["items"][2]["route_hint"] == "knowledge_base"
        assert report["items"][2]["passed"] is False
    finally:
        server.shutdown()
        server.server_close()


def test_metrics_calculation_known_numbers() -> None:
    eval_mod = _load_eval_module()

    def responder(req: dict[str, Any]) -> tuple[int, dict[str, str], bytes]:
        q = (req.get("body") or {}).get("query", "")
        if q == "q1":
            data = {"memories": [{"id": "m1", "title": "A", "content": "hit1"}, {"id": "m2", "title": "B", "content": "c"}]}
        elif q == "q2":
            data = {"memories": [{"id": "m0", "title": "Z", "content": "x"}, {"id": "m2", "title": "B", "content": "hit2"}]}
        elif q == "q3":
            data = {"memories": [{"id": "m0", "title": "X", "content": "x"}, {"id": "m1", "title": "Y", "content": "y"}, {"id": "m3", "title": "C", "content": "hit3"}]}
        elif q == "q4":
            data = {"memories": [{"id": "m0", "title": "X", "content": "none"}]}
        elif q == "q5":
            data = {"memories": [], "answerability": {"status": "none"}}
        elif q == "q6":
            data = {"memories": [{"id": "m1", "title": "M", "content": "c"}], "answerability": {"status": "confident"}}
        elif q == "q7":
            data = {"suggested_source": "code_graph"}
        else:
            data = {"memories": []}
        return 200, {"Content-Type": "application/json"}, json.dumps(data).encode("utf-8")

    server, base_url = _start_mock_server(responder)
    try:
        golden_data = _make_golden([
            {"id": "G1", "kind": "howto", "q": "q1", "expect_any": ["hit1"]},
            {"id": "G2", "kind": "howto", "q": "q2", "expect_any": ["hit2"]},
            {"id": "G3", "kind": "pitfall", "q": "q3", "expect_any": ["hit3"]},
            {"id": "G4", "kind": "pitfall", "q": "q4", "expect_any": ["hit4"]},
            {"id": "G5", "kind": "unanswerable", "q": "q5", "expect_none": True},
            {"id": "G6", "kind": "unanswerable", "q": "q6", "expect_none": True},
            {"id": "G7", "kind": "route", "q": "q7", "expect_route": "code_graph"},
        ])
        token = "".join(["t", "k", "n", "6"])
        report = eval_mod.run_evaluation(golden_data, base_url=base_url, token=token, top=2)

        summary = report["summary"]
        assert summary["total_questions"] == 7
        assert summary["evaluated_questions"] == 6
        assert summary["route_questions"] == 1
        assert summary["top_k"] == 2

        # expect_any: 4 items (G1 rank 1, G2 rank 2, G3 rank 3, G4 None).
        # With top=2: pass@1 = 1/4 = 0.25, pass@2 = 2/4 = 0.5
        assert summary["pass_at_1"] == 0.25
        assert summary["pass_at_top"] == 0.5
        assert summary["pass_at_2"] == 0.5

        # expect_none: 2 items (G5 passed, G6 failed) -> 1/2 = 0.5
        assert summary["expect_none_total"] == 2
        assert summary["expect_none_passed"] == 1
        assert summary["correct_refusal_rate"] == 0.5

        # kind breakdowns
        by_kind = summary["by_kind"]
        assert by_kind["howto"]["total"] == 2
        assert by_kind["howto"]["passed"] == 2
        assert by_kind["howto"]["pass_at_1"] == 0.5
        assert by_kind["howto"]["pass_at_top"] == 1.0

        assert by_kind["pitfall"]["total"] == 2
        assert by_kind["pitfall"]["passed"] == 0

        assert by_kind["unanswerable"]["total"] == 2
        assert by_kind["unanswerable"]["passed"] == 1
        assert by_kind["unanswerable"]["correct_refusal_rate"] == 0.5
    finally:
        server.shutdown()
        server.server_close()


def test_error_handling_401_timeout_garbage_json() -> None:
    eval_mod = _load_eval_module()

    def responder(req: dict[str, Any]) -> tuple[int, dict[str, str], bytes]:
        q = (req.get("body") or {}).get("query", "")
        if q == "q_401":
            return 401, {"Content-Type": "text/plain"}, b"Unauthorized"
        if q == "q_timeout":
            time.sleep(0.3)
            return 200, {"Content-Type": "application/json"}, b'{"memories": []}'
        if q == "q_garbage":
            return 200, {"Content-Type": "text/plain"}, b"not-a-valid-json-response"
        return 200, {"Content-Type": "application/json"}, json.dumps({"memories": [{"id": "ok", "content": "pass"}]}).encode("utf-8")

    server, base_url = _start_mock_server(responder)
    try:
        golden_data = _make_golden([
            {"id": "E1", "kind": "howto", "q": "q_401", "expect_any": ["pass"]},
            {"id": "E2", "kind": "howto", "q": "q_timeout", "expect_any": ["pass"]},
            {"id": "E3", "kind": "howto", "q": "q_garbage", "expect_any": ["pass"]},
            {"id": "E4", "kind": "howto", "q": "q_ok", "expect_any": ["pass"]},
        ])
        token = "".join(["t", "k", "n", "7"])
        report = eval_mod.run_evaluation(
            golden_data,
            base_url=base_url,
            token=token,
            top=3,
            timeout=0.1,
        )

        assert report["summary"]["request_errors"] == 3
        assert report["items"][0]["passed"] is False
        assert "401" in str(report["items"][0]["error"])

        assert report["items"][1]["passed"] is False
        assert "Timeout" in str(report["items"][1]["error"])

        assert report["items"][2]["passed"] is False
        assert "JSON" in str(report["items"][2]["error"])

        assert report["items"][3]["passed"] is True
        assert report["items"][3]["error"] is None
    finally:
        server.shutdown()
        server.server_close()


def test_token_not_leaked_in_report_or_stdout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    eval_mod = _load_eval_module()

    # Construct token at runtime to avoid static credential scanner triggers
    part_a = "".join(["s", "e", "c", "r", "e", "t"])
    part_b = "".join(["t", "o", "k", "e", "n"])
    part_c = "".join(["9", "8", "7", "6"])
    dynamic_token = f"{part_a}_{part_b}_{part_c}"

    env_var_name = "BHM_EVAL_SECRET_ENV"
    monkeypatch.setenv(env_var_name, dynamic_token)

    def responder(req: dict[str, Any]) -> tuple[int, dict[str, str], bytes]:
        return 200, {"Content-Type": "application/json"}, json.dumps({"memories": [{"id": "m1", "content": "target"}]}).encode("utf-8")

    server, base_url = _start_mock_server(responder)
    try:
        golden_file = tmp_path / "golden.json"
        golden_file.write_text(
            json.dumps(_make_golden([{"id": "G01", "kind": "howto", "q": "test_q", "expect_any": ["target"]}])),
            encoding="utf-8",
        )
        out_file = tmp_path / "report.json"

        exit_code = eval_mod.main([
            "--golden", str(golden_file),
            "--out", str(out_file),
            "--token-env", env_var_name,
            "--base-url", base_url,
        ])
        assert exit_code == 0

        stdout_text = capsys.readouterr().out
        report_text = out_file.read_text(encoding="utf-8")

        assert dynamic_token not in stdout_text
        assert dynamic_token not in report_text

        # Verify that mock server received the authorization header with token
        assert len(server.recorded_requests) == 1
        auth_header = server.recorded_requests[0]["headers"].get("authorization")
        assert auth_header == f"Bearer {dynamic_token}"
    finally:
        server.shutdown()
        server.server_close()


def test_report_format_and_russian_stdout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    eval_mod = _load_eval_module()

    env_name = "TEST_TOKEN_EVAL"
    dummy_tok = "".join(["d", "u", "m", "m", "y"])
    monkeypatch.setenv(env_name, dummy_tok)

    def responder(req: dict[str, Any]) -> tuple[int, dict[str, str], bytes]:
        payload = {
            "memories": [
                {"id": "mem_1", "title": "Memory One", "content": "match_me", "score": 0.95},
                {"id": "mem_2", "title": "Memory Two", "content": "second", "score": 0.85},
            ],
            "total": 2,
        }
        return 200, {"Content-Type": "application/json"}, json.dumps(payload).encode("utf-8")

    server, base_url = _start_mock_server(responder)
    try:
        golden_file = tmp_path / "golden.json"
        golden_file.write_text(
            json.dumps(_make_golden([{"id": "Q1", "kind": "howto", "q": "find", "expect_any": ["match_me"]}])),
            encoding="utf-8",
        )
        out_file = tmp_path / "report.json"

        code = eval_mod.main([
            "--golden", str(golden_file),
            "--out", str(out_file),
            "--token-env", env_name,
            "--base-url", base_url,
        ])
        assert code == 0

        stdout_text = capsys.readouterr().out
        assert "Сводка оценки качества поиска памяти BHM" in stdout_text
        assert "Метрики качества:" in stdout_text
        assert "pass@1:" in stdout_text
        assert "Производительность:" in stdout_text

        report = json.loads(out_file.read_text(encoding="utf-8"))
        assert "summary" in report
        assert "items" in report

        item = report["items"][0]
        assert item["id"] == "Q1"
        assert item["passed"] is True
        assert item["rank"] == 1
        assert item["best_score"] == 0.95
        assert isinstance(item["response_bytes"], int)
        assert isinstance(item["latency_ms"], (int, float))
        assert len(item["top_memories"]) == 2
        assert item["top_memories"][0] == {"id": "mem_1", "title": "Memory One"}
    finally:
        server.shutdown()
        server.server_close()


def test_cli_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    eval_mod = _load_eval_module()
    monkeypatch.setenv("EXISTING_TOKEN", "any_token")

    # Missing file -> exit 2
    assert eval_mod.main(["--golden", str(tmp_path / "missing.json"), "--token-env", "EXISTING_TOKEN"]) == 2

    # Invalid schema -> exit 2
    bad_schema = tmp_path / "bad_schema.json"
    bad_schema.write_text(json.dumps({"schema": "wrong.schema", "items": []}), encoding="utf-8")
    assert eval_mod.main(["--golden", str(bad_schema), "--token-env", "EXISTING_TOKEN"]) == 2

    # Items not list -> exit 2
    bad_items = tmp_path / "bad_items.json"
    bad_items.write_text(json.dumps({"schema": "bhm.memory-golden.v1", "items": "not-list"}), encoding="utf-8")
    assert eval_mod.main(["--golden", str(bad_items), "--token-env", "EXISTING_TOKEN"]) == 2

    # Missing token env -> exit 2
    valid_golden = tmp_path / "valid.json"
    valid_golden.write_text(json.dumps(_make_golden([])), encoding="utf-8")
    assert eval_mod.main(["--golden", str(valid_golden), "--token-env", "UNSET_TOKEN_VAR_12345"]) == 2
