# Usage

How to run the daily tracker after [install](../README.md#quick-start).

---

<a id="entry-points"></a>
<details open>
<summary><b>Entry points</b></summary>

<a id="option-1-cli-recommended-for-daily-use"></a>
<details open>
<summary><b>Option 1: CLI (recommended for daily use)</b></summary>

```bash
# Main entry point: formatted console output
python main.py
```

Or, after `pip install markethelm`:

```bash
markethelm
```

This runs the CLI interface which:

- Shows formatted console output
- Displays top gainers/losers
- Shows index performance
- Prints AI summary (if enabled)

</details>

<a id="option-2-direct-cli-module"></a>
<details>
<summary><b>Option 2: Direct CLI module</b></summary>

```bash
python -m src.cli.commands
```

Same as Option 1, invoked as a module.

</details>

<a id="projection-backtesting"></a>
<details>
<summary><b>Projection backtesting</b></summary>

Evaluate saved projections against closing prices on the exact fifth NYSE
trading session after each run:

```bash
markethelm backtest --data-dir data --days 365
markethelm backtest --data-dir data --days 365 --output data/backtest.json
```

The strict JSON report includes absolute error, directional accuracy, target-band
coverage, confidence calibration, confidence cohorts, recommendation cohorts,
and data-coverage counts. A projection is never rolled to a later close when its
exact target-session close is missing. Use `--horizon-sessions` or `--calendar`
to evaluate another explicit horizon, and `--all-samples` to include more than
the default 300 sample rows.

For new, immutable forecasts and their future outcomes, use the separate
[forward evaluation ledger](FORWARD_FORECASTS.md):

```bash
markethelm forecasts report --data-dir data --output data/forward-forecast-report.json
```

Live tracker runs record versioned heuristic and benchmark forecasts automatically.
The report evaluates the first verified case per stock/origin session, shows
missing outcomes and paired model comparisons, and keeps promotion manual.
It never reclassifies old mutable snapshots as forward evidence.

See the [first shadow model](SHADOW_MODEL.md) for the frozen ridge return candidate, historical diagnostic, offline reproduction and prospective review requirements. Live runs enroll it automatically in new forecast cases; dashboard targets continue to use the incumbent.

The repository also preserves a synthetic scenario baseline that detects changes
to evaluator semantics:

```bash
python3 scripts/projection_baseline.py check
```

Do not use its synthetic metrics to tune confidence; calibration requires a
representative baseline of real projections generated before their outcomes.

Newly fetched quote rows record the provider timestamp and Finnhub's explicit
previous-close value (`pc`) against its verified preceding XNYS session. This keeps
premarket, intraday, holiday, and weekend fetches from being mislabeled by the
snapshot filename. Legacy snapshots remain readable, but they cannot qualify as
calibration evidence and are excluded from observed-baseline metrics.
Weekend and holiday runs use their actual collection date, so they cannot replace
the preceding session's snapshot.

Assess the local forward archive against the documented minimum evidence gate:

```bash
python3 scripts/projection_baseline.py assess --data-dir data --days 365
```

The default gate requires at least 200 scored projections, 90% mature-outcome
coverage, 20 distinct run dates, 25 symbols, and two confidence cohorts with at
least 30 samples each. Every scored projection must have a timezone-aware
generation timestamp and every outcome must identify a verified previous-close
session. Thresholds are minimum evidence hygiene, not proof of model quality.

The `Projection evidence collection` workflow collects one post-close snapshot
on weekdays. One completed-session sample per run date avoids overweighting
highly correlated intraday observations in the five-session calibration data. An
exchange-calendar guard skips holidays and any manual run made before that day's
XNYS close. Collection is restricted to the repository's default branch so
feature-branch experiments cannot contaminate the evidence chain. Eligible runs
restore the latest cumulative `projection-forward-archive` artifact, assess the
combined archive, and upload it again with 90-day retention. An unqualified
assessment is reported as normal progress while evidence matures. A tracker
failure, missing fresh snapshot, malformed assessment, archive error, or evaluator
error still fails the workflow. When the evidence gate passes, the workflow also
uploads an immutable `projection-observed-baseline-*` artifact containing the
qualified report and input hashes.

Compare the incumbent against simple benchmarks and fit research calibration
candidates on earlier, available outcomes:

```bash
python3 scripts/projection_baseline.py validate --data-dir data --days 365 \
  > data/projection-validation.json
```

This command evaluates a consistent SQLite backup and records its SHA-256. Unset
`MARKETHELM_DATABASE_URL` first: validation and capture use the explicit sidecar
archive and refuse a hosted database override. The report can be generated while
the evidence gate is unmet; `evidenceQualification` retains the shortfall and
`promotionDecision` always remains `research_only`. A zero exit means the report
was generated, not that the model or evidence qualified. Missing training history
is reported as `insufficient_data`; the protocol never shrinks the holdout.

Protocol `frozen-temporal-v1` reserves the newest five scored run dates together.
It fits once using at least 200 samples across five earlier run dates, and only
outcomes persisted strictly before the earliest held-out generation timestamp.
Overlapping forecast horizons with unavailable outcomes are purged. Matched
outcome revisions retain their own recorded availability time. All comparisons
use the same eligible samples, with paired run-date wins/losses and an equally
weighted daily error difference to expose concentration across correlated stocks.

The point-price benchmark holds the origin price unchanged; always-up/down are
separate direction references. Confidence benchmarks include neutral 50% and the
training-only directional success rate. The candidate maps original confidence
cohorts to Laplace-smoothed training success rates, using the global training
rate for cohorts smaller than 30 samples or unseen in training. Brier score and
cohort calibration error accompany the confidence gap. An 80% nominal range
candidate uses a finite-sample quantile of training absolute residuals divided by
origin price, centered on the existing midpoint. Reports show both coverage and
width; increasing width alone does not establish better forecasts.

Scheduled collection now archives this report every eligible run. A qualified
observed capture also includes `validation.json`, with its hash in the manifest,
using the same private inputs as the original outcome report. The committed
[October 30 research study](../baselines/research/projection-2026-10-30/README.md)
preserves an initial real-data comparison without labeling its 18 scored dates
as a qualified baseline.

These are rolling retrospective research comparisons. The archive and its
headline outcomes have already been inspected; repeated evaluation does not
create an untouched prospective test. Forecasts overlap across dates and stocks,
so sample counts are not independent trials. Keep application scoring unchanged
until a frozen candidate is assessed on fresh forward outcomes, against the
benchmarks, with adequate coverage, cohort breadth, and stability across time.

This evidence cadence is not the intended operating cadence for future automated
trading. Broker execution remains unimplemented and will require a separate
intraday or event-driven loop with position state, idempotent orders, risk limits,
a kill switch, and an auditable execution history.

Once the assessment passes, preserve the exact report plus hashes of every input:

```bash
python3 scripts/projection_baseline.py capture --data-dir data --days 365 \
  --output-dir baselines/observed/projection-YYYY-MM-DD
```

Capture refuses to create an output directory when any qualification fails.
It evaluates a consistent SQLite backup of the `market_bars.sqlite` sidecar and hashes that same backup, preventing
a concurrent refresh from producing a report/manifest mismatch.

</details>

<a id="option-3-direct-workflow-programmatic"></a>
<details>
<summary><b>Option 3: Direct workflow (programmatic)</b></summary>

```bash
python -m src.workflows.tracker
```

Runs the core workflow and returns structured JSON. Useful for:

- Testing workflow logic
- CI/CD pipelines
- Debugging without CLI formatting

</details>

<a id="option-4-programmatic-import"></a>
<details>
<summary><b>Option 4: Programmatic import</b></summary>

```python
from src.workflows.tracker import StockTrackerWorkflow

workflow = StockTrackerWorkflow()
result = workflow.run(use_screener=True)

if result["success"]:
    analysis = result["analysis"]
    top_gainers = analysis["top_gainers"]
    ai_summary = result.get("ai_summary")
```

Ideal for custom dashboards, scheduled tasks with custom notifications, or integration with other systems.

---

</details>

</details>

<a id="web-dashboard"></a>
<details>
<summary><b>Web dashboard</b></summary>

After install:

```bash
markethelm-web
```

Open **<http://localhost:8000>**. API docs are at **/docs**.

For React development (Vite on port 3000, hot reload), see
[dashboard/README.md](../dashboard/README.md#development-with-hot-reload).

---

</details>

<a id="local-alerts"></a>
<details>
<summary><b>Local alerts</b></summary>

Create the user alert configuration from the bundled example, inspect its rule
IDs, and validate a rule without delivering a notification:

```bash
markethelm alerts init
markethelm alerts list
markethelm alerts test --id <alert-id> --dry-run
```

After configuring a real notification channel, evaluate the rules once with
`markethelm alerts run` or keep the local worker running:

```bash
markethelm alerts run --loop
```

Installed packages use `~/.markethelm/alerts.json`. Put notification secrets,
such as SMTP passwords, provider keys, and webhook URLs, in
`~/.markethelm/.env`, not in Git. Use `markethelm alerts --config PATH ...` to
select a different configuration file. Alert provider variables and delivery
testing are documented in
[Transactional alert email](DEPLOYMENT.md#transactional-alert-email).

The dashboard's Helmtower page can also manage alert rules. Hosted multi-user
alerts require database mode and the separate worker described in
[Deployment and persistence](DEPLOYMENT.md#hosted-staging-api--worker--postgresql).

---

</details>

<a id="output-files"></a>
<details>
<summary><b>Output files</b></summary>

Each run writes:

| File                                        | Contents                                    |
| ------------------------------------------- | ------------------------------------------- |
| `data/market_bars.sqlite` (or app DB)       | Quotes, projections, and analysis summaries |
| `data/projections_YYYY-MM-DD.md` (optional) | Human-readable projection report            |
| `logs/markethelm_YYYY-MM-DD.log`            | Detailed execution logs                     |

Set `DATA_DIR` to change the output location; see [DEPLOYMENT.md](DEPLOYMENT.md).

---

</details>

<a id="migrate-legacy-market-data-files"></a>
<details>
<summary><b>Migrate legacy market-data files</b></summary>

Current releases store quotes, projections, and daily summaries in the configured
application database or in `DATA_DIR/market_bars.sqlite`. If an existing install
still has dated `daily_data_*.csv`, `projections_*.csv`, or `summary_*.json`
snapshots, import them with:

```bash
python3 scripts/backfill_market_data.py --data-dir data
```

The command prints a JSON report, retains every source file, and skips dates that
already exist in durable storage. It is therefore safe to rerun. A nonzero exit
status means at least one file could not be imported; the report names each
failure. Review the reported `target` before treating the migration as complete:
when `MARKETHELM_DATABASE_URL` is set it is `configured_database`, otherwise it
is the absolute sidecar path. When no usable projections CSV exists for a date,
the importer recovers projections embedded in that date's legacy summary and
reports the recovery under `embedded_summary_fallbacks`.

Use `--replace-existing` only when deliberately upserting legacy records for a
date that already exists. Without it, even a partially populated durable date is
preserved; with it, matching records are updated while durable records absent
from the legacy file remain.

---

</details>

<a id="console-output"></a>
<details>
<summary><b>Console output</b></summary>

Example:

```text
Top 5 Gainers:
  1. MU (Micron Technology): +10.51% @ $315.42
  2. WDC (Western Digital): +8.96% @ $187.70
  ...

Top 5 Losers:
  1. PLTR (Palantir): -5.56% @ $167.86
  ...

Index Performance:
  S&P 500: Avg Change +1.82% (23 gainers | 7 losers)
  NASDAQ-100: Avg Change +0.01% (12 gainers | 18 losers)
```

---

</details>

## Related

- [CONFIGURATION.md](CONFIGURATION.md): indices and screening filters
- [DEPLOYMENT.md](DEPLOYMENT.md): scheduling daily runs (cron, Docker, Kubernetes)
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md): errors and FAQ
