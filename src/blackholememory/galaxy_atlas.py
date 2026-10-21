"""Atlas read model: every active memory as one 3D point, laid out from its real embedding.

Read-only. SQLite stays authoritative; vectors are read from Qdrant and never written back.
The layout is expensive (O(N^2) per iteration), so it is built in a background thread and
cached on disk keyed by a cheap digest of the memory store.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import numpy as np

SCHEMA = "bhm.galaxy-atlas.v1"
MIN_REBUILD_SECONDS = 600.0
MIN_BUILD_RETRY_SECONDS = 30.0
LAYOUT_ITERATIONS = 260
KNN = 3
KNN_MIN_SIMILARITY = 0.6
_REL_KIND = {
    "depends_on": 1,
    "supersedes": 2,
    "duplicate_of": 3,
    "conflicts_with": 4,
    "condenses": 5,
    "details": 5,
    "relates_to": 6,
    "belongs_to_project_hub": 6,
}


def _slug(project: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", project.lower()).strip("_")


def _connect_ro(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{Path(database_path).as_posix()}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def store_digest(database_path: Path) -> str:
    """Cheap change detector: active count, newest update and link count."""

    connection = _connect_ro(database_path)
    try:
        active, newest = connection.execute(
            "SELECT COUNT(*), COALESCE(MAX(updated_at), '') FROM memories WHERE lifecycle = 'active'"
        ).fetchone()
        links = connection.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0]
    finally:
        connection.close()
    return hashlib.sha256(f"{SCHEMA}|{active}|{newest}|{links}".encode()).hexdigest()[:24]


def _load_vectors(qdrant_url: str, projects: list[str], wanted: set[str]) -> dict[str, np.ndarray]:
    vectors: dict[str, np.ndarray] = {}
    base = qdrant_url.rstrip("/")
    with httpx.Client(timeout=120.0, trust_env=False, follow_redirects=False) as client:
        names = [item["name"] for item in client.get(f"{base}/collections", timeout=30.0).json()["result"]["collections"]]
        for project in projects:
            slug = _slug(project)
            for name in names:
                if name != f"bhm_local_memory_{slug}" and not name.startswith(f"bhm_projection_generation_{slug}_"):
                    continue
                offset: Any = None
                while True:
                    body: dict[str, Any] = {"limit": 256, "with_payload": ["source_id"], "with_vector": True}
                    if offset is not None:
                        body["offset"] = offset
                    response = client.post(f"{base}/collections/{name}/points/scroll", json=body, timeout=120.0)
                    if response.status_code != 200:
                        break
                    result = response.json()["result"]
                    for point in result["points"]:
                        source_id = (point.get("payload") or {}).get("source_id")
                        vector = point.get("vector")
                        if isinstance(vector, dict):
                            vector = next(iter(vector.values()), None)
                        if source_id in wanted and vector and source_id not in vectors:
                            vectors[source_id] = np.asarray(vector, dtype=np.float32)
                    offset = result.get("next_page_offset")
                    if offset is None:
                        break
    return vectors


def _similarity_edges(vectors: np.ndarray, rows: np.ndarray) -> dict[tuple[int, int], tuple[int, float]]:
    """Faint cosine kNN edges (kind 0) plus a minimum spanning tree skeleton (kind 7)."""

    edges: dict[tuple[int, int], tuple[int, float]] = {}
    count = len(rows)
    if count < 2:
        return edges
    k = min(KNN, count - 1)
    for start in range(0, count, 512):
        sim = vectors[start : start + 512] @ vectors.T
        for r in range(sim.shape[0]):
            sim[r, start + r] = -1
        top = np.argpartition(-sim, k - 1, axis=1)[:, :k] if k < count else np.argsort(-sim, axis=1)[:, :k]
        for r in range(sim.shape[0]):
            a = int(rows[start + r])
            for t in top[r]:
                if sim[r, t] < KNN_MIN_SIMILARITY:
                    continue
                b = int(rows[t])
                edges[(min(a, b), max(a, b))] = (0, float(sim[r, t]))
    inf = 1e9
    in_tree = np.zeros(count, dtype=bool)
    in_tree[0] = True
    best = (1 - (vectors @ vectors[0])).astype(np.float32)
    best[0] = inf
    parent = np.zeros(count, dtype=np.int64)
    for _ in range(count - 1):
        nxt = int(np.argmin(np.where(in_tree, inf, best)))
        in_tree[nxt] = True
        a, b = int(rows[nxt]), int(rows[parent[nxt]])
        edges[(min(a, b), max(a, b))] = (7, float(1 - best[nxt]))
        d = (1 - (vectors @ vectors[nxt])).astype(np.float32)
        upd = (d < best) & ~in_tree
        best = np.where(upd, d, best)
        parent = np.where(upd, nxt, parent)
    return edges


def _layout(
    positions: np.ndarray,
    edge_pairs: np.ndarray,
    project_index: np.ndarray,
    project_count: int,
) -> tuple[np.ndarray, np.ndarray]:
    """ForceAtlas2-style 3D layout with one virtual 'mother' hub per project."""

    rng = np.random.default_rng(7)
    n = len(positions)
    deg = np.bincount(edge_pairs.ravel(), minlength=n).astype(np.float32) + 1
    psize = np.bincount(project_index, minlength=project_count).astype(np.float32)
    hub = rng.normal(0, 1, (project_count, 3))
    hub = hub / np.maximum(np.linalg.norm(hub, axis=1, keepdims=True), 1e-9) * 200
    pos = positions + hub[project_index] * 0.6
    kr, kg, kh = 2.2, 0.05, 0.03
    for it in range(LAYOUT_ITERATIONS):
        disp = np.zeros_like(pos)
        hub_disp = np.zeros_like(hub)
        for s0 in range(0, n, 500):
            d = pos[s0 : s0 + 500, None, :] - pos[None, :, :]
            dist2 = (d**2).sum(-1) + 4.0
            disp[s0 : s0 + 500] += (d * (deg[s0 : s0 + 500, None] * deg[None, :] / dist2)[..., None]).sum(1) * kr
        d = pos[edge_pairs[:, 0]] - pos[edge_pairs[:, 1]]
        dist = np.linalg.norm(d, axis=1, keepdims=True) + 1e-3
        f = d / dist * np.log1p(dist)
        np.add.at(disp, edge_pairs[:, 0], -f)
        np.add.at(disp, edge_pairs[:, 1], f)
        dm = pos - hub[project_index]
        disp -= dm * kh * deg[:, None] ** 0.5
        np.add.at(hub_disp, project_index, dm * kh * 0.5)
        hd = hub[:, None, :] - hub[None, :, :]
        hd2 = (hd**2).sum(-1) + 400.0
        hub_disp += (hd * ((np.sqrt(psize)[:, None] * np.sqrt(psize)[None, :]) / hd2)[..., None]).sum(1) * 260.0
        disp -= pos * deg[:, None] * kg
        hub_disp -= hub * 0.04
        step = 5.0 * (1 - it / LAYOUT_ITERATIONS) + 0.4
        length = np.linalg.norm(disp, axis=1, keepdims=True) + 1e-9
        pos += disp / length * np.minimum(length / deg[:, None], step)
        hub_length = np.linalg.norm(hub_disp, axis=1, keepdims=True) + 1e-9
        hub += hub_disp / hub_length * np.minimum(hub_length, step * 2)
    centre = pos.mean(0)
    pos -= centre
    hub -= centre
    scale = 380 / (np.percentile(np.abs(pos), 99) or 1)
    return pos * scale, hub * scale


def build_atlas(database_path: Path, qdrant_url: str) -> dict[str, Any]:
    """Build the whole atlas payload. Slow (minutes for a few thousand memories)."""

    connection = _connect_ro(database_path)
    try:
        rows = connection.execute(
            "SELECT memory_id, project, memory_type, title, created_at, updated_at, memory_class, metadata_json "
            "FROM memories WHERE lifecycle = 'active' ORDER BY memory_id"
        ).fetchall()
        links = connection.execute(
            "SELECT source_id, target_id, relation FROM memory_links WHERE relation NOT LIKE 'demo_tree%'"
        ).fetchall()
    finally:
        connection.close()
    ids = [row["memory_id"] for row in rows]
    count = len(ids)
    index = {memory_id: i for i, memory_id in enumerate(ids)}
    projects = sorted({row["project"] for row in rows})
    project_of = {name: i for i, name in enumerate(projects)}
    project_index = np.array([project_of[row["project"]] for row in rows], dtype=np.int64)

    vectors = _load_vectors(qdrant_url, projects, set(ids)) if count else {}
    rng = np.random.default_rng(7)
    positions = rng.normal(0, 60, (count, 3)).astype(np.float32)
    edges: dict[tuple[int, int], tuple[int, float]] = {}
    if len(vectors) >= 2:
        dim = len(next(iter(vectors.values())))
        matrix = np.zeros((count, dim), dtype=np.float32)
        for memory_id, vector in vectors.items():
            if len(vector) == dim:
                matrix[index[memory_id]] = vector
        has_vec = np.linalg.norm(matrix, axis=1) > 0
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        unit = np.divide(matrix, norms, out=np.zeros_like(matrix), where=norms > 0)
        rows_with_vector = np.where(has_vec)[0]
        unit_v = unit[rows_with_vector]
        edges = _similarity_edges(unit_v, rows_with_vector)
        centred = unit_v - unit_v.mean(0)
        _, _, vt = np.linalg.svd(centred[: min(len(centred), 2500)], full_matrices=False)
        seed = centred @ vt[:3].T
        seed /= np.abs(seed).max() or 1
        positions[rows_with_vector] = seed * 120
    positions += rng.normal(0, 2.0, positions.shape)
    all_edges = [(a, b, kind, weight) for (a, b), (kind, weight) in edges.items()]
    for link in links:
        if link["source_id"] in index and link["target_id"] in index:
            all_edges.append((index[link["source_id"]], index[link["target_id"]], _REL_KIND.get(link["relation"], 6), 1.0))
    edge_pairs = np.array([(a, b) for a, b, _, _ in all_edges], dtype=np.int64).reshape(-1, 2)
    if count:
        positions, hubs = _layout(positions.astype(np.float64), edge_pairs, project_index, len(projects))
    else:
        hubs = np.zeros((0, 3))
    importance, used = [], []
    for row in rows:
        try:
            meta = json.loads(row["metadata_json"] or "{}")
        except ValueError:
            meta = {}
        importance.append(float(meta.get("importance_score") or 5))
        used.append(meta.get("last_accessed_at"))
    return {
        "schema": SCHEMA,
        "projects": projects,
        "hubs": [round(float(v), 2) for v in hubs.reshape(-1)],
        "psize": [int(v) for v in np.bincount(project_index, minlength=len(projects))],
        "nodes": {
            "id": ids,
            "title": [(row["title"] or "")[:110] for row in rows],
            "project": project_index.tolist(),
            "type": [row["memory_type"] for row in rows],
            "cls": [row["memory_class"] or "unclassified" for row in rows],
            "created": [row["created_at"] for row in rows],
            "updated": [row["updated_at"] for row in rows],
            "importance": importance,
            "used": used,
        },
        "pos": [round(float(v), 2) for v in positions.reshape(-1)],
        "edges": {
            "a": [int(a) for a, *_ in all_edges],
            "b": [int(b) for _, b, *_ in all_edges],
            "kind": [int(k) for _, _, k, _ in all_edges],
            "w": [round(float(w), 3) for *_, w in all_edges],
        },
    }


class AtlasCache:
    """Disk cache plus a single background builder. Serves the last good snapshot while rebuilding."""

    def __init__(self, cache_path: Path, database_path: Path, qdrant_url: str) -> None:
        self.cache_path = Path(cache_path)
        self.database_path = Path(database_path)
        self.qdrant_url = qdrant_url
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_attempt = 0.0
        self._error = ""

    def _read(self) -> dict[str, Any] | None:
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _build_and_store(self, digest: str) -> None:
        try:
            payload = build_atlas(self.database_path, self.qdrant_url)
            payload["digest"] = digest
            payload["generated_at"] = time.time()
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.cache_path.with_suffix(".tmp")
            temp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            temp.replace(self.cache_path)
            self._error = ""
        except Exception as exc:  # background job: surface the reason through status, never crash the service
            self._error = f"{type(exc).__name__}: {str(exc)[:160]}"

    def snapshot(self) -> dict[str, Any]:
        """Return the cached atlas (possibly stale) or a status stub; starts a rebuild when the store changed."""

        cached = self._read()
        try:
            digest = store_digest(self.database_path)
        except (OSError, sqlite3.Error):
            digest = ""
        stale = cached is None or (digest and cached.get("digest") != digest)
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            if stale and digest and not running and time.monotonic() - self._last_attempt >= (MIN_BUILD_RETRY_SECONDS if cached is None else MIN_REBUILD_SECONDS):
                self._last_attempt = time.monotonic()
                self._thread = threading.Thread(target=self._build_and_store, args=(digest,), name="bhm-atlas-build", daemon=True)
                self._thread.start()
                running = True
        if cached is None:
            return {"ok": True, "status": "building" if running else "error", "error": self._error}
        cached["ok"] = True
        cached["status"] = "ready"
        cached["rebuilding"] = running
        return cached
