# SQLite retention

BlackHoleMemory сохраняет immutable snapshots индекса репозитория и code
graph. Authoritative memories не удаляются этой политикой. Retention очищает
только старую историю индексации и завершённые outbox events.

## Автоматическая очистка

В SQLite-authoritative runtime она включена по умолчанию. Первый цикл
запускается через 5 минут, затем раз в 6 часов. Один цикл ограничен двумя graph
snapshots, двумя index snapshots и 250 completed outbox events. Текущие
указатели, два последних исторических snapshot на project/root, snapshot моложе
7 дней, convention bindings, active jobs и последний completed event каждого
aggregate сохраняются. Completed outbox моложе 30 дней также не удаляется.

Автоматический цикл никогда не делает backup или `VACUUM`. Освободившиеся
страницы повторно использует SQLite, поэтому файл перестаёт бесконтрольно расти,
но сам размер файла уменьшается только в отдельное maintenance window.

## Dry-run и офлайн-очистка

Сначала сохраните неизменный план:

```powershell
uv run python .\scripts\bhm-sqlite-retention.py `
  --as-of 2026-08-20T10:02:00Z `
  --receipt .\.runtime\retention\plan.json
```

Остановите API и projection sidecar; Qdrant и LLM можно оставить запущенными.
Возьмите точные `as_of` и `plan_digest` из receipt, затем примените план:

```powershell
uv run python .\scripts\bhm-sqlite-retention.py `
  --apply --offline --until-stable --vacuum `
  --as-of 2026-08-20T10:02:00Z `
  --confirm-plan-digest <digest> `
  --backup .\.runtime\backups\sqlite-retention\<timestamp>\memories.sqlite3 `
  --receipt .\.runtime\retention\apply.json
```

Apply создаёт и проверяет rollback backup до первого `DELETE`. Каждый bounded
цикл повторно строит план под `BEGIN IMMEDIATE`, проверяет digest, foreign keys
и логические graph/index/FTS/convention связи. `--vacuum` выполняется один раз
после всех циклов. При изменении базы старый digest отклоняется до удаления.

`--reuse-backup` разрешён только для повторной попытки после ошибки до первого
commit; существующий backup заново проходит integrity verification.

## Rollback

Остановите все SQLite writers, сохраните повреждённую базу как evidence,
восстановите проверенный backup, выполните `PRAGMA quick_check` и
`PRAGMA foreign_key_check`, затем запустите authoritative launcher и заново
согласуйте Qdrant projection из SQLite.
