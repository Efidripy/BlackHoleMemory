#!/usr/bin/env python
"""Run an explicitly disposable local Qdrant semantic LongMemEval-S receipt."""

from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path

from blackholememory.config import settings
from blackholememory.external_evaluation_admission import load_external_evaluation_admission_report
from blackholememory.filesystem_boundaries import replace_bytes_safely
from blackholememory.local_endpoint_policy import open_local_url
from blackholememory.local_endpoint_policy import read_bounded_response
from blackholememory.local_endpoint_policy import validate_local_endpoint
from blackholememory.longmemeval_semantic import LongMemEvalSemanticError
from blackholememory.longmemeval_semantic import run_longmemeval_qdrant_semantic_smoke
from blackholememory.runtime_endpoints import endpoint_url


_MAX_EMBEDDING_RESPONSE_BYTES = 512 * 1024


class LocalEmbeddingAdapter:
    """Minimal local-only OpenAI-compatible embedding adapter for evaluation."""

    def __init__(self, endpoint: str, model: str) -> None:
        self.endpoint = validate_local_endpoint(endpoint)
        self.model = str(model or "").strip()
        if not self.model:
            raise LongMemEvalSemanticError("local embedding model is required")

    def embed(self, text: str, _memory_action: str | None = None) -> list[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str] | tuple[str, ...], memory_action: str = "search") -> list[list[float]]:
        _ = memory_action
        payload = json.dumps({"model": self.model, "input": [str(text) for text in texts]}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.endpoint}/embeddings",
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with open_local_url(request, endpoint=self.endpoint, timeout=60.0) as response:
                # A 16-item, 768-dimensional local embedding batch can exceed
                # the generic provider-response ceiling (256 KiB) while still
                # being well inside this evaluator's fixed, local-only bound.
                body = read_bounded_response(response, limit=_MAX_EMBEDDING_RESPONSE_BYTES)
            parsed = json.loads(body.decode("utf-8"))
        except Exception as exc:
            raise LongMemEvalSemanticError("local embedding endpoint request failed") from exc
        data = parsed.get("data") if isinstance(parsed, dict) else None
        if not isinstance(data, list) or len(data) != len(texts):
            raise LongMemEvalSemanticError("local embedding endpoint returned an incomplete batch")
        by_index: dict[int, list[float]] = {}
        for item in data:
            if not isinstance(item, dict) or not isinstance(item.get("index"), int) or not isinstance(item.get("embedding"), list):
                raise LongMemEvalSemanticError("local embedding endpoint returned an invalid response")
            by_index[int(item["index"])] = item["embedding"]
        if set(by_index) != set(range(len(texts))):
            raise LongMemEvalSemanticError("local embedding endpoint response indexes are invalid")
        return [by_index[index] for index in range(len(texts))]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--admission-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-cases", type=int, default=50)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument(
        "--candidate-scope",
        choices=("case-local", "global"),
        default="case-local",
        help="case-local is the historical control; global uses only the project Qdrant filter and records abstentions",
    )
    parser.add_argument(
        "--minimum-score",
        type=float,
        default=None,
        help="required global cosine-score threshold for a returned candidate; it is evidence-only and never activates runtime policy",
    )
    parser.add_argument(
        "--embedding-endpoint",
        default=endpoint_url("lm_studio"),
        help="local OpenAI-compatible embedding endpoint; defaults to the launcher-aligned LM Studio loopback endpoint",
    )
    parser.add_argument(
        "--embedding-model",
        default=settings.mem0_embedding_model,
        help="local embedding model identifier",
    )
    parser.add_argument("--allow-disposable-qdrant", action="store_true", help="create then delete one isolated evaluation collection")
    args = parser.parse_args()
    try:
        if args.candidate_scope == "case-local" and args.minimum_score is not None:
            raise LongMemEvalSemanticError("--minimum-score is available only with --candidate-scope global")
        if args.candidate_scope == "global" and args.minimum_score is None:
            raise LongMemEvalSemanticError("--candidate-scope global requires an explicit --minimum-score")
        adapter = LocalEmbeddingAdapter(args.embedding_endpoint, args.embedding_model)
        result = run_longmemeval_qdrant_semantic_smoke(
            args.dataset,
            dataset_version=args.dataset_version,
            admission_report=load_external_evaluation_admission_report(args.admission_report),
            allow_disposable_qdrant=args.allow_disposable_qdrant,
            max_cases=args.max_cases,
            k=args.k,
            embedder=adapter,
            candidate_scope=args.candidate_scope,
            minimum_score=args.minimum_score,
        )
        output_dir = args.output_dir.expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        for name, value in result.items():
            replace_bytes_safely(output_dir / f"{name}.json", (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
        print(json.dumps({"ok": True, "report": result["report"]}, ensure_ascii=False, sort_keys=True))
        return 0
    except (LongMemEvalSemanticError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
