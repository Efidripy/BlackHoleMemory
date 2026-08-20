# Data hygiene

`bhm-data-hygiene.py` выполняет одноразовую fail-closed очистку только для
точных disposable project IDs из `config/data-hygiene-policy.json`. Политика не
поддерживает wildcard, prefix, regex или эвристику по словам `test`, `smoke` и
`demo`. Защищённые проекты не могут входить в allowlist.

Операция не создаёт второй полный backup и не запускает `VACUUM`. До
планирования оператор передаёт существующий проверенный full SQLite backup.
`prepare` создаёт только selective rollback package для выбранных строк. Новые
receipts, projection proofs и rollback packages размещаются исключительно в
`.runtime/data-hygiene/` и не добавляются в Git.

## Двухфазный flow

1. Создайте read-only plan, указав существующий full backup. Сохраните `as_of`,
   `memory_ids` и `plan_digest`.
2. Остановите authoritative API и projection sidecar. Выполните offline
   `prepare` с теми же `as_of` и digest. Фаза создаёт selective rollback package
   и tombstone-ит live disposable records.
3. Поднимите API и sidecar, drain-ните transactional outbox. Выполните
   `projection-check`: он проверяет детерминированные point IDs во всех
   применимых Qdrant collections и создаёт fail-closed proof. Pending, failed,
   dead-letter events, найденные points или Qdrant errors блокируют продолжение.
4. Повторите read-only `plan` с тем же full backup. После tombstone/drain
   authoritative snapshot изменился, поэтому для `purge` нужны новые,
   проверенные post-prepare `as_of` и `plan_digest`.
5. Снова остановите API и sidecar. Выполните offline `purge` с post-prepare
   digest и полным projection absence proof.
6. При необходимости выполните offline `restore` из selective rollback
   package, затем снова drain-ните projection.

Qdrant и LLM можно оставить запущенными: offline boundary относится к API и
projection sidecar — процессам, которые могут писать authoritative SQLite.

## Команды

Все примеры выполняются из корня репозитория. `prepare` и `purge` должны
дословно получать `--as-of` из plan, который авторизует соответствующую фазу.

```powershell
$run = ".\.runtime\data-hygiene\YYYYMMDDTHHMMSSZ"
New-Item -ItemType Directory -Force -Path $run | Out-Null
$backup = ".\.runtime\backups\sqlite-retention\BACKUP_TIMESTAMP\memories-before-retention.sqlite3"

uv run python .\scripts\bhm-data-hygiene.py `
  --output "$run\plan-before-prepare.json" `
  plan --existing-backup $backup --as-of 2026-08-20T12:00:00Z
```

После проверки plan остановите API и sidecar:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-bhm-projection-sidecar.ps1 -Action Stop
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-bhm-authoritative.ps1 -StopOnly

uv run python .\scripts\bhm-data-hygiene.py `
  --output "$run\prepare.json" `
  prepare `
  --existing-backup $backup `
  --rollback-package "$run\rollback.zip" `
  --expected-plan-digest <PREPARE_PLAN_DIGEST> `
  --as-of 2026-08-20T12:00:00Z `
  --confirm-offline
```

Поднимите API/sidecar, drain-ните outbox и сформируйте proof:

```powershell
uv run python .\scripts\bhm-data-hygiene.py `
  --output "$run\projection-absence.json" `
  projection-check --qdrant-url http://127.0.0.1:6333
```

Proof пригоден для purge только при `complete=true`, пустых `present` и
`errors`, а `absent_ids` должны покрывать все `memory_ids` post-prepare plan.
Неполная проверка всё равно сохраняет диагностический report, но CLI завершается
с ненулевым кодом и такой файл отклоняется командой `purge`.

Получите и проверьте post-prepare plan, затем снова остановите API и sidecar:

```powershell
uv run python .\scripts\bhm-data-hygiene.py `
  --output "$run\plan-before-purge.json" `
  plan --existing-backup $backup --as-of <PURGE_AS_OF>

uv run python .\scripts\bhm-data-hygiene.py `
  --output "$run\purge.json" `
  purge `
  --existing-backup $backup `
  --expected-plan-digest <PURGE_PLAN_DIGEST> `
  --as-of <PURGE_AS_OF> `
  --projection-absence-report "$run\projection-absence.json" `
  --confirm-offline
```

Аварийный selective restore:

```powershell
uv run python .\scripts\bhm-data-hygiene.py `
  --output "$run\restore.json" `
  restore --rollback-package "$run\rollback.zip" --confirm-offline
```

Любое изменение authoritative snapshot, policy, backup или plan digest должно
завершать команду ошибкой, а не расширять scope.
