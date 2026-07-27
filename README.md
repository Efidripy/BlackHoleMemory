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
