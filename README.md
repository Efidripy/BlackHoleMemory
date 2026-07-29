# BlackHoleMemory

Локальная self-hosted память для AI-агентов.

BlackHoleMemory сохраняет контекст проектов между сессиями Codex, Claude и
других инструментов, а затем возвращает его через REST и MCP. Основной сценарий
— Windows, локальная инфраструктура и полный контроль оператора над данными.

## Что это

BlackHoleMemory объединяет:

- SQLite WAL как единственный источник истины для lifecycle и metadata;
- Mem0 как semantic/logical layer;
- Qdrant как восстанавливаемую vector projection;
- LangGraph для orchestration и stateful agent flows;
- FastAPI и Streamable HTTP MCP для подключения инструментов и агентов.

## Зачем

Обычный AI-агент теряет рабочий контекст после завершения сессии. BHM добавляет
долговременную память, которую можно искать, проверять и восстанавливать без
второго authoritative хранилища.

Ключевые свойства:

- local-first и self-hosted deployment;
- SQLite остаётся authoritative, Qdrant можно пересобрать;
- destructive actions требуют явного operator control;
- MCP работает через локальный Streamable HTTP endpoint;
- proposal-only операции не меняют код или данные без явного apply.

## Как это работает

```text
AI agent
   |
   v
REST / MCP
   |
   v
FastAPI + LangGraph
   |
   v
SQLite WAL  ----->  Mem0 semantic layer
   |
   +------------->  Qdrant vector projection
```

## Benchmark

`BHM Value Benchmark v1` сравнивает шесть режимов на 1000 уникальных memory/code-intelligence кейсах, повторённых 10 раз: всего 10 000 case evaluations.

| Режим | Task success | Recall@5 | Citation validity | Leakage | Context tokens |
| --- | ---: | ---: | ---: | ---: | ---: |
| `no-memory` | 0.0% | 0.0% | 0.0% | 0 | 0.0 |
| `file-only` | 0.0% | 100.0% | 80.0% | 3000 | 158.1 |
| `naive-vector` | 0.0% | 87.5% | 80.0% | 3000 | 160.5 |
| `bhm-no-graph` | 75.0% | 100.0% | 100.0% | 0 | 89.9 |
| `bhm-no-filters` | 0.0% | 100.0% | 80.0% | 3000 | 158.1 |
| `bhm-full` | 87.5% | 100.0% | 100.0% | 0 | 89.9 |

На замороженном локальном fixture BHM не просто находит target в top-5: он сохраняет project scope, provenance и bounded context. Без graph channel task success падает до 75%, без safety filtering — до 0% с 3000 leakage. Это `deterministic-local-replay`, не real-user telemetry и не универсальная оценка модели.

[Методика и полный receipt benchmark](docs/benchmarks/bhm-value-benchmark.md)

Для отдельной проверки реального model call доступен `local-model-replay`: 1000
уникальных кейсов × 10 повторов для `file-only` и `bhm-full` (20 000 вызовов), с
фиксированными prompt, `temperature=0`, `max_tokens=96` и `tool_budget=0`. Модель
получает frozen context и не вызывает BHM tools; это не real-user telemetry.
В этом release receipt local-model replay не включён: прогон не завершён, поэтому
его нельзя выдавать за measurement. Контракт и воспроизводимая команда сохранены
в [методике benchmark](docs/benchmarks/bhm-value-benchmark.md).

Канонический локальный MCP endpoint:

```text
http://127.0.0.1:8000/mcp
```

## Установка

Требования:

- Windows 10/11;
- Python 3.12+;
- [uv](https://docs.astral.sh/uv/);
- Docker Desktop с Docker Compose для локального Qdrant.

```powershell
git clone https://github.com/Efidripy/BlackHoleMemory.git
cd BlackHoleMemory
uv sync --locked
```

## Запуск

Запустить authoritative runtime:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-bhm-authoritative.ps1
```

Проверить readiness:

```powershell
Invoke-WebRequest http://127.0.0.1:8000/health/ready
```

После запуска доступны:

- BHM API: `http://127.0.0.1:8000/bhm/`;
- MCP: `http://127.0.0.1:8000/mcp`;
- Galaxy UI: `http://127.0.0.1:8000/bhm/galaxy`;
- Qdrant dashboard: `http://127.0.0.1:6333/dashboard/`.

Для Windows launcher соберите release-артефакт командой `scripts\build-release.ps1`. Бинарные release-артефакты публикуются отдельно от исходного репозитория.

## Благодарности

Спасибо авторам и сообществам [LangGraph](https://github.com/langchain-ai/langgraph),
[Mem0](https://github.com/mem0ai/mem0), [Qdrant](https://github.com/qdrant/qdrant),
[FastAPI](https://github.com/fastapi/fastapi) и [MCP](https://modelcontextprotocol.io/).

И отдельное спасибо всем, кто делился идеями, задавал неудобные вопросы и помогал
довести архитектуру до рабочего состояния.

## Лицензия

[0BSD](LICENSE).
