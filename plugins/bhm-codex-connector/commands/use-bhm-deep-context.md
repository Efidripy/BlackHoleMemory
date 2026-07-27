---
description: Switch BHM to the deep context profile on this workstation
---

Switch the local BHM runtime to the `deep` profile and restart the worker:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\plugins\bhm-codex-connector\scripts\bhm-profile.ps1 -Action deep -RestartWorker -AsJson
```

Report the selected profile, backup path and runtime verification result.
