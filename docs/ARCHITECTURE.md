# Architecture

MarketHelm has a reusable Python core, two presentation layers (CLI and web), a
React dashboard, and an optional hosted persistence/worker subsystem.

<a id="repository-layout"></a>
<details open>
<summary><b>Repository layout</b></summary>

```text
markethelm/
|-- main.py                         # Source-checkout CLI entry point
|-- src/
|   |-- core/                       # Configuration and logging
|   |-- services/                   # Finnhub client, index/screening/data fetch
|   |-- analysis/                   # Market analysis, projections, summaries
|   |-- storage/                    # Files, SQLite/PostgreSQL, sessions, migrations
|   |-- workflows/                  # Reusable tracker orchestration
|   |-- alerts/                     # Rules, workers, channels, delivery status
|   `-- cli/                        # `markethelm` command presentation
|-- dashboard/
|   |-- backend/                    # FastAPI routes, auth, rate limits, health
|   `-- frontend/                   # React/TypeScript SPA
|       `-- src/pages/              # Dashboard, brief, history, watches, accounts, recovery
|-- config/                         # Exchange, filter, and alert examples
|-- data/                           # Runtime data: SQLite sidecar, optional Markdown reports
|-- tests/                          # Python unit/integration/security tests
|-- e2e/                            # Playwright browser tests, fixtures and configuration
`-- scripts/                        # Build, release, worker, and validation helpers
```

The [dashboard layout and page map](../dashboard/README.md#project-layout) lists
the current frontend views and routes.

Business logic belongs in `src/` so the CLI, FastAPI routes, and workers can reuse
it. The frontend communicates with FastAPI through `/api/*` routes. A production
frontend build is emitted into `dashboard/backend/static/` and served by FastAPI.

</details>

<a id="operating-modes"></a>
<details>
<summary><b>Operating modes</b></summary>

<a id="localself-hosted-file-mode"></a>
<details>
<summary><b>Local/self-hosted file mode</b></summary>

This is the default when `MARKETHELM_DATABASE_URL` is unset.

- Market runs write quotes, projections, and daily summaries to durable storage (`market_bars`, `projections`, `daily_summaries`) in `DATA_DIR/market_bars.sqlite`, or in the app database when `MARKETHELM_DATABASE_URL` is set. Optional Markdown projection reports go under `DATA_DIR`. Dashboard and alert readers use the same durable store.
- Alert preferences and history use the local MarketHelm configuration directory.
- Alert API routes are intended for an operator-controlled deployment and do not
  require user accounts.
- `markethelm alerts run --loop` evaluates rules on a schedule.

</details>

<a id="hosted-multi-user-mode"></a>
<details>
<summary><b>Hosted multi-user mode</b></summary>

Setting `MARKETHELM_DATABASE_URL` enables SQLite or PostgreSQL persistence for
accounts and tenant-owned alert state.

- Bearer sessions protect tenant-specific API routes.
- Alert configuration, watches, jobs, and delivery history are scoped per user.
- Market data remains a shared platform input, not tenant-owned.
- The orchestrator creates jobs and workers claim/process them from the database.
- Versioned migrations run at startup and fail closed on an unknown newer schema.
- Rate limiting defaults on and health/readiness/worker/metrics endpoints support
  operations.

The account router provides registration, login/logout, current-user lookup,
verification request/confirmation, password-reset request/confirmation, password
change, and account deletion. Password changes invalidate other sessions. Optional
verification enforcement is controlled with
`MARKETHELM_REQUIRE_EMAIL_VERIFICATION`.

`init_database()` applies pending migrations in version order. Older untracked
SQLite installations are adopted through the idempotent initial migration, while
an unknown newer schema version causes startup to fail closed. The PostgreSQL 16
integration gate exercises migrations, tenant storage, and the worker lifecycle:

```bash
docker compose -f docker-compose.postgres-test.yml up \
  --abort-on-container-exit --exit-code-from tests
docker compose -f docker-compose.postgres-test.yml down --volumes
```

See [DEPLOYMENT.md](DEPLOYMENT.md) for configuration and hosted verification.

</details>

</details>

<a id="daily-market-workflow"></a>
<details>
<summary><b>Daily market workflow</b></summary>

```text
index constituents
        |
        v
lightweight quote screening -- Finnhub rate limiter/retry
        |
        v
detailed quote/profile fetch for selected symbols
        |
        v
market analysis + heuristic five-session XNYS projections
        |
        +--> durable market_bars/projections store (+ optional Markdown)
        +--> exact-session backtest CLI + dashboard history/accuracy APIs
        `--> alert evaluation snapshot
```

The workflow is batch-oriented. "Fetch New" starts the same underlying tracker
work in a background process; it is not a streaming quote service.

</details>

<a id="alert-workflow"></a>
<details>
<summary><b>Alert workflow</b></summary>

```text
market snapshot / selected quote
             |
             v
       rule evaluation ---- cooldown state
             |
             v
   log / email / webhook delivery
             |
             v
       delivery outcome/history
```

In hosted mode, database jobs add claim/lease semantics around evaluation so
multiple workers can process user rules without sharing in-memory tenant state.

Current channels are log, SMTP/SendGrid/Mailgun email, and generic/Slack/Discord webhooks. Supported
conditions are price thresholds, RSI thresholds, shallow AND/OR compounds of those
leaves (plus screening match), with history loaded from provider candles, then durable `market_bars`. Patterns,
nested compounds, SMS, push, and cloud queue-provider adapters
are not implemented.

| Module                                      | Responsibility                                  |
| ------------------------------------------- | ----------------------------------------------- |
| `alert_engine.py`, `alert_rules.py`         | Parse and evaluate current rules                |
| `alert_storage.py`, `user_alert_storage.py` | Local and tenant-scoped persistence             |
| `alert_runner.py`, `alert_worker.py`        | One-shot and looping evaluation                 |
| `alert_orchestrator.py`, `job_processor.py` | Schedule, claim, and process hosted jobs        |
| `delivery_status.py`                        | Record per-channel outcomes                     |
| `notifiers/`                                | Email/webhook delivery and retry classification |

</details>

<a id="data-ownership"></a>
<details>
<summary><b>Data ownership</b></summary>

| Data                                  | Local mode                    | Hosted mode                         |
| ------------------------------------- | ----------------------------- | ----------------------------------- |
| Market quotes, projections, summaries | `DATA_DIR/market_bars.sqlite` | Shared application database         |
| Alert config and history              | Local JSON/files              | Per-user database records           |
| Accounts and sessions                 | Not used                      | Database                            |
| Worker jobs and outcomes              | Local run state/history       | Database                            |
| Provider credentials                  | Environment or local `.env`   | Platform secret manager/environment |

Quotes, projections, and daily summaries are stored durably in `market_bars`, `projections`, and `daily_summaries` (application database when `MARKETHELM_DATABASE_URL` is set, otherwise the `DATA_DIR` sidecar); only optional Markdown reports remain files.

</details>

<a id="external-api-limiting-and-retries"></a>
<details>
<summary><b>External API limiting and retries</b></summary>

`src/services/api_client.py` owns Finnhub request limiting, connection reuse, retry,
and `429 Retry-After` behavior. Screening uses a quote-only request; only selected
symbols receive the more expensive detailed fetch. Configuration should stay
within the active Finnhub plan rather than relying on a hard-coded provider quota.

This limiter is separate from the dashboard's inbound API rate limiter in
`dashboard/backend/rate_limit.py`. Hosted API limits are configurable by route
class and use trusted-proxy configuration to determine the client address.

</details>

<a id="webapi-boundaries"></a>
<details>
<summary><b>Web/API boundaries</b></summary>

FastAPI groups routes by concern:

- market overview/movers, projections, stocks, summaries, and history;
- background refresh start/status/cancel;
- alert configuration, quotes, execution, tests, and delivery status;
- registration, sessions, verification, recovery, and account controls; and
- liveness, readiness, worker health, and metrics.

File mode preserves the original operator workflow. Hosted mode changes ownership
and authorization of tenant data; it does not change the shared market-data model.

</details>

<a id="important-limitations"></a>
<details>
<summary><b>Important limitations</b></summary>

- Projections are heuristic and are not a validated trading model.
- Quotes and dashboard data are batch/refresh based, not WebSocket streams.
- Managed PostgreSQL, provider delivery, ingress, backups, and restore need staging
  verification even though adapters and tests exist.
- There is no broker API, order model, or automated execution path.

</details>

## Related documentation

- [Project status and test gaps](PROJECT_STATUS.md)
- [Configuration](CONFIGURATION.md)
- [Deployment](DEPLOYMENT.md)
- [Stock projections](STOCK_PROJECTIONS.md)
- [Dashboard guide](../dashboard/README.md)
