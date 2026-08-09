# BlackHoleMemory

<div align="center">

**Local, verifiable long-term memory and code intelligence for AI agents.**

[![BHM CI](https://github.com/Efidripy/BlackHoleMemory/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Efidripy/BlackHoleMemory/actions/workflows/ci.yml)
[![CodeQL](https://github.com/Efidripy/BlackHoleMemory/actions/workflows/codeql.yml/badge.svg?branch=main)](https://github.com/Efidripy/BlackHoleMemory/actions/workflows/codeql.yml)
[![Release](https://img.shields.io/github/v/release/Efidripy/BlackHoleMemory?display_name=tag&sort=semver)](https://github.com/Efidripy/BlackHoleMemory/releases)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-Streamable_HTTP-6f42c1)](https://modelcontextprotocol.io/)
[![License: 0BSD](https://img.shields.io/badge/license-0BSD-0a7bbc.svg)](LICENSE)

[English](#english) · [Русский](#русский)

</div>

## English

### What it is

BlackHoleMemory (BHM) is a local-first, self-hosted memory contour for AI coding agents. It connects facts, decisions, tasks, errors, files, and dependencies into bounded, provenance-bearing context that can be searched and checked after a session ends. BHM exposes the same core through a FastAPI REST surface and a local Streamable HTTP MCP endpoint.

### Design at a glance

```text
AI agent / IDE
      │
      ├── REST API       http://127.0.0.1:8000/bhm/
      └── MCP            http://127.0.0.1:8000/mcp
                           │
                    FastAPI + LangGraph
                           │
             SQLite (authoritative lifecycle store)
                 │                       │
       Mem0 semantic/logical       Qdrant rebuildable
             layer                 vector projection
```

The authority boundary is deliberate:

- **SQLite is authoritative** for memory lifecycle, metadata, provenance, and recovery.
- **Mem0 is the semantic/logical layer** used to consolidate and retrieve context.
- **Qdrant is a rebuildable projection**, never a second source of truth.
- **LangGraph** orchestrates stateful agent workflows.
- Destructive or proposal-only operations stay behind explicit operator controls; BHM does not silently edit code or data.

### Capabilities

- Project-scoped memory, search, context compilation, tasks, sessions, and checkpoints.
- Repository indexing, bounded code search, code-graph and architecture queries, and change-impact previews.
- Provenance-aware MCP tools with caller/project authorization, bounded outputs, and fail-closed transport checks.
- Optional Galaxy/workbench views for local operators. The browser UI is launcher-bound: direct anonymous navigation intentionally requires a trusted local bootstrap token.
- Rebuildable Qdrant projection with SQLite parity and read-only lifecycle/telemetry reports.

### Quick start (Windows)

Requirements: Windows 10/11, Python 3.12+, [uv](https://docs.astral.sh/uv/), and Docker Desktop with Compose for Qdrant.

```powershell
git clone https://github.com/Efidripy/BlackHoleMemory.git
cd BlackHoleMemory
uv sync --locked
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-bhm-authoritative.ps1
Invoke-RestMethod http://127.0.0.1:8000/health/ready
```

Canonical local endpoints:

| Surface | URL |
| --- | --- |
| BHM API | `http://127.0.0.1:8000/bhm/` |
| MCP | `http://127.0.0.1:8000/mcp` |
| Galaxy UI | `http://127.0.0.1:8000/bhm/galaxy` (open through the trusted launcher) |
| Readiness | `http://127.0.0.1:8000/health/ready` |
| Qdrant dashboard | `http://127.0.0.1:6333/dashboard/` |

For the packaged Windows launcher, use [`scripts/build-release.ps1`](scripts/build-release.ps1). Release archives and detached verification sidecars are produced outside the source tree.

### MCP integration

The canonical server id is `bhm` and the transport is local Streamable HTTP. Keep caller credentials in environment/configuration outside Git:

```toml
[mcp_servers.bhm]
url = "http://127.0.0.1:8000/mcp"
env.BHM_CALLER_TOKEN = "${BHM_CALLER_TOKEN}"
```

See [`docs/mcp-caller-token-runbook.md`](docs/mcp-caller-token-runbook.md) for token handling and [`docs/architecture-authority.md`](docs/architecture-authority.md) for the authority model. Configured MCP inventory is informational until the current client has verified an attached native session.

### Evidence and benchmark boundaries

The checked-in [BHM Value Benchmark](docs/benchmarks/bhm-value-benchmark.md) is a deterministic frozen-fixture study: 1,000 unique cases replayed 10 times (10,000 case evaluations) across controlled no-memory, file-only, vector, and BHM modes. It demonstrates retrieval, provenance, context-budget, and leakage behavior on that fixture; it is not universal model quality or real-user telemetry.

The separate local-model replay uses 111 cases × 3 repetitions for `file-only` and `bhm-full` (666 calls) with frozen prompts and `temperature=0`. Its receipt is local operational evidence, not a hosted-service benchmark.

### Security and data boundaries

BHM is designed for an authorized local development environment. Keep `.runtime`, `.docs`, `.src`, credentials, databases, logs, and release signing keys local. SQLite remains the recovery anchor; Qdrant may be rebuilt from it. Review the [security policy](SECURITY.md), [error taxonomy](docs/error-taxonomy.md), and [troubleshooting guide](docs/troubleshooting.md) before enabling operator workflows.

### Documentation, contributing, and license

- Start with [`docs/getting-started.md`](docs/getting-started.md), [`docs/usage.md`](docs/usage.md), and [`docs/configuration.md`](docs/configuration.md).
- Read [`docs/README.md`](docs/README.md) for the documentation map.
- Run the project gates before submitting changes: `uv run pytest -q`, `uv run ruff check src tests scripts`, `uv run python -m compileall -q src scripts tests`, and the documented release/public-tree validators.
- Contributions are welcome through issues and pull requests. Preserve the SQLite-authoritative boundary and do not commit secrets or runtime artifacts.

BHM-authored code and documentation are released under the [0BSD license](LICENSE). Third-party components retain their own licenses and provenance records.

## Русский

BlackHoleMemory (BHM) — локальная self-hosted память и контур code intelligence для AI-агентов. Он сохраняет факты, решения, задачи, ошибки, файлы и зависимости с областью проекта и provenance, а затем возвращает проверяемый контекст через REST и локальный MCP Streamable HTTP.

Архитектурное правило неизменно: **SQLite — единственный authoritative store**, Mem0 — семантический слой, Qdrant — восстанавливаемая projection, LangGraph — оркестрация. Разрушительные и proposal-only действия требуют явного operator control. Galaxy/workbench — локальная launcher-bound UI-поверхность; прямой anonymous URL намеренно требует доверенный bootstrap.

Быстрый запуск:

```powershell
uv sync --locked
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-bhm-authoritative.ps1
Invoke-RestMethod http://127.0.0.1:8000/health/ready
```

Точки доступа: API — `http://127.0.0.1:8000/bhm/`, MCP — `http://127.0.0.1:8000/mcp`, Galaxy — `http://127.0.0.1:8000/bhm/galaxy` через доверенный launcher. Подробности: [`docs/getting-started.md`](docs/getting-started.md), [`docs/usage.md`](docs/usage.md), [`docs/mcp-caller-token-runbook.md`](docs/mcp-caller-token-runbook.md).

Benchmark в репозитории — frozen-fixture evidence, а не telemetry реальных пользователей. Основной прогон: 1,000 кейсов × 10 повторов; отдельный local-model replay: 111 × 3 для двух режимов, всего 666 вызовов. Секреты, базы, `.runtime`, `.docs`, `.src` и ключи подписи не публикуются.

Лицензия материалов BHM — [0BSD](LICENSE); зависимости сохраняют собственные лицензии.
