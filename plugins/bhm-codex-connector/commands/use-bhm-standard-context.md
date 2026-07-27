---
description: Switch BHM to the standard profile on this workstation
---

Switch the local BHM runtime to the `standard` profile and restart the worker:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\plugins\bhm-codex-connector\scripts\bhm-profile.ps1 -Action standard -RestartWorker -AsJson
```

After the command returns, report:
- whether the profile switch succeeded
- whether the worker restarted healthy
- that `standard` is currently less preferred than `low-context` on this workstation unless a future comparison proves otherwise
