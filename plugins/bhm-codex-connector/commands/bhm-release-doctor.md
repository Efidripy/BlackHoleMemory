---
description: Run the unified BHM install/update/doctor/native-attach operator gate
---

Run the plugin-local wrapper in read-only doctor mode:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\.codex\plugins\local\bhm-codex-connector\scripts\bhm-release-operator.ps1" -Action doctor -AsJson
```

For native attach, use `-Action native-attach`. The report must distinguish
configured MCP entries from a live Streamable HTTP session; configuration alone
is not an attach claim. Install/update/rollback require explicit `-Confirm`, a target
outside a repository checkout, and an operator-selected backup root.
