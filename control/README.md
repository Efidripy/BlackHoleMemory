# Workspace control helpers

Этот каталог содержит только небольшие versioned helpers, необходимые для локального hook/observation flow.

- `scripts/shared/BhmObservationIdentity.ps1` — канонический resolver identity для BHM observations;
- `scripts/shared/Invoke-Utf8Script.ps1` — безопасный UTF-8 wrapper для PowerShell entrypoints.

Runtime logs, credentials, local workspace state и backup-артефакты сюда не добавляются.
