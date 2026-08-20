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

## Qdrant или Docker временно недоступен

Канонический SQLite-authoritative launcher сначала поднимает BHM core, затем
восстанавливает Qdrant projection. Поэтому `/health/ready` остаётся `ready`,
если SQLite исправна, а защищённые `/bhm/health` и `/bhm/health/slo` показывают
`degraded`/`breached` до возвращения projection. Сообщение
`remote_qdrant_required_but_unavailable` само по себе не доказывает crash
Qdrant: часто первым недоступен Docker Desktop/WSL engine.

Повторный безопасный запуск:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-bhm-authoritative.ps1
```

`start-qdrant.ps1` ограниченно запускает Docker Desktop, выполняет Compose и
ждёт HTTP `/healthz`. Он не вызывает `wsl --shutdown` и не перезапускает
исправный BHM API. Для специального строгого профиля установите
`BHM_QDRANT_REQUIRED_FOR_CORE=true`; тогда startup/readiness остаются
fail-closed. `Invalid Origin header` для неизвестного origin и Windows
`ConnectionResetError 10054` от закрывшего соединение клиента сами по себе не
означают завершение API.

## `bhm_index_repository` возвращает timeout

Не запускайте повторную force-refresh индексацию вслепую. Сначала вызовите
`bhm_index_status`: завершённый snapshot мог быть опубликован уже после старого
client timeout. В актуальном контракте индекс выполняется bounded slices по 666
файлов и возвращает `index_next`; code graph строится отдельным `graph_next`
только для завершённого snapshot. Если `index_next` присутствует, продолжайте
именно этим receipt — он сохраняет project/root и сбрасывает `force_refresh`,
не создавая дублирующий epoch. Receipt также фиксирует `expected_job_id` и
`expected_state_digest`; если дерево изменилось между срезами, BHM отклонит
продолжение и потребует начать новый индексный epoch.

Если `graph_next` возвращает `graph_operation.status=running`, не запускайте
параллельный build. Передайте `poll_next` в `bhm_index_status`; повторный
`graph_next` безопасен и вернёт тот же `operation_id`. Состояния
`freshness_status=probe_timeout` и `freshness_status=sqlite_busy` означают
временную деградацию read probe: повторите status/coverage после `Retry-After`,
не создавая новый force-refresh epoch.

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
## Ошибки memory refinery

- `refinery plan digest mismatch`: plan или переданный digest не совпадает с
  исходным артефактом. Не редактируйте plan; выполните новый rehearsal.
- `authoritative records changed after the refinery plan was created`: SQLite
  изменилась после планирования. Сохраните старый rollback backup, удалите из
  рабочего flow устаревшую working copy и выполните новый rehearsal.
- `rollback backup digest mismatch`: остановитесь и не выполняйте live apply.
  Сохраните артефакты и определите процесс, изменивший sealed backup.
- Ошибка restore-probe или integrity: остановитесь до live apply. Успешный
  rehearsal требует `quick_check=ok`, ноль foreign-key errors, неизменный
  SHA-256 backup и совпадающие logical fingerprints.
- `atomic refinery apply failed: MemoryRevisionConflict`: authoritative
  snapshot изменился после plan/rehearsal. Не повторяйте старый plan; создайте
  новый immutable rehearsal.
- `atomic refinery apply failed: MemoryRepositoryIntegrityError`: обнаружена
  project alias collision в `upsert_key` или link identity. Не объединяйте
  записи автоматически; вынесите конфликт в reviewed data-quality gate.

После успешной live-нормализации `projection_pending > 0` ожидаем: каждая
изменённая memory создаёт transactional outbox event. Дождитесь drain через
launcher-managed projection sidecar, затем выполняйте Qdrant reconciliation до
canonical `upsert=0`, `delete=0`. `review=0` обязателен только когда нет
классифицированных alias-orphan points; их удаление остаётся отдельным reviewed
gate. Refinery не запускает `VACUUM`, не удаляет historical graph snapshots и
alias orphans.

## Ошибки SQLite retention

- `plan digest mismatch`: база или policy изменились после dry-run. Не
  повторяйте старый apply; создайте новый plan.
- `active_build`: выполняется repository index или строится graph. Дождитесь
  terminal state и повторите dry-run.
- `live retention requires ... listener ... stopped`: API всё ещё владеет
  authoritative SQLite. Остановите API и projection sidecar.
- `insufficient ... free space`: для backup или `VACUUM` недостаточно места;
  не обходите gate удалением rollback evidence.
- `compaction_failed` после `retention_committed=true`: логическое удаление уже
  завершено, но файл не сжат. База может оставаться рабочей; проверьте receipt и
  не повторяйте старый digest.

Полный operator flow: [SQLite retention](sqlite-retention.md).
