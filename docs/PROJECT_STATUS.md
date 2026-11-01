# Project status and roadmap

**Last updated:** 2026-11-01 (mission, vision, and product goals)

This is the authoritative inventory of what MarketHelm currently ships, what is
covered by automated tests, and what remains unfinished. Deployment instructions
live in [DEPLOYMENT.md](DEPLOYMENT.md); design documents describe longer-term ideas
and should not be read as implementation claims.

<a id="product-direction"></a>
<details open>
<summary><b>Product direction</b></summary>

MarketHelm is building a market intelligence product that takes users from market
observation to informed decisions and, eventually, controlled execution.

**Mission:** Turn market data into explainable insight that helps people evaluate
opportunities and make informed decisions.

**Vision:** A hosted platform where users sign up, choose what to follow, receive
explained recommendations and private alerts, and eventually execute trades
within defined risk limits and with an audit trail. Users manage preferences in
the UI; the platform operates data providers and notification delivery.

**Product goals:**

1. **Monitor:** Build reliable market context from saved data and, in a later
   phase, live quotes.
2. **Interpret:** Explain signals and uncertainty. Develop richer inference with
   ML, fundamentals, and news context as future extensions to today's technical
   heuristics and optional AI briefs.
3. **Suggest and validate:** Present reasoned outlooks, recommendations, and
   alerts; measure projections against real outcomes before changing scoring.
4. **Act:** Eventually support broker execution with risk limits and audit
   history, after separate execution and operational design.
5. **Serve many users:** Make accounts, private preferences, and reliable delivery
   available through a hosted product, with local operation supported for
   development and self-hosting.

The current implementation provides screening, heuristic five-session
projections, optional AI briefs, historical outcome tracking, accounts, and
per-user alerts through a Python core and React dashboard. The capability matrix
below and the ordered work sections distinguish shipped behavior from the goals.

The repository supports two operating modes:

- **Local/self-hosted mode:** market data uses `DATA_DIR/market_bars.sqlite`,
  while alert preferences remain file based.
- **Hosted multi-user mode:** shared market data, accounts, sessions, per-user
  alert settings, jobs, and delivery history use SQLite or PostgreSQL.

Automated broker execution is a future direction, not a current capability. Its
runtime is expected to be intraday or event-driven and separate from the
once-per-session projection-evidence collector. MarketHelm does not provide
investment, legal, or tax advice.

</details>

<a id="status-definitions"></a>
<details>
<summary><b>Status definitions</b></summary>

| Label                                        | Meaning                                                                                                        |
| -------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| **Shipped and tested**                       | Implemented with automated coverage in this repository                                                         |
| **Shipped; operational verification needed** | Implemented and tested in isolation, but requires a real provider or hosted environment to validate end to end |
| **Partial**                                  | A useful first version exists, with material scope still open                                                  |
| **Not implemented**                          | Design or roadmap only                                                                                         |

Automated coverage does not mean every production integration has been exercised.
For example, tests mock Finnhub and notification providers; managed PostgreSQL,
real email delivery, DNS, TLS, backups, and restore procedures require staging.

</details>

<a id="current-capability-matrix"></a>
<details open>
<summary><b>Current capability matrix</b></summary>

| Area                          | Status                                       | What exists                                                                                                                                                                                       | Important remaining work                                                                                                 |
| ----------------------------- | -------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| CLI and daily tracker         | **Shipped and tested**                       | Index screening, quote/profile fetch, analysis, projections, durable `market_bars` storage with optional Markdown reports, and service-boundary resilience coverage                               | Live Finnhub smoke testing and full-market run reliability                                                               |
| Web dashboard                 | **Shipped and tested**                       | Overview, movers, stock detail, summaries, historical trends, accuracy, refresh controls, exports, dark mode                                                                                      | Route-level code splitting, saved views/watchlists, keyboard shortcuts, performance/accessibility passes                 |
| Projection model              | **Partial**                                  | Five-session XNYS heuristic targets, confidence, risk, recommendations, and a deterministic JSON backtest CLI                                                                                     | Qualified out-of-sample baselines, evidence-led calibration changes, fundamentals/news/ML                                |
| Historical accuracy           | **Partial**                                  | CLI, API, and dashboard share exact-session metrics; a committed scenario matrix and golden report protect evaluator semantics                                                                    | Preserve qualified real-data baselines; add risk-adjusted and longer-horizon views                                       |
| Alerts                        | **Shipped and tested**                       | Price, RSI, and shallow compound rules, screening match, cooldowns, log/webhook/email delivery, retries, scheduled worker, delivery history, Helmtower UI                                         | Nested compounds and additional indicators; SMS/push; real-provider staging tests                                        |
| Accounts and tenant isolation | **Shipped and tested**                       | Registration, login/logout, bearer sessions, email verification, password reset/change, account deletion, per-user alert data                                                                     | Account export and stronger administrative/support tooling                                                               |
| Hosted persistence            | **Shipped; operational verification needed** | SQLite/PostgreSQL adapter, versioned migrations (incl. `market_bars`), durable dashboard/alert reads, legacy snapshot backfill, queue/orchestrator, and automated container backup/restore drills | Run and verify the legacy backfill per environment; managed PostgreSQL snapshot/PITR, pooling/TLS, and failover sign-off |
| Production controls           | **Shipped; operational verification needed** | Rate limiting, trusted-proxy handling, health/metrics, ingress/tenant acceptance, bounded capacity baseline, retention and incident runbooks                                                      | Connect a real staging ingress/provider/monitor and record external sign-off evidence                                    |
| Automated trading             | **Not implemented**                          | No broker connection or order execution                                                                                                                                                           | Intraday/event-driven orchestration, broker integration, order/risk model, audit trail, compliance and safety controls   |

</details>

<a id="hosted-alerts-and-accounts"></a>
<details>
<summary><b>Hosted alerts and accounts</b></summary>

The hosted foundation is implemented. When `MARKETHELM_DATABASE_URL` is set,
alert routes require authentication and scope configuration, watches, jobs, and
delivery history to the signed-in user. The account lifecycle includes:

- registration, login, current-user lookup, and logout;
- optional email-verification enforcement;
- verification and password-reset email flows;
- password change with session invalidation; and
- account deletion.

The database-backed worker evaluates enabled watches across users and records
per-channel outcomes. SMTP, SendGrid, and Mailgun are supported for platform email;
generic, Slack, and Discord webhook formats are supported. Retry/backoff is
configurable with `ALERT_DELIVERY_*` environment variables.

Local mode remains intentionally supported. Without `MARKETHELM_DATABASE_URL`,
Helmtower and the alert CLI use the operator's `alerts.json` file and environment
credentials. End users of a hosted deployment do not provide SMTP credentials.

See [ARCHITECTURE.md](ARCHITECTURE.md) for component boundaries and
[DEPLOYMENT.md](DEPLOYMENT.md) for hosted configuration.

</details>

<a id="test-posture-and-known-verification-gaps"></a>
<details>
<summary><b>Test posture and known verification gaps</b></summary>

The repository has broad Python and frontend unit/integration coverage, including
auth lifecycle, tenant isolation, storage migrations, worker orchestration,
delivery history, rate limits, security boundaries, API routes, UI flows, and a
local HTTP provider harness that exercises tracker throttling, malformed JSON,
connection loss, timeouts, partial success, total failure, persistence, and
backtest ingestion. CI also defines PostgreSQL 16 integration, browser smoke, and
full container staging-readiness gates.

The following should not be inferred from those tests:

- live Finnhub availability, quota behavior, or full-market run reliability;
- successful delivery through production SendGrid, Mailgun, SMTP, Slack, or Discord;
- managed PostgreSQL backup, restore, failover, pooling, and TLS behavior;
- production ingress/proxy correctness and sustained-load capacity;
- complete cross-browser, mobile-device, accessibility, and performance coverage; or
- financial validity of the projection heuristic.

These are the highest-value testing gaps because they cross system boundaries that
unit tests and container-only integration tests cannot fully reproduce.

</details>

<a id="recommended-next-work"></a>
<details>
<summary><b>Recommended next work</b></summary>

1. **Market data DB cutover:** daily bars, projections, and summaries now write to
   the app DB or `DATA_DIR/market_bars.sqlite`; the dashboard, API, and backtests read only durable storage. Run and verify the legacy snapshot backfill in each environment, compare representative dates, then retire the old snapshot files. Runtime reads no longer fall back to CSV; RSI price history uses provider candles, then `market_bars`.
2. **Projection validation:** the weekday post-close workflow preserves a
   cumulative forward archive and its qualification report. Keep collecting exact
   target closes until `projection_baseline.py assess` passes and the workflow
   emits a hashed observed baseline. New snapshots retain quote-session
   provenance; legacy filename-dated closes are excluded from qualification. Do
   not change confidence scoring until adequately sized cohorts demonstrate
   stable bias; the committed synthetic baseline validates evaluator behavior
   only.
3. **External staging sign-off:** in parallel, complete the ordered
   [external staging execution TODO](DEPLOYMENT.md#external-staging-execution-todo)
   against the chosen managed PostgreSQL, ingress, monitoring, and
   transactional-email providers. This is an operator-owned release gate requiring
   credentials/evidence, not unfinished repository automation.
4. **Alert depth:** extend beyond RSI and shallow compounds (more indicators,
   nested rules); consider SMS/push only after hosted email is proven reliable.
5. **Dashboard quality:** code-split routes, run accessibility/performance audits,
   and decide whether saved watchlists/views belong in the product.

</details>

<a id="live-prices-roadmap"></a>
<details>
<summary><b>Live prices roadmap</b></summary>

**Status: planned, not implemented.** Quotes and dashboard data are batch/refresh
based today. "Fetch New" starts a background tracker run, and quotes shown in the UI come from the last saved run. Live prices are a long-term product goal; this
section records the intended order of work. It is a plan, not an implementation claim.

<a id="principles"></a>
<details>
<summary><b>Principles</b></summary>

- Keep batch projections and saved quotes as the reliable fallback. Live prices
  layer on top and must degrade to the latest saved quote when the feed is down.
- Keep the provider behind the existing boundary in `src/services/api_client.py` so
  the data source can change without touching the dashboard or alert rules.
- Hosted mode (shared database, per-user alerts) is the target. Local file mode
  stays supported for development but is not the customer path.

</details>

<a id="proposed-phases"></a>
<details>
<summary><b>Proposed phases</b></summary>

1. **Finish the market data database cutover** (see Recommended next work). Live quotes need a durable shared store first, and the retired CSV path is already gone, so no second legacy data path remains.
2. **Choose and validate a live-quote provider.** Confirm that the chosen plan
   allows the needed symbol count, update rate, and redistribution to end users.
   Open question: whether the current Finnhub plan is sufficient or another
   provider is required.
3. **Add a quote service in the backend.** A long-running component that holds the
   provider connection, normalizes ticks, and writes the latest quote and intraday
   bars to the shared store. Open question: whether it runs inside the existing
   worker process or as its own service.
4. **Deliver live quotes to the dashboard.** Add a push or short-interval polling
   endpoint (WebSocket or server-sent events), show a "Live" or "Delayed" state with
   the quote time, and fall back to the latest saved quote whenever the feed is unavailable.
5. **Make alerts react to live quotes.** The alert worker evaluates on a schedule
   today. Extend price rules to evaluate against the latest live quote while
   keeping cooldowns and delivery retries, and do not change RSI/compound semantics
   until intraday bars are available.
6. **Operational readiness.** Rate limits and per-tenant fan-out limits,
   reconnect/backoff behavior, market-hours handling for XNYS sessions, metrics and
   health checks for feed lag, and staging verification against the real provider.

</details>

<a id="not-in-scope-for-this-roadmap"></a>
<details>
<summary><b>Not in scope for this roadmap</b></summary>

- Automated trading and broker execution (see Explicitly deferred). It would depend
  on this work but needs its own risk, compliance, and audit design.
- Changing projection or confidence scoring. Projections stay batch-based and
  evidence-led.

</details>

</details>

<a id="explicitly-deferred"></a>
<details>
<summary><b>Explicitly deferred</b></summary>

| Item                               | Reason                                                                                   |
| ---------------------------------- | ---------------------------------------------------------------------------------------- |
| Automated trading                  | Requires separate intraday orchestration plus risk, compliance, broker, and audit design |
| Nested compound / extra indicators | RSI + shallow AND/OR cover the first technical slice; deeper nesting deferred            |
| SMS and push notifications         | Email/webhook production operation should be proven first                                |
| International exchanges            | Current screening is centered on S&P 500 and NASDAQ-100                                  |
| ML/fundamental/news projections    | Current projection engine is intentionally heuristic                                     |

</details>

<a id="keeping-this-document-current"></a>
<details>
<summary><b>Keeping this document current</b></summary>

- Update the date and capability matrix after meaningful behavior changes.
- Distinguish code completion from real-environment operational verification.
- Link roadmap references here instead of maintaining conflicting status lists.
- Keep release-specific history in [CHANGELOG.md](../CHANGELOG.md).

</details>

## Related documentation

- [Deployment and persistence](DEPLOYMENT.md)
- [Dashboard guide](../dashboard/README.md)
- [Architecture](ARCHITECTURE.md)
- [Contributing](../CONTRIBUTING.md)
