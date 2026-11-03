# MarketHelm tests

<a id="overview"></a>
<details open>
<summary><b>Overview</b></summary>

This directory contains Python unit, integration and security tests for MarketHelm.
The test layout mirrors the source packages. React component tests live beside
the frontend code, and Playwright browser tests live in [`e2e/tests`](../e2e/tests).
See the [dashboard verification guide](../dashboard/README.md#verification) for
frontend and browser test commands.

</details>

<a id="test-structure"></a>
<details>
<summary><b>Test Structure</b></summary>

Tests are organized to mirror the source code layout:

```text
tests/
|-- __init__.py
|-- conftest.py       # Pytest config: adds project root to sys.path
|-- README.md
|-- helpers/          # Shared test helpers
|-- alerts/           # mirrors src/alerts/ (engine, rules, storage, workers, notifiers)
|-- analysis/         # mirrors src/analysis/
|-- cli/              # mirrors src/cli/
|-- core/             # mirrors src/core/
|-- dashboard/        # mirrors dashboard/ (backend API and services)
|-- integration/      # PostgreSQL storage and provider-resilience tests
|-- scripts/          # scripts/ helpers plus workflow and compose hygiene checks
|-- services/         # mirrors src/services/
|-- storage/          # mirrors src/storage/ (accounts, alerts, market bars, migrations)
|-- utils/            # mirrors src/utils/
`-- workflows/        # mirrors src/workflows/
```

</details>

<a id="path-setup"></a>
<details>
<summary><b>Path Setup</b></summary>

`conftest.py` adds the project root to `sys.path`, so tests can use:

- `from src.analysis.xxx import ...`
- `from dashboard.backend.services.xxx import ...`

No manual `sys.path.insert` is needed in individual test files.

</details>

<a id="running-tests"></a>
<details open>
<summary><b>Running Tests</b></summary>

<a id="run-all-tests"></a>
<details open>
<summary><b>Run All Tests</b></summary>

```bash
# Database-free suite from the project root
python -m pytest tests/ -v --ignore=tests/integration/test_postgresql_storage.py

# PostgreSQL integration against a disposable test database
MARKETHELM_POSTGRES_TEST_URL=postgresql://user:password@localhost:5432/markethelm \
  python -m pytest tests/integration/test_postgresql_storage.py -v

# Or using unittest
python -m unittest discover tests/
```

</details>

<a id="run-specific-package"></a>
<details>
<summary><b>Run Specific Package</b></summary>

```bash
python -m pytest tests/analysis/
python -m pytest tests/core/
python -m pytest tests/dashboard/
```

</details>

<a id="run-specific-test-file"></a>
<details>
<summary><b>Run Specific Test File</b></summary>

```bash
python -m pytest tests/core/test_config.py -v
```

</details>

<a id="run-specific-test-class-or-method"></a>
<details>
<summary><b>Run Specific Test Class or Method</b></summary>

```bash
python -m pytest tests/core/test_config.py::TestCoreConfig
python -m pytest tests/core/test_config.py::TestCoreConfig::test_default_indices
```

</details>

<a id="run-with-coverage"></a>
<details>
<summary><b>Run with Coverage</b></summary>

```bash
pip install pytest-cov
python -m pytest tests/ --ignore=tests/integration/test_postgresql_storage.py --cov=src --cov-report=html
```

</details>

</details>

<a id="test-coverage"></a>
<details>
<summary><b>Test Coverage</b></summary>

| Module                                      | Tests                                             |
| ------------------------------------------- | ------------------------------------------------- |
| `src/core/config.py`                        | Configuration loading and defaults                |
| `src/core/logger.py`                        | Logging setup and handlers                        |
| `src/services/api_client.py`                | Rate limiting and Finnhub API                     |
| `src/analysis/analyzer.py`                  | Stock data analysis                               |
| `src/analysis/ai_summarizer.py`             | Demo summary, fallback when no API key            |
| `src/analysis/projector.py`                 | Stock projections and recommendations             |
| `src/storage/data_storage.py`               | Data persistence                                  |
| `src/workflows/tracker.py`                  | Workflow integration with mocked deps             |
| `src/alerts/notifiers/webhook_notifier.py`  | Webhook URL resolution, POST payload              |
| `dashboard/backend/api`                     | Market, summary, health, history (incl. accuracy) |
| `dashboard/backend/services/data_loader.py` | Data loading, projection accuracy computation     |

</details>

<a id="writing-new-tests"></a>
<details>
<summary><b>Writing New Tests</b></summary>

1. **Mirror the source structure**  
   Place tests in the matching package:
   - `src/services/foo.py` → `tests/services/test_foo.py`

2. **Use descriptive names**  
   `test_` prefix, clear docstrings.

3. **Use `conftest.py` for shared setup**  
   Fixtures and path setup are centralized.

4. **Mock external dependencies**  
   Use `@patch` or `unittest.mock` for API calls and file I/O.

5. **Clean up resources**  
   Use `setUp`/`tearDown` or pytest fixtures for temp dirs.

</details>

<a id="dependencies"></a>
<details>
<summary><b>Dependencies</b></summary>

```bash
pip install pytest pytest-cov
```

</details>

<a id="next-priority"></a>
<details>
<summary><b>Next Priority</b></summary>

**Missing or light tests (by module):**

1. Integration tests for full workflow (end-to-end with temp `data/`)

**Roadmap:** [docs/PROJECT_STATUS.md](../docs/PROJECT_STATUS.md)

</details>
