# BHM Value Benchmark v1

Этот benchmark проверяет влияние retrieval и bounded context assembly на выполнение агентом типовых memory/code-intelligence задач.

## Evidence contract

- Стенд read-only: не вызываются модель, сеть, live BHM runtime, SQLite, Qdrant или Mem0.
- `task_success_rate` — детерминированный proxy: target должен быть первым, в context не должно быть недопустимых записей, citations должны содержать project scope и source evidence.
- Это `deterministic-local-replay`, а не real-user telemetry и не универсальная оценка качества модели.
- Каждый receipt содержит fixture digest, report digest, environment и число повторов.

## Сравниваемые режимы

- `no-memory` — агент без памяти;
- `file-only` — lexical file-like поиск без project/lifecycle фильтра;
- `naive-vector` — сортировка только по semantic score;
- `bhm-no-graph` — project/lifecycle filtering и semantic + lexical fusion без graph channel;
- `bhm-no-filters` — fusion без authoritative project/lifecycle post-filter;
- `bhm-full` — project/lifecycle filtering, semantic + lexical + graph fusion и bounded context compilation.

## Workload

Замороженный fixture содержит 1000 уникальных кейсов четырёх типов: `memory-continuity`, `code-navigation`, `incident-recovery` и `cross-agent`. Он покрывает 8 доменов и 8 вариантов сложности: `direct`, `paraphrase`, `graph`, `scope`, `stale`, `conflict`, `handoff` и `tie`. Каждый кейс содержит target, same-project distractor, cross-project hard negative, archived entry, log entry и graph neighbor.

Команда запуска:

```powershell
uv run python scripts\run-bhm-value-benchmark.py --cases 1000 --repeats 10 --output-dir docs\benchmarks\results\bhm-value-v1-1000-20260728
```

## Результат запуска 2026-07-28

1000 кейсов × 10 повторов, то есть 10 000 case evaluations. Fixture digest: `2a4749348bec4cc7d46fea3c7e872529288515cd5f6db506fba9415ed91d4b1d`.

| Mode | Task success | Recall@5 | Citation validity | Leakage | Context tokens | Runner p95 ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `no-memory` | 0.0% | 0.0% | 0.0% | 0 | 0.0 | 0.591 |
| `file-only` | 0.0% | 100.0% | 80.0% | 3000 | 158.1 | 164.422 |
| `naive-vector` | 0.0% | 87.5% | 80.0% | 3000 | 160.5 | 121.481 |
| `bhm-no-graph` | 75.0% | 100.0% | 100.0% | 0 | 89.9 | 141.277 |
| `bhm-no-filters` | 0.0% | 100.0% | 80.0% | 3000 | 158.1 | 232.583 |
| `bhm-full` | 87.5% | 100.0% | 100.0% | 0 | 89.9 | 147.873 |

### Интерпретация

На этом fixture простые режимы находят target в top-5, но не могут безопасно собрать context: за 10 повторов они пропускают 3000 hard negatives, включая cross-project, archived и log entries. Поэтому их task-success proxy равен 0%.

`bhm-full` сохранил target в top-1 в 87.5% кейсов, обеспечил project-scoped citations и не пропустил leakage. Удаление graph channel снижает task success до 75%. Удаление safety filtering обнуляет task success и возвращает 3000 leakage. Полный BHM использовал на 43.2% меньше context tokens, чем `file-only`.

Это измерение подтверждает полезность BHM filtering/fusion/context contract на данном fixture. Оно не доказывает качество на всех репозиториях, моделях или пользовательских сценариях.

Полные артефакты:

- [summary.md](results/bhm-value-v1-1000-20260728/summary.md)
- [report.json](results/bhm-value-v1-1000-20260728/report.json)

## Local model replay

Для проверки влияния BHM на реальный model call используется тот же frozen fixture, но
с локальной `qwen2.5-coder-7b-instruct` через OpenAI-compatible endpoint. Prompt,
`temperature=0`, `max_tokens=96`, `tool_budget=0` и `enable_thinking=false` фиксированы.
Сравниваются только `file-only` и `bhm-full`; live BHM tools не вызываются, SQLite,
Qdrant и Mem0 не изменяются.

Команда полного прогона:

```powershell
uv run python scripts\run-bhm-local-model-replay.py `
  --cases 1000 `
  --repeats 10 `
  --max-in-flight 4 `
  --max-tokens 96 `
  --tool-budget 0 `
  --output-dir docs\benchmarks\results\bhm-local-model-v1-20260728
```

Это 20 000 model calls: 1000 кейсов × 10 повторов × 2 режима. В текущем release
прогон не завершён и receipt намеренно не публикуется; незавершённый запуск не
считается measurement. После полного завершения receipt можно добавить рядом с
этой методикой.
Этот слой evidence — `local-model-replay`: он измеряет конкретную локальную модель,
зафиксированный prompt и frozen context, но не real-user telemetry и не человеческую
оценку качества ответов.
