## BHM Value Benchmark

Deterministic local replay: 1000 cases × 10 repetitions. Evidence class: `deterministic-local-replay`. No model, network, or live memory backend was used.

| Mode | Task success | Recall@5 | Citation validity | Leakage | Context tokens | Runner p95 ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `no-memory` | 0.0% | 0.0% | 0.0% | 0.0 | 0.0 | 10.698 |
| `file-only` | 0.0% | 100.0% | 80.0% | 3000.0 | 158.1 | 164.422 |
| `naive-vector` | 0.0% | 87.5% | 80.0% | 3000.0 | 160.5 | 121.481 |
| `bhm-no-graph` | 75.0% | 100.0% | 100.0% | 0.0 | 89.9 | 141.277 |
| `bhm-no-filters` | 0.0% | 100.0% | 80.0% | 3000.0 | 158.1 | 232.583 |
| `bhm-full` | 87.5% | 100.0% | 100.0% | 0.0 | 89.9 | 147.873 |

Fixture digest: `2a4749348bec4cc7d46fea3c7e872529288515cd5f6db506fba9415ed91d4b1d`
Report digest: `f3ca0c6fe0b22c7f371b9b6f710cccb6ef0d490234173e10e87eee66d7419b78`

> This benchmark measures retrieval/context impact on a deterministic task proxy. It is not real-user telemetry and does not claim universal model quality.
