# Использование

BlackHoleMemory — локальная память для AI-агентов. SQLite WAL хранит
authoritative lifecycle и metadata, Mem0 отвечает за semantic/logical layer,
Qdrant используется как восстанавливаемая vector projection, а LangGraph — для
оркестрации stateful flows.

## Authority and projection

SQLite is the authoritative store. In the canonical authoritative runtime the
in-process projection worker is intentionally disabled by the SQLite authority
guard; this is a protection boundary, not a runtime failure. A separate,
launcher-managed projection sidecar continuously consumes the transactional
outbox in `sqlite-shadow` mode and writes only the rebuildable Qdrant
projection. Qdrant must never become a second source of truth. The
compatibility-sidecar boundary is documented in
[`docs/architecture-authority.md`](architecture-authority.md).

The sidecar is started automatically by the authoritative launcher. Its status
and bounded logs are local runtime artifacts:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-bhm-projection-sidecar.ps1 -Action Status
```

If the sidecar was stopped or a bounded backlog needs recovery, use the
operator flow:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\bhm-projection-operator.ps1 -Action drain -MaxCycles 32
```

The flow temporarily runs the explicit projection worker in `sqlite-shadow`
mode, then restores the canonical `sqlite-authoritative` runtime and verifies
that pending and failed projection events are zero.

Metadata-only changes are projected with a stable
`projection_payload_digest`. If the vector text is unchanged, the sidecar uses
Qdrant `set_payload` instead of recomputing an embedding. Points created before
this digest existed are treated as stale once and refreshed from SQLite.

## Memory refinery

The refinery normalizes project aliases, tags, display titles, summaries and
taxonomy metadata without changing storage lifecycle or applying retention.
The safe flow always creates three distinct databases: a sealed rollback
backup, a writable rehearsal copy and a restore probe.

```powershell
uv run python .\scripts\run-memory-refinery.py rehearse `
  --database .\.runtime\live-memory\memories.sqlite3 `
  --backup .\.runtime\refinery\rollback-20260812.sqlite3 `
  --working-copy .\.runtime\refinery\working-20260812.sqlite3 `
  --restore-probe .\.runtime\refinery\restore-probe-20260812.sqlite3 `
  --plan .\.runtime\refinery\normalization-plan-20260812.json `
  --receipt .\.runtime\refinery\rehearsal-receipt-20260812.json
```

The rehearsal applies only to `--working-copy`, verifies the rollback backup's
SHA-256 before and after the run, restores it into `--restore-probe`, and
compares the logical SQLite fingerprint. Live apply requires both an exact plan
digest and `--allow-live` for every CLI apply target; any intervening
authoritative write makes the plan
stale and fails closed. All output paths must be distinct and must not already
exist, so a sealed backup or prior receipt cannot be overwritten. Historical
graph retention and `VACUUM` use the separate reviewed
[SQLite retention](sqlite-retention.md) flow; alias-orphan deletion is never
implied by refinery.
Stop the API and projection sidecar for the rehearsal/apply maintenance window;
Qdrant and the LLM may remain running. Tombstoned storage rows stay tombstoned,
and ambiguous provenance remains unset with an explicit unresolved count in the
plan instead of being guessed as synthetic.

## Disposable data hygiene

Одноразовая очистка подтверждённых disposable namespaces отделена от refinery
и retention. Она использует точный policy allowlist, существующий full backup и
selective rollback package. Active records сначала tombstone-ятся offline в
`prepare`, затем projection sidecar должен drain-нуть удаления, а
`projection-check` — подтвердить отсутствие каждого ID в Qdrant. Только после
нового post-prepare plan отдельное offline-окно разрешает физический `purge`.
`VACUUM` не является частью операции.

CLI, полный порядок остановки/запуска и restore описаны в
[Data hygiene](data-hygiene.md).

## Freshness and review inventory

`audit-bhm-freshness-review.py` is a bounded, read-only baseline for review
planning. It opens the SQLite authority using `mode=ro` plus `PRAGMA
query_only=ON`; it does not write SQLite, Qdrant, Mem0, lifecycle state, or
review status. Pin `--as-of` for a reproducible digest and treat an exit code
of `2` as an incomplete bounded scan, not a clean baseline.

```powershell
uv run python .\scripts\audit-bhm-freshness-review.py `
  --database .\.runtime\live-memory\memories.sqlite3 `
  --as-of 2026-08-21T12:00:00Z `
  --max-records 5000 `
  --sample-limit 50 `
  --output .\.runtime\freshness-review\baseline.json
```

The report contains only project names, aggregate counts and hashed memory
references: never raw memory content, source references, paths, provenance or
source digests. The reason codes (`source_changed`, `superseded_by_revision`,
`contradicted`, `unreferenced`, `age_threshold_reached`) are review signals
only. In particular, age alone can never archive, tombstone or delete a
memory. Until WL-295.2 persists freshness-candidate decision events, review
latency and freshness false-positive rate may correctly be reported as
`unavailable` rather than fabricated as zero.

## Offline hybrid retrieval evaluation

`run-bhm-hybrid-retrieval-evaluation.py` is WL-295.3's frozen-fixture gate for
a *possible* project-filtered SQLite FTS5/BM25 candidate lane. It compares the
unchanged current BHM ranker, candidate expansion, and equal-weight RRF. The
SQLite database is `:memory:` and contains only synthetic fixture rows; the
command does not open authoritative SQLite or call Qdrant, Mem0, a model or
the network. It never enables a runtime feature flag.

```powershell
uv run python .\scripts\run-bhm-hybrid-retrieval-evaluation.py `
  --cases 120 `
  --repeats 11 `
  --output .\.runtime\hybrid-retrieval-evaluation\baseline.json
```

Only a measurable `Recall@5` gain, zero project-isolation regression, and an
end-to-end RRF p95 increase of no more than 20% may return
`propose-feature-flag`. Any other result is `defer`: current retrieval remains
the only production path. A passing local fixture gate still requires a
separate feature-flag design, production-like validation and operator review.

## Galaxy controls

Galaxy is a visual read model of active memory records from authoritative
SQLite. Records are ordered by `updated_at` newest first and can be scoped to
one project or all projects. The amount selector provides `50`, `200`, `500`,
`1000`, and `ALL`; `ALL` walks every SQLite page and does not impose a hidden
total-record cap. The status line always discloses the returned and available
memory counts.

The visible sidebar is intentionally limited to visual exploration controls:
project, amount, whole-graph or selected-node focus, hop depth, reload, and fit.
Runtime, MCP, CBM, raw-filter, domain, relation-preset, and layout diagnostics
are not part of the Galaxy navigation. Persisted SQLite memory links are shown
when both endpoints are present in the selected record set. New memory events
refresh the open visualization when the record is not already visible.

The browser UI is launcher-session-bound: start the trusted BHM launcher, which
uses the caller bearer boundary and passes a one-time bootstrap token in the
URL fragment. Bare anonymous browser requests to `/bhm/ui/session/bootstrap`
are rejected even on loopback; do not expose `/` or `/bhm/*` through an
untrusted reverse proxy, and do not treat `X-Forwarded-*` headers as an
authentication signal. A proxy deployment must preserve the loopback boundary
and explicitly authenticate before forwarding requests.

### Launcher operator tools

Откройте `TOOLS` справа в trusted launcher. Левая навигация остаётся только
инструментами доступа к BHM и Galaxy. Правая панель содержит семь workflows:
integrity audit, verified SQLite backup, offline restore, retention cleanup,
strict index repair, bounded Qdrant projection rebuild и admin snapshot
export/import. Read-only действия выполняются сразу; мутации сначала показывают
preview, затем требуют backup и явное подтверждение. Пути restore/import
ограничены соответствующими `.runtime` каталогами, а admin REST-вызовы требуют
caller token и `BHM_ADMIN_CAPABILITY`.

## MCP

Канонический локальный endpoint:

```text
http://127.0.0.1:8000/mcp
```

Подключайте агента к серверу `bhm` через Streamable HTTP. Для проверки сначала
убедитесь, что `/health/ready` возвращает успешный ответ.

### Лимит и очередь сессий

Транспорт держит до 32 одновременно admitted MCP-сессий. Если лимит занят,
новые `initialize` ждут в FIFO-очереди и не создают SDK-сессию заранее. После
`DELETE`, idle expiry или transport loss освобождённый слот получает первый
ожидающий клиент. Состояние доступно в `/bhm/health` в полях
`active_count`, `max_sessions`, `queued_count` и `pending_count`.

Очередь ограничивает только MCP transport lifecycle; она не является очередью
записи памяти и не создаёт записи в BHM сама по себе.

REST/MCP clients should use the shared [BHM error taxonomy](error-taxonomy.md)
instead of parsing free-form error messages.

### Bounded repository indexing

`bhm_index_repository` выполняет не более 666 файлов за один MCP-вызов и
возвращает устойчивый `job_id`, progress и `index_next`. Если `status=running`,
повторите вызов с полями из `index_next`; forced refresh продолжается с
`force_refresh=false`, чтобы не создать новый epoch.
Continuation fields also include `expected_job_id` and `expected_state_digest`; pass them
unchanged so a repository mutation between slices fails closed instead of silently
starting a different job.

Построение code graph намеренно отделено от индексного slice. После
`status=completed` ответ содержит `graph_next` с точным `snapshot_id`.
Передайте этот receipt обратно в `bhm_index_repository`; `graph_only=true`
fail-closed отклонит незавершённый или устаревший snapshot. Быстрый graph build
по-прежнему возвращает готовый graph в том же ответе. Если построение занимает
больше 20 секунд, BHM возвращает `graph_operation.status=running` и `poll_next`;
опрос через `bhm_index_status` показывает `running`, `completed` или `failed`.
Повтор того же `graph_next` дедуплицируется по project/root/snapshot/parser digest
и не запускает второй build. Внутренние operation-specific deadlines остаются
60 секунд для bounded index, 120 секунд для graph и 30 секунд для status/coverage,
но deferred receipt укладывает native MCP-вызов в его 30-секундную границу.

`bhm_index_status` и `bhm_check_index_coverage` используют быстрый schema probe и
пятисекундный repository probe. При временной занятости SQLite или превышении
probe budget они возвращают явный `freshness_status` и `retryable=true`, а не
маскируют состояние общим upstream timeout.

## Принцип безопасности

Операции изменения кода и данных proposal-only по умолчанию. Деструктивные
операции требуют явного действия оператора; локальные credentials, базы,
runtime logs и raw payload не являются частью public repository.
