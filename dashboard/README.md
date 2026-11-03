# MarketHelm web dashboard development

This guide is only for developing or rebuilding the React/FastAPI dashboard.
For installing and running MarketHelm, start with the
[main README](../README.md). For hosted configuration, persistence, and secrets,
use the [deployment guide](../docs/DEPLOYMENT.md).

<a id="web-architecture"></a>
<details>
<summary><b>Web architecture</b></summary>

- `dashboard/frontend/` contains the React and TypeScript application.
- `dashboard/backend/` contains the FastAPI application and API routes.
- During development, Vite serves the UI on port 3000 and proxies `/api` to
  FastAPI on port 8000.
- A production build is written to `dashboard/backend/static/`. FastAPI then
  serves the compiled UI and API together on port 8000.

The frontend is therefore part of the web application image; it is not normally
deployed as an independent Vercel or Netlify application.

</details>

<a id="project-layout"></a>
<details>
<summary><b>Project layout and pages</b></summary>

```text
dashboard/
|-- backend/                # FastAPI entry point, API routes and services
|   `-- static/             # Generated production SPA
`-- frontend/
    `-- src/
        |-- App.tsx         # Routes, shared navigation and providers
        |-- components/     # Cards, charts, forms and layout
        |-- contexts/       # Authentication and theme state
        |-- pages/          # Views listed below
        |-- services/       # API client
        |-- types/          # Shared TypeScript types
        `-- utils/          # Formatting, logging and other helpers

e2e/                        # Repository-level Playwright browser suite
|-- tests/                  # Smoke, accessibility, Helmtower and failure handling
|-- scripts/                # Fixture seeding and README capture
`-- playwright.config.ts    # Chromium project, screenshots and traces
```

The routes are defined in [`App.tsx`](frontend/src/App.tsx):

| View                                      | Source in `frontend/src/pages/`              | Route                                                  |
| ----------------------------------------- | -------------------------------------------- | ------------------------------------------------------ |
| Market overview and brief                 | `Dashboard.tsx`, with embedded `Summary.tsx` | `/`; `/summary` redirects to `/#market-brief`          |
| Historical trends and projection accuracy | `HistoricalTrends.tsx`                       | `/historical`                                          |
| Helmtower watches and delivery settings   | `AlertsSettings.tsx`                         | `/alerts`                                              |
| Sign in and registration                  | `SignIn.tsx`                                 | `/sign-in`                                             |
| Password recovery and email verification  | `AccountRecovery.tsx`                        | `/forgot-password`, `/reset-password`, `/verify-email` |
| Account settings                          | `AccountSettings.tsx`                        | `/account`                                             |

`RequireAuth` wraps `/alerts` and `/account`; its behavior follows the configured
authentication mode. `Summary.tsx` renders the market brief inside the dashboard;
`/summary` redirects to that section.

</details>

<a id="development-with-hot-reload"></a>
<details open>
<summary><b>Development with hot reload</b></summary>

<a id="requirements"></a>
<details open>
<summary><b>Requirements</b></summary>

- Python 3.12 or newer
- The Node.js version in [`.nvmrc`](../.nvmrc)
- npm

From the repository root, install the Python package and frontend dependencies:

```bash
pip install -e .
cd dashboard/frontend
npm ci
cd ../..
```

Start FastAPI from the repository root:

```bash
python3 -m uvicorn dashboard.backend.main:app --reload --port 8000
```

In another terminal, start Vite:

```bash
cd dashboard/frontend
npm run dev
```

Open <http://localhost:3000>. The API documentation remains available at
<http://localhost:8000/docs>.

If port 3000 is occupied, use the repository's paired alternate ports. Start
FastAPI with `python3 -m uvicorn dashboard.backend.main:app --reload --port 8001`,
then run `npm run dev:3001` in `dashboard/frontend/` and open
<http://localhost:3001>.

On Windows PowerShell, replace `python3` in this guide with
`.\.venv\Scripts\python.exe`.

</details>

</details>

<a id="build-the-integrated-web-application"></a>
<details>
<summary><b>Build the integrated web application</b></summary>

Build the React UI:

```bash
cd dashboard/frontend
npm run build
```

The output goes to `dashboard/backend/static/`. Return to the repository root
and start the integrated application:

```bash
markethelm-web
```

Open <http://localhost:8000>. Release automation performs the same frontend
build before creating the Python package, and [`Dockerfile.web`](../Dockerfile.web)
performs it in a Node build stage before constructing the Python runtime image.

</details>

<a id="verification"></a>
<details>
<summary><b>Verification</b></summary>

Run frontend checks from `dashboard/frontend/`:

```bash
npm test
npm run build
```

Run backend tests from the repository root:

```bash
python3 -m pytest tests/dashboard/ -v
```

The repository also has a Playwright browser suite in [`e2e/tests`](../e2e/tests).
It runs Chromium against the built SPA and FastAPI, covering navigation, stock
history, Helmtower behavior, API failure recovery, keyboard interactions, compact
layouts and automated accessibility checks. React component tests run separately
through `npm test` in `dashboard/frontend/`.

For a local browser run, build the SPA as above. From the repository root,
install the backend dependencies and start the API with isolated fixture data.
This shell example is for macOS/Linux:

```bash
pip install -e ".[dev]" -r dashboard/backend/requirements.txt
export DATA_DIR="$(mktemp -d)"
export MARKETHELM_DATABASE_URL=""
export FINNHUB_API_KEY=""
export OPENAI_API_KEY=""
python3 e2e/scripts/seed_ci_data.py
python3 -m uvicorn dashboard.backend.main:app --host 127.0.0.1 --port 8000
```

Keep that API process running. In another terminal, from the repository root:

```bash
cd e2e
npm ci
npx playwright install chromium
npm test
```

On Linux, use `npx playwright install --with-deps chromium` if browser system
libraries are missing. `E2E_BASE_URL` overrides the default
`http://127.0.0.1:8000`. Seeding and the API must use the same disposable `DATA_DIR`;
the seeder writes representative market data into the configured store.

[`PR E2E`](../.github/workflows/pr-e2e.yml) builds the SPA, seeds fixtures, checks
the API with curl and runs Playwright on pull requests. It uploads screenshots
and test output as `e2e-test-results` and the HTML report as `playwright-report`.
See the workflow run's artifacts or its PR comment for these files. Browser
coverage currently uses Chromium; cross-browser coverage remains future work.

The complete required checks and development workflow are documented in
[CONTRIBUTING.md](../CONTRIBUTING.md).

</details>

<a id="configuration-and-behavior"></a>
<details>
<summary><b>Configuration and behavior</b></summary>

- `DATA_DIR` selects the market-data directory. A source checkout defaults to
  the repository's `data/`; an installed wheel defaults to the user data directory.
- `HOST`, `PORT`, and `UVICORN_RELOAD` control `markethelm-web`.
- `CORS_ORIGINS` configures allowed browser origins.
- `VITE_DEV_PORT` and `VITE_DEV_API_TARGET` affect only the Vite development server.
- Database, authentication, email, proxy, rate-limit, and alert-worker variables
  are documented only in [Deployment and persistence](../docs/DEPLOYMENT.md).

Feature availability and unfinished work are tracked only in
[Project status](../docs/PROJECT_STATUS.md). API groups and service boundaries
are documented in [Architecture](../docs/ARCHITECTURE.md#webapi-boundaries).

</details>
