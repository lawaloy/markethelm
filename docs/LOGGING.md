# Logging

MarketHelm uses one Python logging configuration for the CLI, API, workers, and
scripts, and a shared browser logger for the React application. Both use UTC
timestamps, severity, the source module or component, and a descriptive message:

```text
2026-10-22T12:00:00Z [WARNING] src.alerts.price_history: Provider candle history unavailable for AAPL
```

## Python

Request a logger at module scope. This does not create files or install handlers:

```python
from src.core.logger import get_logger

logger = get_logger(__name__)

logger.info("Fetched %s symbols", count)
logger.warning(f"No saved bars for {symbol}")

try:
    refresh_market_data()
except Exception:
    logger.exception("Market refresh failed")
```

Loggers are standard `logging.Logger` instances. Existing f-strings and `%s`
arguments are both supported; interpolation syntax is not the convention being
enforced. Use a meaningful message and include the symbol, job ID, or operation
needed to understand it. Use `logger.exception(...)` inside an exception handler
when a traceback is useful; `logger.error(..., exc_info=True)` also works.

Entry points call `configure_logging()` after loading their environment and before
running application work. The dashboard bootstrap, CLI (including alert workers),
and scripts that call application services already do this. Repeated calls share
the same handlers. Modules must not call `setup_logger`, `basicConfig`, create
handlers, or configure logging during import. `setup_logger` remains available for
compatibility with external callers.

| Setting                | Default | Behavior                                                                  |
| ---------------------- | ------- | ------------------------------------------------------------------------- |
| `MARKETHELM_LOG_LEVEL` | `INFO`  | Minimum console severity: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `MARKETHELM_LOG_DIR`   | `logs`  | Shared log directory; set to an empty string for console only             |

The console writes to stderr. All application modules share a general file
(`markethelm_YYYY-MM-DD.log`, DEBUG and above) and an error file
(`markethelm_errors_YYYY-MM-DD.log`, ERROR and above). The filenames use the UTC
startup date. File creation failures leave console logging available. Uvicorn
access and lifecycle logs use the same configuration. `configure_logging(force=True)`
explicitly closes and replaces MarketHelm's handlers; unrelated handlers such as
pytest capture remain attached.

Programmatic consumers may call `configure_logging()` themselves before using
application services. A custom console level can also be supplied as
`configure_logging(level="DEBUG")`. Applications can disable files explicitly with
`configure_logging(log_dir="")`. Module loggers allow DEBUG records into the
shared general file; the console applies its own level filter. Third-party
loggers inherit the root's configured level unless their library sets one.

## Browser

Create a scoped logger once per module:

```typescript
import { getLogger } from '../utils/logger';

const logger = getLogger('services/api');

logger.info('Refresh complete');
logger.warning('Using saved data');
logger.error('Refresh failed', error);
```

The API provides `debug`, `info`, `warning`, `error`, and `critical`. Additional
arguments retain their original objects for browser inspection. `VITE_LOG_LEVEL`
sets the minimum severity at build time and defaults to `INFO`. Browser messages
use the same UTC/severity/scope format as Python. They stay in the browser console;
there is no automatic upload to the backend. The API interceptor retains its
existing development-only request diagnostics.

## Severity and output conventions

| Level    | Use                                                                    |
| -------- | ---------------------------------------------------------------------- |
| DEBUG    | Details used to investigate behavior                                   |
| INFO     | Normal progress and completed operations                               |
| WARNING  | Recoverable failures, unavailable optional services, fallback behavior |
| ERROR    | An operation failed and could not deliver its intended result          |
| CRITICAL | The process cannot continue                                            |

Do not include credentials, authorization headers, cookies, or full sensitive
request payloads in logs. Log an operation and safe identifiers instead.

CLI tables, generated reports, health-probe values, and JSON consumed by shell
commands are command output and may use `print`. Operational diagnostics from
application code use the shared logger. Keeping diagnostics on stderr preserves
machine-readable stdout.

The Python suite checks application and script imports to prevent independent
logging implementations. Frontend ESLint rejects direct console calls outside
the shared logger. Extend these shared modules when adding a new destination or
format rather than configuring an individual feature.
