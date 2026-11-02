# Deployment & persistence

<a id="hosted-staging-api--worker--postgresql"></a>
<details open>
<summary><b>Hosted staging (API + worker + PostgreSQL)</b></summary>

The staging stack in `docker-compose.staging.yml` builds the React application
into the API image and runs the API, scheduled alert worker, and PostgreSQL as
separate services. Named volumes persist PostgreSQL and the shared `DATA_DIR` used
by both application processes.

1. Copy `.env.staging.example` to `.env.staging` and replace every placeholder.
2. Set `POSTGRES_PASSWORD` in the host environment (never commit it).
3. Validate with `docker compose -f docker-compose.staging.yml config`.
4. Start with `docker compose -f docker-compose.staging.yml up -d --build`.
5. Put a TLS reverse proxy in front of `127.0.0.1:8000` and add its IP/CIDR to
   `MARKETHELM_TRUSTED_PROXY_CIDRS`.
6. Verify `/health/live`, `/health/ready`, `/health/worker`, and `/metrics`, then test registration
   and the email-verification link against the public staging hostname.

`/health/live` proves the API process is alive. `/health/ready` returns 503 when
the database is unavailable or migrations are incomplete and includes the latest
worker heartbeat when one exists.

<a id="scheduled-market-data"></a>
<details>
<summary><b>Scheduled hosted market-data collection</b></summary>

The hosted collector updates the application's shared database independently of
browser visits. It uses the existing screening/analysis pipeline and XNYS calendar,
runs once per completed session after a provider grace period, and catches up on
the most recent completed session after a restart. It is a daily collector, not a
live quote feed. The GitHub projection-evidence workflow remains a separate archive.

Set `MARKETHELM_MARKET_REFRESH_ENABLED=true` in the environment shared by the API
and collector, configure `FINNHUB_API_KEY`, and ensure `MARKETHELM_DATABASE_URL`
points to the same database. Start the optional Compose service:

```bash
docker compose -f docker-compose.staging.yml --profile market-data up -d --build
```

On another host, run `markethelm collect-market-data --loop` as a supervised
service, or invoke `markethelm collect-market-data` from a periodic cron job.
Concurrent scheduled collectors share a database lease. Their retry budget and
last successful session survive restarts. Ordinary `markethelm` and dashboard
manual refreshes remain separate operator actions; schedule those separately to
avoid additional provider calls. Hosted notification delivery stays in the alert
worker; the collector does not evaluate local alert rules.

| Setting | Default | Meaning |
| --- | --- | --- |
| `MARKET_REFRESH_CLOSE_DELAY_SECONDS` | `900` | Delay after the actual exchange close, including early closes |
| `MARKET_REFRESH_TIMEOUT_SECONDS` | `1800` | Maximum runtime per collection attempt; parent and child enforce it |
| `MARKET_REFRESH_RETRY_SECONDS` | `900` | Wait between failed attempts |
| `MARKET_REFRESH_MAX_ATTEMPTS` | `3` | Attempts per target session; a new session gets a new budget |
| `MARKET_REFRESH_MIN_SYMBOLS` | `10` | Minimum fresh symbols with persisted bars, projections, and summary |
| `MARKET_REFRESH_TOP_N` | `50` | Maximum selected stocks passed to the existing tracker |
| `MARKET_REFRESH_POLL_SECONDS` | `60` | Poll interval in loop mode |

Adjust coverage, runtime, and the selected universe for the provider plan. The
existing API client still controls provider rate limits. A quote counts as fresh
only if its aware provider timestamp belongs to the target session and is within
15 minutes of that session's close or later; future timestamps beyond five minutes
are rejected. A successful exit without newly persisted outputs fails collection.

Probe `/health/market-data` externally, or run
`markethelm collect-market-data --check`. Missing/stale data returns HTTP 503 or a
nonzero CLI status. Both remain unhealthy until the first verified collection.
The freshness deadline allows the post-close delay, one polling interval, one
bounded collection runtime, and a 120-second recovery margin; the response also reports the latest
due session, data session, retry state, and sanitized failure code. This probe is
separate from API liveness/readiness, so collection failures do not restart the API.
It reports `disabled` when the feature is off. Monitor repeated failed attempts
as well as missed freshness deadlines.

After correcting a provider/configuration failure, an operator can run
`markethelm collect-market-data --retry-failed` to reset that session's retry
budget. It does not override an active lease or re-run a successful session.
Partial/failing runs can leave partial outputs in the shared database; a failure
never advances the scheduler's last-success marker. The collector does not publish
an atomic full-universe snapshot or backfill every missed session.

Keep this service opt-in in CI and local environments: starting it with a real
provider key performs provider requests. The default container readiness workflow
does not enable the `market-data` profile.

</details>

<a id="staging-acceptance-harness"></a>
<details>
<summary><b>Staging acceptance harness</b></summary>

After the stack is reachable, run the credential-free operational checks:

```bash
python scripts/staging_acceptance.py \
  --base-url https://staging.example.com \
  --report staging-acceptance.json
```

The harness requires HTTPS except for loopback development URLs. It verifies API
liveness, database readiness and schema version, a fresh worker heartbeat,
Prometheus metrics, and the hosted authentication boundary. The JSON report
contains check results and timings but no credentials; keep generated reports as
deployment evidence rather than committing them.

To prove write isolation, prepare two **dedicated, verified staging accounts**.
Their alert configurations must be empty. Supply credentials through the process
environment so passwords do not appear in shell history:

```bash
export MARKETHELM_STAGING_TENANT_A_EMAIL=acceptance-a@example.com
export MARKETHELM_STAGING_TENANT_A_PASSWORD='...'
export MARKETHELM_STAGING_TENANT_B_EMAIL=acceptance-b@example.com
export MARKETHELM_STAGING_TENANT_B_PASSWORD='...'
python scripts/staging_acceptance.py \
  --base-url https://staging.example.com \
  --tenant-check \
  --report staging-acceptance.json
```

The tenant check refuses non-empty accounts, creates two distinct log-only
watches, confirms config/index/dry-run isolation, and restores both configurations
to their original empty state. It never sends email or webhooks. A failed cleanup
is reported as a failed acceptance check and must be handled before reusing the
accounts.

`--skip-worker` is available for diagnosing a stack before its worker starts, but
a report produced with that option is marked `incomplete`, exits nonzero, and is
not sufficient for staging sign-off.

Pass `--ingress-origin https://staging.example.com` to additionally require a
correlation ID, exact-origin credentialed CORS, rejection of an untrusted origin,
and HSTS on HTTPS. Disposable tenant bootstrapping is available only on loopback:

```bash
python scripts/staging_acceptance.py \
  --base-url http://127.0.0.1:8000 \
  --ingress-origin http://127.0.0.1:8000 \
  --tenant-check --bootstrap-loopback-tenants
```

</details>

<a id="operational-readiness-drills"></a>
<details>
<summary><b>Operational readiness drills</b></summary>

The `Hosted staging readiness` workflow is the repeatable repository gate. It
builds the real images, starts API + worker + PostgreSQL, runs operational/ingress/
tenant checks, applies a bounded load baseline, restores PostgreSQL and shared
market data, restarts the database and worker, and publishes credential-free JSON
evidence. Run it locally before changing deployment behavior and trigger it
manually after infrastructure changes.

For an environment-specific capacity baseline:

```bash
python scripts/staging_load.py \
  --base-url https://staging.example.com \
  --requests 100 --concurrency 10 \
  --max-error-rate 0 --max-p95-ms 1000 \
  --report staging-load.json
```

This deliberately defaults to the read-only readiness endpoint, exercising a
database connection without mutating tenant data. Raise traffic gradually and set
thresholds from the service's actual SLO; do not turn the tool into an unapproved
stress test against a shared host.

</details>

<a id="backup-and-restore-runbook"></a>
<details>
<summary><b>Backup and restore runbook</b></summary>

Back up **both** persistence layers as one recovery set:

1. Take a managed PostgreSQL snapshot or custom-format `pg_dump`.
2. Archive the persistent `DATA_DIR` at the same logical time.
3. Record application version, schema version from `/health/ready`, timestamps,
   checksums, encryption/key reference, and retention expiry with the artifacts.
4. Restore PostgreSQL into a newly named database—never over the active database.
5. Restore `DATA_DIR` into a new empty path, start one API and one worker against
   the restored stores, and run `staging_acceptance.py`.
6. Compare user/config/migration counts and a sample of dated market files before
   declaring the recovery point usable. Remove the drill resources afterward.

The compose readiness workflow demonstrates these mechanics with a disposable
database and volume. A managed staging sign-off must additionally use the cloud
provider's snapshot/PITR process, TLS settings, and credentials because a local
container cannot prove those controls.

Default retention unless a stricter policy applies:

- encrypted daily PostgreSQL + `DATA_DIR` recovery sets for 30 days;
- encrypted weekly recovery sets for 12 weeks;
- application logs for 14 days and metrics for 30 days;
- readiness reports for at least 14 days and through the next deployment; and
- delivery history is already bounded to the newest 100 records per user.

Test one restore monthly and after database/schema changes. Alert on a missed
backup, failed checksum, expired encryption key, or failed restore drill.

</details>

<a id="monitoring-and-incident-runbook"></a>
<details>
<summary><b>Monitoring and incident runbook</b></summary>

Scrape `/metrics` and probe `/health/live`, `/health/ready`, and `/health/worker`
from outside the host. Preserve `X-Request-ID` in proxy and application logs.
Recommended pages are: liveness unavailable for 2 minutes, readiness unavailable
for 5 minutes, worker unhealthy for two evaluation intervals, HTTP 5xx above 2%
for 5 minutes, or backup/restore evidence overdue.

Respond in this order:

1. **Liveness failure:** stop rollout, inspect proxy/container logs, and roll back
   the application image if the previous release is healthy.
2. **Readiness/database failure:** stop writes and workers, check provider health,
   connection/TLS limits, and schema version; fail over only through the managed
   database procedure, then rerun acceptance.
3. **Worker failure:** keep the API online, stop duplicate workers, restart one
   worker, and verify a fresh heartbeat plus queued-job progress before scaling.
4. **Provider failure:** pause alert delivery, retain queued jobs, check provider
   status/credentials/sender-domain records, then resume and inspect retry results.
5. **Suspected data loss:** freeze writes, preserve logs, restore the newest
   verified recovery set into new resources, validate it, then perform a deliberate
   cutover with a documented rollback target.

</details>

<a id="external-staging-sign-off"></a>
<details>
<summary><b>External staging sign-off</b></summary>

Repository automation cannot prove controls owned by a hosting or email provider.
Complete this operator-owned TODO in order after the staging-readiness PR merges.
Never put provider credentials in an issue, PR, report, or committed environment
file; use the selected platform's secret manager.

<a id="external-staging-execution-todo"></a>
<details>
<summary><b>External staging execution TODO</b></summary>

<a id="1-record-decisions-and-ownership"></a>
<details>
<summary><b>1. Record decisions and ownership</b></summary>

- [ ] Choose the staging hostname and confirm who controls its DNS.
- [ ] Choose the application host, managed PostgreSQL provider, region, and budget.
- [ ] Choose SendGrid, Mailgun, or SES and the sender domain/address.
- [ ] Choose the monitoring service and its email/Slack/PagerDuty destination.
- [ ] Record the availability target, database RPO/RTO, backup retention, and
      person responsible for acknowledging staging incidents.

</details>

<a id="2-add-and-provision-the-target-deployment"></a>
<details>
<summary><b>2. Add and provision the target deployment</b></summary>

- [ ] Add provider-specific deployment configuration or infrastructure-as-code;
      the checked-in compose stack is the portable reference, not proof of a managed
      deployment.
- [ ] Provision secret storage and inject credentials without committing them.
- [ ] Provision persistent shared `DATA_DIR` storage for the API and worker.
- [ ] Deploy the same reviewed application image as separate API and worker
      services, then record the image digest and application version.

</details>

<a id="3-sign-off-managed-postgresql"></a>
<details>
<summary><b>3. Sign off managed PostgreSQL</b></summary>

- [ ] Provision PostgreSQL 16 with encryption at rest, TLS required, restricted
      networking, least-privilege application credentials, and pooling/connection
      limits appropriate for the host.
- [ ] Enable automated snapshots and point-in-time recovery with the recorded
      retention policy.
- [ ] Run migrations and the staging acceptance/tenant checks against the managed
      database.
- [ ] Restore a snapshot/PITR point into a newly named database, compare schema and
      critical record counts, and record the measured RPO/RTO.
- [ ] Perform a controlled provider failover, rerun acceptance, and save provider
      event IDs and recovery timings.

</details>

<a id="4-sign-off-public-dns-and-tls"></a>
<details>
<summary><b>4. Sign off public DNS and TLS</b></summary>

- [ ] Create the staging DNS record pointing only to the intended ingress.
- [ ] Install or enable a trusted, hostname-matching certificate with automatic
      renewal; enforce HTTP-to-HTTPS redirects and HSTS.
- [ ] Set `MARKETHELM_PUBLIC_URL`, `CORS_ORIGINS`, and the exact trusted-proxy
      CIDRs for the deployed ingress.
- [ ] Run `staging_acceptance.py --ingress-origin ...` against the public HTTPS URL
      and save its JSON report plus certificate/DNS evidence.

</details>

<a id="5-sign-off-transactional-email"></a>
<details>
<summary><b>5. Sign off transactional email</b></summary>

- [ ] Authenticate the sender domain with provider-supplied SPF/DKIM records and
      publish a DMARC policy.
- [ ] Store a restricted provider key and configure `ALERT_EMAIL_PROVIDER` plus
      `ALERT_EMAIL_FROM`.
- [ ] Receive and complete registration verification and password-reset links;
      confirm both use `MARKETHELM_PUBLIC_URL`.
- [ ] Deliver a real alert while the dashboard is closed and record the provider
      message ID and inbox authentication results.
- [ ] Trigger a controlled rejection and confirm retries/failure details appear in
      MarketHelm delivery history without leaking credentials.

</details>

<a id="6-sign-off-external-monitoring-and-release-evidence"></a>
<details>
<summary><b>6. Sign off external monitoring and release evidence</b></summary>

- [ ] Probe `/health/live`, `/health/ready`, and `/health/worker` from outside the
      host and collect `/metrics` through an appropriately restricted path.
- [ ] Configure the runbook thresholds and notification destination.
- [ ] Stop the staging worker and API in controlled drills; confirm alerts arrive,
      are acknowledged, and send recovery notifications after restoration.
- [ ] Attach DNS/TLS results, database restore/failover evidence, email message IDs,
      monitoring incident IDs, image digest, and acceptance/load JSON reports to the
      staging release record.
- [ ] Review least privilege, secret rotation, backup retention, and rollback;
      obtain the named operator's final staging sign-off.

Until those artifacts exist for a specific environment, the code is staging-ready
but that environment is not approved for production.

</details>

</details>

</details>

<a id="hosted-mode-configuration"></a>
<details>
<summary><b>Hosted-mode configuration</b></summary>

Hosted mode requires a database URL and a stable signing secret. SQLite is useful
for development; PostgreSQL is recommended for a deployed service:

```bash
export MARKETHELM_DATABASE_URL=sqlite:////path/to/markethelm.db
export MARKETHELM_AUTH_SECRET=change-me-in-production-min-16-chars

# Hosted alternative:
export MARKETHELM_DATABASE_URL=postgresql://user:password@host:5432/markethelm
```

When `MARKETHELM_DATABASE_URL` is unset, the application remains in local file
mode. Quotes, projections, and daily summaries live in the database when it is configured (otherwise in `DATA_DIR/market_bars.sqlite`); the database also stores accounts,
sessions, per-user alert settings, jobs, and delivery outcomes.

The account API under `/api/auth` includes registration, login/logout, current-user
lookup, email-verification request/confirmation, password-reset
request/confirmation, password change, and account deletion. Account email links
require `MARKETHELM_PUBLIC_URL` plus a configured platform email provider.

Schema migrations run automatically at startup and fail closed if the database has
an unknown newer version. Before production, exercise the PostgreSQL integration
gate documented in [ARCHITECTURE.md](ARCHITECTURE.md) and verify managed backups,
restore, pooling, TLS, and worker concurrency in staging.

This project runs **locally** and can run **on a host** (VPS, PaaS, containers) the same way: application code is deployed; **market data and projections live in `DATA_DIR/market_bars.sqlite` or the application database** configured via environment variables—not committed to git.

---

</details>

</details>

<a id="what-gets-deployed-vs-what-stays-private"></a>
<details>
<summary><b>What gets deployed vs what stays private</b></summary>

|                                                 | In git                    | On the server (never in git)                  |
| ----------------------------------------------- | ------------------------- | --------------------------------------------- |
| Application code                                | Yes                       | Built from git                                |
| `data/*.sqlite`, `data/*.md`                    | **No** (see `.gitignore`) | Written at runtime by the tracker / dashboard |
| API keys (`FINNHUB_API_KEY`, broker keys, etc.) | **No**                    | Injected env vars or host secret store        |

---

</details>

<a id="persistence-data_dir"></a>
<details>
<summary><b>Persistence: <code>DATA_DIR</code></b></summary>

The tracker and dashboard **read and write** durable market data (`market_bars.sqlite`) and optional Markdown reports under a directory that defaults to **`data/`** at the project root.

- **Local dev:** usually nothing to set; `data/` is created when you run the tracker.
- **Deployed:** set **`DATA_DIR`** to an **absolute path** on **persistent storage** (attached volume, mounted disk).

If you omit persistence, containers that restart **lose** history unless you restore backups or re-fetch.

**Backend (dashboard):** `dashboard/backend` resolves data via `DATA_DIR` (see `dashboard/backend/services/data_loader.py`). If unset, it uses the repo’s `data/` folder relative to the project root.

**Example (Linux):**

```bash
export DATA_DIR=/var/lib/markethelm/data
```

Point your process manager (systemd, Docker, etc.) at that environment.

<a id="legacy-market-data-backfill"></a>
<details>
<summary><b>Legacy market-data backfill</b></summary>

Installations upgraded from the CSV/JSON serving path can copy their dated
snapshots into durable storage with:

```bash
python3 scripts/backfill_market_data.py --data-dir "$DATA_DIR"
```

Back up both the legacy files and the target database first. The command does not
modify its source files and skips dates already present in the database, so it is
safe to rerun after a partial operational attempt. Check the JSON report and its
`target` field: a configured `MARKETHELM_DATABASE_URL` takes precedence over the
sidecar path. Resolve every reported file error before removing old artifacts; the application no longer reads those CSV files at runtime. See [USAGE.md](USAGE.md#migrate-legacy-market-data-files)
for `--replace-existing` upsert semantics.

---

</details>

</details>

<a id="environment-variables-reference"></a>
<details>
<summary><b>Environment variables (reference)</b></summary>

| Variable                                | Used by                         | Purpose                                                                                     |
| --------------------------------------- | ------------------------------- | ------------------------------------------------------------------------------------------- |
| `DATA_DIR`                              | Tracker, dashboard backend      | Path to durable `market_bars.sqlite` / DB (`market_bars`, `projections`, `daily_summaries`) |
| `FINNHUB_API_KEY`                       | Tracker CLI                     | Market data (required for live fetches)                                                     |
| `CORS_ORIGINS`                          | Dashboard backend               | Comma-separated origins allowed in browser (e.g. `https://app.example.com`)                 |
| `VITE_API_URL`                          | Dashboard frontend (build time) | Public URL of the API (e.g. `https://api.example.com`)                                      |
| `MARKETHELM_DATABASE_URL`               | API, worker                     | Enables hosted mode; SQLite for development or PostgreSQL for hosted use                    |
| `MARKETHELM_AUTH_SECRET`                | Dashboard backend               | Required hosted session-signing secret; minimum 16 characters                               |
| `MARKETHELM_PUBLIC_URL`                 | Dashboard backend               | Safe public base URL for email verification and password-reset links                        |
| `MARKETHELM_REQUIRE_EMAIL_VERIFICATION` | Dashboard backend               | Require verified email before protected hosted operations                                   |
| `MARKETHELM_RATE_LIMIT_ENABLED`         | Dashboard backend               | Enables API rate limiting; defaults on when database mode is enabled                        |
| `MARKETHELM_RATE_LIMIT_GLOBAL`          | Dashboard backend               | Per-client API requests/minute (default `120`)                                              |
| `MARKETHELM_RATE_LIMIT_LOGIN`           | Dashboard backend               | Login attempts/client/minute (default `10`)                                                 |
| `MARKETHELM_RATE_LIMIT_REGISTER`        | Dashboard backend               | Registrations/client/hour (default `5`)                                                     |
| `MARKETHELM_RATE_LIMIT_AUTH_EMAIL`      | Dashboard backend               | Verification/reset email requests/client/hour (default `5`)                                 |
| `MARKETHELM_RATE_LIMIT_EXPENSIVE`       | Dashboard backend               | Expensive write requests/client/minute (default `10`)                                       |
| `MARKETHELM_TRUSTED_PROXY_CIDRS`        | Dashboard backend               | Comma-separated proxy CIDRs allowed to supply `X-Forwarded-For`                             |
| `ALERT_WEBHOOK_URL`                     | Tracker (alerts)                | Default webhook when rules use `webhook` without per-rule `url`                             |
| `ALERT_WEBHOOK_FORMAT`                  | Tracker (alerts)                | `json`, `slack`, or `discord` webhook body format                                           |
| `DISCORD_WEBHOOK_URL`                   | Tracker (alerts)                | Default Discord incoming webhook URL when a rule has no `webhook_url`                       |
| `MARKETHELM_ALERTS_CONFIG`              | Tracker (alerts)                | Optional path to `alerts.json` (default `~/.markethelm/alerts.json`)                        |
| `SMTP_HOST`                             | Tracker (alerts)                | SMTP server for `email` notifications                                                       |
| `SMTP_PORT`                             | Tracker (alerts)                | SMTP port (default `587`)                                                                   |
| `SMTP_USER`                             | Tracker (alerts)                | SMTP username                                                                               |
| `SMTP_PASSWORD`                         | Tracker (alerts)                | SMTP password or app password                                                               |
| `ALERT_EMAIL_TO`                        | Tracker (alerts)                | Default recipients for `email` notifications                                                |
| `ALERT_EMAIL_FROM`                      | Tracker (alerts)                | Platform **From** address (`alerts@yourdomain.com`); required for SendGrid/Mailgun          |
| `ALERT_EMAIL_PROVIDER`                  | Tracker (alerts)                | `smtp` (default), `sendgrid`, or `mailgun`; auto-detected when API keys are set             |
| `SENDGRID_API_KEY`                      | Tracker (alerts)                | SendGrid API key when `ALERT_EMAIL_PROVIDER=sendgrid`                                       |
| `MAILGUN_API_KEY`                       | Tracker (alerts)                | Mailgun API key when `ALERT_EMAIL_PROVIDER=mailgun`                                         |
| `ALERT_DELIVERY_MAX_ATTEMPTS`           | Tracker (alerts)                | Total send attempts per notification (default `3`)                                          |
| `ALERT_DELIVERY_RETRY_BASE_SECONDS`     | Tracker (alerts)                | Initial backoff delay between retries (default `1`)                                         |
| `ALERT_DELIVERY_RETRY_MAX_SECONDS`      | Tracker (alerts)                | Max backoff delay cap (default `8`)                                                         |
| `MAILGUN_DOMAIN`                        | Tracker (alerts)                | Mailgun sending domain (e.g. `mg.yourdomain.com`)                                           |
| `MAILGUN_API_BASE`                      | Tracker (alerts)                | Optional; default `https://api.mailgun.net` (EU: `https://api.eu.mailgun.net`)              |

**Dev vs product email:** SMTP env vars suit **self-host / operator** mail (e.g. personal Gmail). For production, use a transactional provider with a verified domain — see [Transactional alert email](#transactional-alert-email) below.

Never commit values; use your host’s secret manager or encrypted env.

<a id="api-rate-limiting"></a>
<details>
<summary><b>API rate limiting</b></summary>

Hosted database mode enables rate limiting automatically. Counters are stored in
SQLite or PostgreSQL so PostgreSQL deployments share limits across every API
instance. File mode remains unlimited unless `MARKETHELM_RATE_LIMIT_ENABLED=true`;
in that mode counters are process-local and intended only for development.

The API returns `429 Too Many Requests` with `Retry-After` and
`X-RateLimit-*` response headers. If the shared rate-limit database is unavailable,
hosted API requests fail closed with `503` instead of silently bypassing limits.

Do not trust forwarded client headers by default. When a known load balancer or
reverse proxy connects directly to MarketHelm, set `MARKETHELM_TRUSTED_PROXY_CIDRS`
to only that proxy network. The middleware walks `X-Forwarded-For` from right to
left and selects the first untrusted hop, preventing a client-supplied prefix from
bypassing per-client limits.

---

</details>

</details>

<a id="transactional-alert-email"></a>
<details>
<summary><b>Transactional alert email</b></summary>

Helmtower users only enter their **To** address. The platform operator configures **how** email is sent via environment variables (never in git).

<a id="provider-selection"></a>
<details>
<summary><b>Provider selection</b></summary>

Set `ALERT_EMAIL_PROVIDER` explicitly, or omit it and let MarketHelm auto-detect from API keys:

| Provider           | When to use                                | Required env                                                                 |
| ------------------ | ------------------------------------------ | ---------------------------------------------------------------------------- |
| **SMTP** (default) | Dev, self-host, or **AWS SES SMTP relay**  | `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD`, `ALERT_EMAIL_FROM` or `SMTP_USER` |
| **SendGrid**       | Hosted product with verified sender domain | `SENDGRID_API_KEY`, `ALERT_EMAIL_FROM`                                       |
| **Mailgun**        | Hosted product with Mailgun domain         | `MAILGUN_API_KEY`, `MAILGUN_DOMAIN`, `ALERT_EMAIL_FROM`                      |

Users still set `email_to` in Helmtower (or `ALERT_EMAIL_TO` as a default). Secrets stay in the host environment only.

</details>

<a id="sendgrid-example"></a>
<details>
<summary><b>SendGrid example</b></summary>

```bash
export ALERT_EMAIL_PROVIDER=sendgrid
export SENDGRID_API_KEY=SG.xxxx
export ALERT_EMAIL_FROM="MarketHelm Alerts <alerts@yourdomain.com>"
```

Verify the sender domain in SendGrid (SPF/DKIM) before going live.

</details>

<a id="mailgun-example"></a>
<details>
<summary><b>Mailgun example</b></summary>

```bash
export ALERT_EMAIL_PROVIDER=mailgun
export MAILGUN_API_KEY=key-xxxx
export MAILGUN_DOMAIN=mg.yourdomain.com
export ALERT_EMAIL_FROM="MarketHelm Alerts <alerts@yourdomain.com>"
# EU region:
# export MAILGUN_API_BASE=https://api.eu.mailgun.net
```

</details>

<a id="aws-ses-smtp-relay"></a>
<details>
<summary><b>AWS SES (SMTP relay)</b></summary>

SES works with the **SMTP** provider — no separate integration required:

```bash
export ALERT_EMAIL_PROVIDER=smtp
export SMTP_HOST=email-smtp.us-east-1.amazonaws.com
export SMTP_PORT=587
export SMTP_USER=your-ses-smtp-username
export SMTP_PASSWORD=your-ses-smtp-password
export ALERT_EMAIL_FROM="MarketHelm Alerts <alerts@yourdomain.com>"
```

Generate SMTP credentials in the AWS SES console and verify your domain first.

</details>

<a id="test-delivery"></a>
<details>
<summary><b>Test delivery</b></summary>

```bash
markethelm alerts test <alert-id>
```

Or use **Send test** in Helmtower (`/alerts`). The test uses the same provider as production alerts.

---

</details>

</details>

<a id="typical-deployment-layout"></a>
<details>
<summary><b>Typical deployment layout</b></summary>

1. **Backend** — Run FastAPI (`uvicorn` or `python main.py`) with `DATA_DIR` and `FINNHUB_API_KEY` set.
2. **Frontend** — Build `dashboard/frontend` (`npm run build`) and serve `dist/` from a static host (or the same reverse proxy).
3. **Scheduler** — Run the daily tracker on a schedule (cron, GitHub Actions with self-hosted runner, or the platform’s scheduler) **or** use the dashboard “Fetch New” flow if you only trigger manually.

**CORS:** set `CORS_ORIGINS` to your frontend origin so the browser can call the API.

---

</details>

<a id="when-you-go-live"></a>
<details>
<summary><b>When you go live</b></summary>

Use this when moving from **local dev** to a **public host**. For day-to-day development, Gmail SMTP in `.env` is enough — skip this section until you deploy.

1. **Host and ingress** — provision TLS, a persistent `DATA_DIR`, and an explicitly trusted reverse-proxy CIDR.
2. **Database** — use managed PostgreSQL for hosted mode; set `MARKETHELM_DATABASE_URL`, verify migrations, pooling, TLS, backups, and restore.
3. **Deploy API and worker** — run `markethelm-web` plus a separate `markethelm alerts run --loop` process using the same database and secrets.
4. **Auth** — set `MARKETHELM_AUTH_SECRET`, `MARKETHELM_PUBLIC_URL`, and email-verification policy; test registration, verification, reset, password change, logout, and deletion.
5. **Daily tracker** — schedule `markethelm` and persist its shared market-data output.
6. **Email** — verify a sender domain and configure SendGrid, Mailgun, or SES SMTP. Users only enter their recipient address in Helmtower.
7. **Operations** — configure rate limits/proxies, collect `/metrics`, and monitor `/health/ready` plus `/health/worker`.
8. **Secrets** — keep all credentials in the host secret manager; never commit `.env`.
9. **Staging proof** — test cross-tenant isolation, a real alert with the dashboard stopped, provider failures/retries, backup/restore, and worker recovery before public traffic.

Roadmap context: [PROJECT_STATUS.md](PROJECT_STATUS.md).

---

</details>

<a id="future-automated-trading-not-implemented"></a>
<details>
<summary><b>Future: automated trading (not implemented)</b></summary>

This repo today is **analysis + dashboard + alerts**. It does **not** place orders.
The current product direction and capability matrix are in
[PROJECT_STATUS.md](PROJECT_STATUS.md#product-direction). If you later add
**automated buy/sell**:

1. **Broker API** — You need a broker that exposes **order placement** (e.g. Alpaca, Interactive Brokers, Tradier). Finnhub is **market data**, not a substitute for execution.
2. **Secrets** — Trading keys must live only in **host secrets**; rotate and scope to paper vs live.
3. **Persistence** — Use a **database** (e.g. PostgreSQL) for orders, positions, and audit logs—**before** trusting real money.
4. **Safety** — Paper trading first, hard limits (max position, max loss), kill switch, full logging.

This is **not legal or financial advice**; follow your broker’s terms and applicable regulations.

---

</details>

<a id="docker-cli-tracker"></a>
<details>
<summary><b>Docker (CLI tracker)</b></summary>

Build and run the daily tracker in a container:

```bash
docker build -t markethelm:latest .
docker run --rm -e FINNHUB_API_KEY=your-key markethelm:latest
# Or: docker run --rm --env-file .env markethelm:latest
```

Mount persistent data:

```bash
docker run --rm --env-file .env \
  -v /var/lib/markethelm/data:/app/data \
  -v /var/lib/markethelm/logs:/app/logs \
  markethelm:latest
```

<a id="docker-compose"></a>
<details>
<summary><b>Docker Compose</b></summary>

```yaml
services:
  markethelm:
    build: .
    environment:
      - FINNHUB_API_KEY=${FINNHUB_API_KEY}
      - OPENAI_API_KEY=${OPENAI_API_KEY}
    volumes:
      - ./data:/app/data
      - ./logs:/app/logs
```

---

</details>

</details>

<a id="scheduled-runs"></a>
<details>
<summary><b>Scheduled runs</b></summary>

Use cron (Linux/Mac), Task Scheduler (Windows), or systemd to run once per day.

**Cron example:**

```bash
0 9 * * * docker run --rm -e FINNHUB_API_KEY=$(cat /path/to/key) markethelm:latest >> /var/log/markethelm.log 2>&1
```

For alert evaluation on a schedule (independent of dashboard access), use
`markethelm alerts run --loop`. Alert component boundaries and unsupported rule
types/channels are documented in [ARCHITECTURE.md](ARCHITECTURE.md#alert-workflow).

---

</details>

<a id="kubernetes"></a>
<details>
<summary><b>Kubernetes</b></summary>

Use `k8s/markethelm-cronjob.yaml` as a CronJob. Create secrets first:

```bash
kubectl create secret generic markethelm-secrets \
  --from-literal=FINNHUB_API_KEY=your-key \
  --from-literal=OPENAI_API_KEY=your-key
```

Mount a persistent volume for `DATA_DIR` so history survives pod restarts.

---

</details>

<a id="cloud-platforms"></a>
<details>
<summary><b>Cloud platforms</b></summary>

Common patterns:

- **AWS** — ECS task + EventBridge schedule; secrets in Secrets Manager.
- **GCP** — Cloud Run job + Cloud Scheduler; secrets in Secret Manager.
- **Azure** — Container Instances + Logic Apps; secrets in Key Vault.

Store API keys in the platform secret manager; never bake them into images.

---

</details>

<a id="security"></a>
<details>
<summary><b>Security</b></summary>

- Never commit keys; `.env` is gitignored.
- Use secret stores in production (AWS Secrets Manager, GCP Secret Manager, Azure Key Vault).
- Rotate keys periodically; audit Finnhub usage at <https://finnhub.io/dashboard>.

---

</details>

## Related

- [PROJECT_STATUS.md](PROJECT_STATUS.md) — roadmap and future execution notes
- [Dashboard README](../dashboard/README.md) — local dev, env vars
- [USAGE.md](USAGE.md) — CLI entry points and output files
- [Contributing](../CONTRIBUTING.md) — development workflow
