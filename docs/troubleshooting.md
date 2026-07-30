# Диагностика

1. Проверьте readiness:

   ```powershell
   Invoke-WebRequest http://127.0.0.1:8000/health/ready
   ```

2. Если endpoint недоступен, запустите authoritative launcher из
   `scripts/start-bhm-authoritative.ps1` и проверьте Docker Desktop/Qdrant.
3. Если MCP не подключается, проверьте адрес `http://127.0.0.1:8000/mcp` и
   имя сервера `bhm`.
4. Не переносите в Git диагностические логи и raw receipts. Их место — в
   локальной `.local/`-зоне.

## Launcher без GUI-зависимостей

Launcher можно безопасно проверить в headless-среде без запуска окна:

```powershell
uv run python scripts\bhm_launcher.py --check-dependencies
```

Команда завершится с кодом `0`, если PyQt6 доступен, и с кодом `1` с
понятной диагностикой, если он отсутствует. Импорт `bhm_launcher` не читает
stdin и не устанавливает пакеты автоматически. Если GUI действительно нужен,
установите зависимость вручную:

```powershell
python -m pip install PyQt6
```

Если проблема воспроизводится после чистой установки, приложите версию,
команду запуска и обезличенный ответ readiness; секреты и полные payload не
прикладывайте.

## Health endpoints и диагностические данные

Анонимный health-контракт намеренно минимален:

- `/health/live` — процесс отвечает;
- `/health/ready` — возвращает только `ok` и `status` (`ready` или
  `not_ready`).

Внутренние сведения о хранилищах, Qdrant, очередях, MCP-сессиях и SLO закрыты
caller authentication. Для их получения используйте bearer credential,
который выдаёт локальный launcher:

- `/health/dependencies`;
- `/health/cutover`;
- `/bhm/health`;
- `/bhm/health/slo`.

Ответ `401 caller_auth_required` на эти маршруты без credential означает
штатную границу раскрытия данных, а не неисправность runtime. Для обычной
проверки запуска достаточно `/health/ready`.
