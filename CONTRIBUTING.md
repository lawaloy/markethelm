# Contributing to MarketHelm

Thank you for your interest in contributing! This document provides guidelines for contributing to the project.

<a id="getting-started"></a>
<details open>
<summary><b>Getting Started</b></summary>

1. **Fork the repository** on GitHub
2. **Clone your fork** locally:

   ```bash
   git clone https://github.com/YOUR_USERNAME/markethelm.git
   cd markethelm
   ```

   **End users** can install from PyPI: `pip install markethelm` — see [PyPI](https://pypi.org/project/markethelm/) and the main README.

3. **Create a virtual environment**:

   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```

4. **Install in development mode**:

   ```bash
   pip install -e .[dev]  # package plus pytest, coverage, black, isort
   ```

</details>

<a id="package-version-single-source-of-truth"></a>
<details>
<summary><b>Package version (single source of truth)</b></summary>

The **canonical** release line for this repo is **`setup.cfg`** → **`[metadata]`** → **`version`** (e.g. `0.3.7`).

These must stay aligned for new contributors and CI:

- `dashboard/frontend/package.json` (`version`)
- `dashboard/frontend/package-lock.json` (root + `packages[""]` `version`)
- `dashboard/backend/main.py` (FastAPI `version=` and the root JSON `"version"` next to `"MarketHelm API"`)

**After you change `setup.cfg` version**, run:

```bash
python scripts/version_sync.py sync
```

**Check without writing:**

```bash
python scripts/version_sync.py check
```

The **CI** job runs **`check`** on every PR (read-only; it does **not** auto-fix so the PR stays an honest diff). **Publish to PyPI** sets `setup.cfg` from the **release tag** on the runner, then runs **`sync`** in the build job before **`npm ci`** so the wheel and UI metadata match that tag (still not committed to `main` from CI). Publishing a release goes to **TestPyPI** first (`testpypi` environment); only after that job succeeds does it publish to **PyPI**. PyPI is reachable only through a release (**Create Release**), and that includes urgent fixes: cut a patch release and it still goes TestPyPI first. A manual run of Publish to PyPI publishes to **TestPyPI only**, as a unique dev version (`scripts/dev_version.py`: next patch plus `.dev<run number>`, for example `0.3.8.dev42` when `setup.cfg` has `0.3.7`), so every test adds a new TestPyPI version; its Actions title is `Publish to TestPyPI only`. After a successful release publish, **Post-release sync to main** (separate workflow) may open a PR to align `main` with that tag.

</details>

<a id="development-workflow"></a>
<details>
<summary><b>Development Workflow</b></summary>

<a id="1-create-a-branch"></a>
<details>
<summary><b>1. Create a Branch</b></summary>

```bash
git checkout -b feature/your-feature-name
# or
git checkout -b fix/your-bug-fix
```

</details>

<a id="2-make-your-changes"></a>
<details>
<summary><b>2. Make Your Changes</b></summary>

- Follow the existing code structure
- Write clear, descriptive commit messages
- Add tests for new functionality
- Update documentation as needed

</details>

<a id="3-run-tests"></a>
<details>
<summary><b>3. Run Tests</b></summary>

```bash
# Run the database-free suite
python -m pytest tests/ -v --ignore=tests/integration/test_postgresql_storage.py

# Run the PostgreSQL integration test against a disposable test database
MARKETHELM_POSTGRES_TEST_URL=postgresql://user:password@localhost:5432/markethelm \
  python -m pytest tests/integration/test_postgresql_storage.py -v

# Run with coverage
python -m pytest tests/ --ignore=tests/integration/test_postgresql_storage.py --cov=src --cov-report=html

# Run specific test file
python -m pytest tests/core/test_config.py -v
```

</details>

<a id="4-check-code-quality"></a>
<details>
<summary><b>4. Check Code Quality</b></summary>

```bash
# Install black and isort (pinned in the dev extra)
pip install -e .[dev]

# Format the Python files your branch changed (line length 100, isort profile black)
python scripts/check_python_style.py --base origin/main --fix

# Check without changing files, exactly as CI does
python scripts/check_python_style.py --base origin/main

# flake8 is part of the same check; to run it alone on specific files
pip install flake8
flake8 path/to/changed_file.py
```

CI runs `scripts/check_python_style.py` on the Python files changed in each pull
request. **black**, **isort**, and **flake8** at 100 characters (configured in
`pyproject.toml` and `.flake8`) must all pass for those files. Files you do not
touch are not checked, so older findings elsewhere do not block your PR; if you
edit a file that has legacy findings, fix them in the same PR. CI also runs
flake8 with its error-only rules (`E9,F63,F7,F82`) on the whole repository.

This repository keeps no lint suppressions: no `# noqa`, no `per-file-ignores`, and
no `ignore` list in `.flake8`. Fix the finding instead of silencing it. One case to
know about: black writes a slice with complex bounds as `ham[lower + offset : upper + offset]`,
and flake8 reports that spacing as `E203`. Keep slice bounds simple, or assign complex
bounds to named variables first (`start = lower + offset`, `stop = upper + offset`,
`ham[start:stop]`). Simple slices such as `ham[1:9]` or `ham[lower:upper]` are not
affected. `scripts/check_python_style.py` prints this advice when it sees `E203`.

Frontend code is linted with ESLint (`react-hooks/rules-of-hooks` and
`react-hooks/exhaustive-deps` are errors, and warnings fail the run):

```bash
cd dashboard/frontend
npm --prefix tools/eslint ci   # once
npm run lint
```

ESLint lives in its own package, `dashboard/frontend/tools/eslint`, because
`typescript-eslint` does not support TypeScript 7 (the app's compiler) yet, so the
tooling pins its own TypeScript 6. Remove that folder and lint from the app package
once `typescript-eslint` supports TypeScript 7. Do not add `eslint-disable` comments;
fix the code instead.

</details>

<a id="5-commit-your-changes"></a>
<details>
<summary><b>5. Commit Your Changes</b></summary>

```bash
git add .
git commit -m "feat: add new feature X"
# or
git commit -m "fix: resolve issue with Y"
```

**Commit Message Guidelines:**

- `feat:` - New feature
- `fix:` - Bug fix
- `docs:` - Documentation changes
- `test:` - Adding or updating tests
- `refactor:` - Code refactoring
- `chore:` - Maintenance tasks

</details>

<a id="6-push-and-create-pull-request"></a>
<details>
<summary><b>6. Push and Create Pull Request</b></summary>

```bash
git push origin feature/your-feature-name
```

Create a Pull Request through GitHub or an installed GitHub integration using the
repo template format:

```markdown
## What + Why

- Why this change exists (at least one bullet)

## Checks

- [ ] `dashboard/frontend`: `npm ci` and `npm run build`
- [ ] `pytest tests/ --ignore=tests/integration/test_postgresql_storage.py` (repo root)

<!-- AUTO:START -->
<!-- AUTO:END -->
```

**Important:**

- Use **`## What + Why`**, not `## Summary` — matches [`.github/pull_request_template.md`](.github/pull_request_template.md).
- Include the **`<!-- AUTO:START -->` / `<!-- AUTO:END -->`** markers so the [PR Description workflow](.github/workflows/pr-description.yml) can refresh the file list without overwriting your text.
- Fill in **at least one What + Why bullet** before pushing again; until then, later syncs skip auto-updates so an empty template is not clobbered.
- For Codex-driven work, prefer the installed GitHub app/connector for PR creation,
  updates, checks, and auto-merge. Local `git` remains responsible for pushing the
  branch and synchronizing the working tree.

Also include when relevant:

- Reference to any related issues
- Screenshots (if UI changes)

</details>

</details>

<a id="code-structure"></a>
<details>
<summary><b>Code Structure</b></summary>

```text
src/
├── core/          # Core utilities (config, logging)
├── services/      # External data services (API, fetchers)
├── analysis/      # Data analysis & AI
├── storage/       # Data persistence
├── workflows/     # Business logic (reusable across interfaces)
└── cli/           # CLI interface (presentation layer)
```

**Key Principle**: Business logic in `workflows/` is reusable.
CLI/Web/API layers consume workflows for their specific presentation needs.

</details>

<a id="coding-standards"></a>
<details>
<summary><b>Coding Standards</b></summary>

<a id="python-style"></a>
<details>
<summary><b>Python Style</b></summary>

- Follow PEP 8
- Use type hints where appropriate
- Maximum line length: 100 characters (enforced by black and flake8 on changed files; see "Check Code Quality")
- Use descriptive variable names

</details>

<a id="imports"></a>
<details>
<summary><b>Imports</b></summary>

```python
# Standard library
import os
from pathlib import Path

# Third-party
import pandas as pd
import requests

# Local
from ..core.logger import setup_logger
from .api_client import FinnhubClient
```

</details>

<a id="documentation"></a>
<details>
<summary><b>Documentation</b></summary>

- Add docstrings to all functions and classes
- Use Google-style docstrings:

```python
def function_name(param1: str, param2: int) -> bool:
    """
    Brief description of function.

    Args:
        param1: Description of param1
        param2: Description of param2

    Returns:
        Description of return value

    Raises:
        ValueError: When something goes wrong
    """
    pass
```

</details>

<a id="testing"></a>
<details>
<summary><b>Testing</b></summary>

- Write tests for new features
- Maintain or improve code coverage
- Use descriptive test names:

```python
def test_function_returns_expected_value_when_given_valid_input(self):
    """Test that function returns correct value with valid input."""
    # Arrange
    input_data = create_test_data()

    # Act
    result = function_under_test(input_data)

    # Assert
    self.assertEqual(result, expected_value)
```

</details>

</details>

<a id="areas-for-contribution"></a>
<details>
<summary><b>Areas for Contribution</b></summary>

**Roadmap detail:** See [docs/PROJECT_STATUS.md](docs/PROJECT_STATUS.md) for current status, what shipped, what’s next, and deferred items. **Hosting:** [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

<a id="current-gaps--opportunities"></a>
<details>
<summary><b>Current Gaps &amp; Opportunities</b></summary>

- **Hosted readiness:** Container acceptance, persistence, recovery, backup/restore,
  bounded load, retention, and incident procedures are implemented. External gates
  are managed infrastructure, real-provider delivery, and monitoring sign-off — see
  [docs/PROJECT_STATUS.md](docs/PROJECT_STATUS.md).
- **Historical / accuracy:** Multi-day trends, exact XNYS-session **projection accuracy**, and confidence-band metrics (CLI, API, and Historical Trends UI) are in place; **risk-adjusted** and longer-horizon views are still open.
- **Real-time:** Data is batch/daily; refresh is explicit (not streaming).
- **Screening:** The screener filters on volume, price, daily change, and market cap only. Technical indicators are not screening filters yet (RSI exists only as an alert rule); MACD, Bollinger Bands, and moving averages are not implemented.

</details>

<a id="completed-milestones"></a>
<details>
<summary><b>Completed Milestones</b></summary>

- ✅ **Web Dashboard (v0.3+)** — Market overview, projections, Historical Trends, **projection accuracy**, **Helmtower** (`/alerts`).
- ✅ **Alerts** — `AlertEngine`, price/screening/RSI rules, shallow AND/OR compounds, cooldowns, **webhook** (JSON/Slack/Discord), **email** (SMTP + SendGrid/Mailgun), CLI, scheduled worker (`alerts run --loop`).
- ✅ **Projection validation**: exact XNYS trading-session targets and `markethelm backtest`, shared by the CLI, API, and Historical Trends UI, with confidence-band cohorts.

</details>

<a id="priority-features-ranked-by-impact"></a>
<details>
<summary><b>Priority Features (ranked by impact)</b></summary>

<a id="priority-1-alert--notification-system"></a>
<details>
<summary><b>Priority #1: Alert &amp; notification system</b></summary>

**Status:** Local and hosted foundations are shipped; infrastructure validation and
advanced rules/channels are next — [docs/PROJECT_STATUS.md](docs/PROJECT_STATUS.md).

**On `main`:**

- [x] Alert engine (price threshold, screening match), storage, cooldowns
- [x] Log + **webhook** notifiers (JSON, Slack, Discord)
- [x] **SMTP email** + transactional providers (SendGrid, Mailgun)
- [x] CLI: `alerts init`, `alerts list`, `alerts test`, `alerts run --loop`
- [x] **Helmtower** — dashboard Settings UI for watches, email, webhooks, test send
- [x] Delivery retry/backoff and per-channel status/history
- [x] Accounts, tenant-scoped storage, database queue/worker, and auth lifecycle

**Next:**

- [ ] Record managed PostgreSQL, real email/webhook, ingress, and monitoring
      evidence using the staging runbook
- [x] RSI threshold + shallow compound (price AND/OR RSI) rules in Helmtower
- [ ] Nested compounds / more indicators; later, SMS/push

---

</details>

<a id="priority-2-historical-trends--accuracy"></a>
<details>
<summary><b>Priority #2: Historical trends &amp; accuracy</b></summary>

**Status:** Partially implemented.

**Done / in repo:**

- [x] Multi-day aggregation, summary API, charts (confidence, recommendations, expected move)
- [x] Per-symbol historical chart with projection overlay
- [x] **Projection accuracy** — `GET /api/history/accuracy`, UI on Historical Trends
- [x] Accuracy by **confidence band** and exact XNYS-session target alignment

**Still to build:**

- [ ] **Risk-adjusted** and longer-horizon accuracy views
- [ ] Recommendation change timeline, volume patterns (as needed)

---

</details>

<a id="priority-3-web-dashboard-enhancements"></a>
<details>
<summary><b>Priority #3: Web dashboard enhancements</b></summary>

**Status:** Ongoing.

**Completed (examples):**

- [x] Dark mode, export, mobile-oriented layout (see dashboard README)
- [x] Projection accuracy views (Historical Trends)

**Still to build:**

- [ ] Code splitting / lazy routes, watchlist, shortcuts — see [dashboard/README.md](dashboard/README.md)

---

</details>

</details>

<a id="high-priority---additional-features"></a>
<details>
<summary><b>High Priority - Additional Features</b></summary>

- [ ] **Support for additional stock exchanges** (international markets: LSE, TSE, HKEX)
- [ ] **More screening filters** (technical indicators such as RSI, MACD, Bollinger Bands, and moving averages; RSI is currently available only as an alert rule)
- [ ] **Enhanced AI summaries** (sentiment analysis, news integration, contextual recommendations)
- [ ] **Sector analysis** (group stocks by sector, compare performance)
- [ ] **Portfolio tracking** (track multiple portfolios, performance metrics)
- [ ] **Strategy backtesting** (simulate trading strategies against historical data; `markethelm backtest` currently validates saved projections only)

</details>

<a id="medium-priority---improvements"></a>
<details>
<summary><b>Medium Priority - Improvements</b></summary>

- [ ] Additional unit tests where coverage is still thin
- [ ] Broader end-to-end workflow tests (provider-resilience integration tests already exist)
- [ ] Performance optimizations
- [ ] CLI improvements (progress bars, colors, interactive mode)

</details>

<a id="low-priority---enhancements"></a>
<details>
<summary><b>Low Priority - Enhancements</b></summary>

- [ ] Configuration validation with detailed error messages
- [ ] Additional export formats (Excel, Parquet; the dashboard already exports CSV and PDF)
- [ ] Enhanced error handling and recovery

</details>

</details>

<a id="reporting-issues"></a>
<details>
<summary><b>Reporting Issues</b></summary>

When reporting issues, please include:

1. **Description**: Clear description of the issue
2. **Steps to Reproduce**: Minimal steps to reproduce the problem
3. **Expected Behavior**: What you expected to happen
4. **Actual Behavior**: What actually happened
5. **Environment**:
   - OS (Windows/Mac/Linux)
   - Python version
   - Relevant package versions
6. **Logs**: Relevant log excerpts from `logs/` directory

</details>

## Questions?

- Open an issue for general questions
- Check existing issues and PRs first
- Be respectful and constructive

## License

By contributing, you agree that your contributions will be licensed under the MIT License.

Thank you for contributing!
