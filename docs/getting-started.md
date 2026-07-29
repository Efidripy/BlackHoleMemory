# Быстрый старт

## Требования

- Windows 10/11;
- Python 3.12+;
- [uv](https://docs.astral.sh/uv/);
- Docker Desktop с Docker Compose для локального Qdrant.

## Установка

```powershell
git clone https://github.com/Efidripy/BlackHoleMemory.git
cd BlackHoleMemory
uv sync --locked
```

## Запуск

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-bhm-authoritative.ps1
Invoke-WebRequest http://127.0.0.1:8000/health/ready
```

Основные адреса:

- API: `http://127.0.0.1:8000/bhm/`;
- MCP: `http://127.0.0.1:8000/mcp`;
- Galaxy UI: `http://127.0.0.1:8000/bhm/galaxy`;
- Qdrant dashboard: `http://127.0.0.1:6333/dashboard/`.

Для остановки и диагностики используйте штатные команды Docker Desktop и
скрипты из `scripts/`. Runtime state не коммитится в Git.
