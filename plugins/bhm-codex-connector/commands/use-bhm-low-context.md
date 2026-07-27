---
description: Switch BHM to the recommended low-context profile on this workstation
---

Switch the local BHM runtime to the recommended `low-context` profile and restart the worker:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\plugins\bhm-codex-connector\scripts\bhm-profile.ps1 -Action low-context -RestartWorker -AsJson
```

After the command returns, report:
- whether the profile switch succeeded
- whether the worker restarted healthy
- that `low-context` is the current recommended safe default for this workstation
