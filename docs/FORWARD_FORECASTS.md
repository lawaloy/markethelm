# Immutable forecasts and forward evaluation

New live tracker runs record the existing heuristic and simple benchmarks before
their outcomes happen. This ledger is separate from the mutable daily projection
view used by the dashboard. It does not change customer-facing price targets.
The first [ridge return candidate](SHADOW_MODEL.md) joins new cases in shadow
mode after its frozen artifact timestamp; prior cases remain unchanged.

## What is preserved

Migration 9 adds six append-only tables to the shared application database. Local
mode creates the same tables in `DATA_DIR/market_bars.sqlite` on the first live
recording. A consistent transaction records:

- the frozen `prospective-v1` experiment and its criteria;
- model version, definition, parameters and implementation fingerprints;
- forecast identity, symbol, generation/recording times, completed origin session,
  exact fifth XNYS target session, and origin price;
- the numeric quote inputs actually available, their provider timestamp and SHA-256;
- the heuristic, unchanged-price, always-up and always-down references together;
- the frozen ridge return shadow candidate on new cases after its enrollment time;
- verified outcome observations and the first exact-session score for each forecast.

Inputs use an explicit allowlist of quote fields. Credentials and arbitrary
provider payloads are not stored. Model definitions must include any learned
parameters/artifact hashes and feature definitions when adding future candidates.

SQLite and PostgreSQL triggers reject ordinary UPDATE and DELETE operations.
The storage API only appends: an exact retry does nothing, a changed output for
an existing identity fails, and a later generation creates a separate case.
A model cannot be attached later to an old case. Changed model definitions require
a new version, and changing frozen experiment criteria requires a new protocol.
The source fingerprint also detects changes to the existing projector, so its
version must advance when its implementation changes. Changing the incumbent
requires a new protocol as well, preserving the old comparison contract.
The protocol also freezes the scoring rule version and implementation hash. A changed
scorer cannot silently add scores to an older experiment.

## Collecting and scoring

Recording runs automatically in the CLI/dashboard tracker and hosted scheduled
collector, immediately after projections are generated and before AI-summary work.
A recording failure fails the run; it is not silently treated as valid evidence.
Malformed or old projection rows are counted as skipped, not backfilled. Rows with
missing/stale input timestamps can be retained but are excluded from the primary
comparison, with exclusion counts in the report.

Primary inputs require an aware provider timestamp at or after fifteen minutes
before the completed origin session's close, on that session's date, and no later
than forecast generation. Every admitted forecast is durably recorded before its
exact target close, with generation and recording sharing the same completed-
session anchor.

Finnhub's verified `pc` becomes the outcome for the preceding XNYS session. Only
new live observations are accepted, with finite positive closes and matching
session/timestamp provenance. The tracker requests quote-only data for due stocks
that have left the screener, without adding those stocks to the new forecast
universe. These extra requests use the existing bounded provider retries and the
hosted collector's overall timeout.

Scores use the first available accepted observation for the exact target session.
They are not rolled to a later close. Repeated observations do not duplicate
scores. Corrections are retained as separate observations and do not rewrite the
original score. A missed provider window remains explicitly missing; historical
outcome recovery from another verified source is not implemented here.

## Reports and commands

```bash
markethelm forecasts report --output data/forward-forecast-report.json
markethelm forecasts report --data-dir data --days 365
markethelm forecasts score --data-dir data
```

With `MARKETHELM_DATABASE_URL` set, these commands use the shared database;
otherwise they use the `DATA_DIR`/`--data-dir` sidecar. `report` reads recorded
scores and does not fetch data or register an experiment. `score` only processes
already recorded outcomes; it makes no provider requests. Both run without API
keys. Use `--protocol` on `report` to inspect a previously frozen experiment.

A fresh ledger reports `not_started`. Once recording begins it reports
`collecting`, then `evaluating` as outcomes arrive. An empty report is valid and
must not be represented as a validated model. Historical daily projection rows
never become prospective rows simply because this feature was enabled.

Reports include model manifests, protocol hashes, paired benchmark comparisons,
per-origin-session results, pending/missing counts, exclusions and readiness
failures. The primary cohort selects the first verified case per stock and
completed origin session; repeated refreshes remain auditable without increasing
the primary sample count. Selection happens across the full experiment before
applying a report window, so a later refresh cannot replace an original forecast
that has aged out of the window. Price comparisons require the same case identities on
both sides and weight origin sessions equally, while individual forecast metrics
also show sample-weighted values. Direction references do not claim price targets.

The scheduled evidence workflow archives `forward-forecast-report.json` alongside
the cumulative database. Qualified observed captures include `forward.json` and
its SHA-256 in the manifest, using the same private database snapshot. Prospective
readiness is independent of the older retrospective qualification gate.

## Frozen review criteria

These criteria are declared before new outcome collection, not selected to fit
an observed winner:

| Requirement                                                  | Minimum                                                                                                                      |
| ------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------- |
| Scored paired cases                                          | 200                                                                                                                          |
| Scored completed origin sessions                             | 20                                                                                                                           |
| Symbols                                                      | 25                                                                                                                           |
| Mature paired-outcome coverage                               | 90%                                                                                                                          |
| Relative improvement in equally weighted session price error | 5% versus unchanged price, and the incumbent for candidates                                                                  |
| Origin sessions with lower error                             | 60% against each required price reference                                                                                    |
| Confidence, when supplied                                    | 90% coverage; two cohorts of 30; Brier score below neutral 0.25 and the paired incumbent when available                      |
| Ranges, when supplied                                        | Complete scored-case coverage and at least 75% observed coverage against an 80% nominal target; width requires manual review |

The report can return `manual_review_required` after every numeric requirement
passes. It never changes the application's active model. Overlapping horizons and
stocks are correlated; twenty sessions and the sample floors are evidence hygiene,
not proof of profitability or independent statistical trials. Stability across
time, data quality, coverage/width tradeoffs and fresh forward results still need
review before a customer-facing model change.

## Deployment and persistence

Back up the hosted database before applying migration 9 and deploy the matching
application image. Existing migration policy rejects an older application against
a newer schema. No live market data is fetched by the migration.

Include the ledger in database/sidecar backups and restores. There is no ledger
pruning or mutation API; size it for the retained forecast history. Operator-level
schema restore is distinct from changing individual recorded predictions.
