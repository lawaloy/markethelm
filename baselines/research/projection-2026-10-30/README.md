# Projection research snapshot: October 30, 2026

This is **unqualified retrospective research**, not an approved model or a
qualified observed baseline. The archive has 3,076 verified scored forecasts
across 174 symbols and 18 run dates. It passes all evidence requirements except
the minimum 20 scored run dates; outcome coverage is 98.118%. No scoring change
is promoted by this study.

Source: [projection evidence workflow run 41156746107](https://github.com/lawaloy/markethelm/actions/runs/41156746107),
artifact `projection-forward-archive` (ID `13025626473`). The workflow ran October
31 UTC for the October 30 New York snapshot. [source.json](source.json) records
archive/database/report hashes. [validation.json](validation.json) contains all
metrics, the exact split, fitted parameters, sample floors, and evidence failures.
Only aggregate research outputs are committed; the real quote archive remains a
workflow artifact with its configured retention.

## Fixed temporal comparison

Protocol `frozen-temporal-v1` fits on 1,365 forecasts from September 29 through
October 8. It excludes 855 otherwise earlier forecasts whose outcomes were not
persisted before the earliest held-out generation time. The entire October 16,
19, 20, 21, and 22 run dates form the 856-forecast holdout. Parameters are frozen
before scoring those dates; held-out outcomes never enter the fit.

| Held-out measure                         | Current heuristic |                                       Reference / research candidate |
| ---------------------------------------- | ----------------: | -------------------------------------------------------------------: |
| Mean absolute price error                |            4.038% |                                              Unchanged price: 3.950% |
| Directional accuracy                     |           50.117% |                                                   Always up: 58.995% |
| Confidence Brier score (lower is better) |            0.3081 | Cohort candidate: 0.2567; training rate: 0.2514; neutral 50%: 0.2500 |
| Low/high coverage                        |            5.491% |                      Residual range candidate: 75.701% (80% nominal) |
| Mean low/high width / origin price       |            0.583% |                                    Residual range candidate: 11.216% |

The current midpoint loses to unchanged price by 0.087 percentage points of mean
absolute error. Equal run-date weighting yields a similar deficit; it wins three
dates and loses two, so the win count alone would hide the error magnitude.
Cohort confidence reduces overconfidence but fails both confidence references.
The range candidate improves coverage by widening substantially and still misses
its nominal target. These results do not justify promotion.

The confidence candidate uses training-only Laplace-smoothed success frequencies
in the original confidence cohorts, falling back to the overall training rate
when a cohort has fewer than 30 samples. The range candidate is centered on the
existing midpoint with a finite-sample 80th-percentile absolute residual,
normalized by origin price. There is no midpoint optimization or hyperparameter
search on the holdout.

## Reproduce and continue

Download the source workflow artifact and extract it to a directory containing
`market_bars.sqlite`. Unset `MARKETHELM_DATABASE_URL` and run:

```bash
python3 scripts/projection_baseline.py validate --data-dir /path/to/archive --days 365 \
  > validation.json
```

The command makes a consistent SQLite backup before evaluation. Its input hash
identifies that exact backup; `source.json` also records the original downloaded
database hash. SQLite versions can change backup page encoding/hashes; compare
metrics and provenance with the recorded inputs when reproducing elsewhere.

This archive's outcomes were inspected before this study. Overlapping dates and
stocks are correlated, and five held-out dates are not enough to establish
stability across regimes. Rolling reports are diagnostics, not fresh independent
tests. Keep collecting until the original evidence gate passes; the workflow will
capture a qualified report plus hashed validation automatically. Preserve this
protocol and require fresh forward outcomes for any future candidate promotion.
