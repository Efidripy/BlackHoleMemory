---
description: Compare BHM low, standard and deep context profiles using runtime metrics
---

Run the lightweight profile comparison and report the runtime-metric verdict:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\plugins\bhm-codex-connector\scripts\bhm-profile.ps1 -Action compare -AsJson
```

Summarize:
- recommended profile
- score and p95 latency for `standard`, `deep` and `low-context`
- successful/failed context calls and contract violations
- whether any of these errors appeared:
  - `Context size has been exceeded`
  - `circuit_breaker_open`
  - `Summarize failed`
  - `Compression failed`
  - `Graph extraction failed`

The comparison is read-only on the BHM data path. Profile switching still
creates the normal reversible native-env backup, and the final profile is
restored to `low-context`.
