"""Service-boundary coverage for Finnhub failures in a complete tracker run."""

from __future__ import annotations

import json
import socket
import threading
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from requests.adapters import HTTPAdapter

import src.storage.market_refresh as refresh_store
import src.workflows.market_refresh as refresh_worker
from dashboard.backend.main import app
from src.analysis.backtesting import backtest_data_dir
from src.analysis.forward_evaluation import forward_report
from src.analysis.market_calendar import get_market_calendar
from src.analysis.return_candidate import MODEL_VERSION, bundled_candidate
from src.cli.commands import main as cli_main
from src.core.market_refresh import MarketRefreshSettings
from src.storage.database import init_database
from src.storage.forecast_ledger import ledger_snapshot
from src.storage.market_bars import list_market_bar_dates
from src.storage.projections_store import list_projection_dates
from src.workflows.tracker import StockTrackerWorkflow


class _FinnhubStub(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _FinnhubHandler)
        self.request_counts: Counter[str] = Counter()
        self.count_lock = threading.Lock()

    def record(self, symbol: str) -> int:
        with self.count_lock:
            self.request_counts[symbol] += 1
            return self.request_counts[symbol]


class _FinnhubHandler(BaseHTTPRequestHandler):
    server: _FinnhubStub

    def _handle_get(self) -> None:
        parsed = urlparse(self.path)
        symbol = parse_qs(parsed.query).get("symbol", [""])[0]
        attempt = self.server.record(symbol)

        if parsed.path != "/api/v1/quote":
            self.send_error(404)
            return

        if symbol == "THROTTLED" and attempt == 1:
            self.send_response(429)
            self.send_header("Retry-After", "0")
            self.end_headers()
            return

        if symbol == "MALFORMED":
            self._write(200, b"{not-json", "application/json")
            return

        if symbol == "DROPPED":
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return

        if symbol == "TIMEOUT":
            time.sleep(1.0)

        price = 125.0 if symbol == "THROTTLED" else 100.0
        payload = json.dumps(
            {
                "c": price,
                "h": price + 2,
                "l": price - 2,
                "o": price - 1,
                "pc": price - 1,
                "t": getattr(self.server, "quote_timestamp", int(time.time())),
                "v": 1_000_000,
            }
        ).encode("utf-8")
        self._write(200, payload, "application/json")

    # BaseHTTPRequestHandler dispatches GET requests to an attribute named ``do_GET``.
    do_GET = _handle_get

    def _write(self, status: int, payload: bytes, content_type: str) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            # Expected when the TIMEOUT client abandons its response.
            pass

    def log_message(self, _format: str, *_args: object) -> None:
        pass


@contextmanager
def _finnhub_stub():
    server = _FinnhubStub()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _workflow(
    monkeypatch,
    tmp_path: Path,
    server: _FinnhubStub,
    symbols: list[str],
    *,
    load_alerts: bool = True,
):
    monkeypatch.setenv("FINNHUB_API_KEY", "integration-test")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STOCK_FETCH_MAX_WORKERS", "1")
    monkeypatch.setenv("MARKETHELM_ALERTS_CONFIG", str(tmp_path / "no-alerts.json"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    monkeypatch.setattr("src.services.data_fetcher.get_indices_to_track", lambda: ["TEST"])
    monkeypatch.setattr("src.workflows.tracker.get_indices_to_track", lambda: ["TEST"])
    monkeypatch.setattr(
        "src.services.data_fetcher.IndexFetcher.get_index_symbols",
        lambda _self, _index: symbols,
    )
    monkeypatch.setattr(
        "src.services.api_client.time",
        SimpleNamespace(time=time.time, sleep=lambda _seconds: None),
    )
    workflow = StockTrackerWorkflow(include_profile=False, load_alerts=load_alerts)
    client = workflow.fetcher.api_client
    client.base_url = f"http://127.0.0.1:{server.server_port}/api/v1"
    client.session.mount("http://", HTTPAdapter(max_retries=0))

    # Keep the integration test quick while still exercising a real socket timeout.
    session_get = client.session.get

    def short_get(url, *, params, timeout):
        del timeout
        return session_get(url, params=params, timeout=0.5)

    monkeypatch.setattr(client.session, "get", short_get)
    return workflow


def test_tracker_keeps_valid_symbols_and_writes_backtestable_snapshots(monkeypatch, tmp_path):
    with _finnhub_stub() as server:
        workflow = _workflow(
            monkeypatch,
            tmp_path,
            server,
            ["GOOD", "THROTTLED", "MALFORMED", "DROPPED", "TIMEOUT"],
        )

        result = workflow.run(use_screener=False)

    assert result["success"] is True
    assert {row["symbol"] for row in result["data"]} == {"GOOD", "THROTTLED"}
    assert server.request_counts["THROTTLED"] == 2
    assert server.request_counts["MALFORMED"] >= 3
    assert server.request_counts["DROPPED"] >= 3
    assert server.request_counts["TIMEOUT"] >= 3

    assert list_market_bar_dates(data_dir=tmp_path, limit=10)
    assert (tmp_path / "market_bars.sqlite").is_file()
    assert list_projection_dates(data_dir=tmp_path, limit=10)
    assert not list(tmp_path.glob("projections_*.csv"))
    report = backtest_data_dir(tmp_path)
    assert report["summary"]["projectionCount"] == 2
    assert report["summary"]["validProjectionCount"] == 2
    assert report["summary"]["pendingCount"] == 2


def test_total_provider_failure_reaches_nonzero_cli_exit(monkeypatch, tmp_path):
    with _finnhub_stub() as server:
        workflow = _workflow(monkeypatch, tmp_path, server, ["MALFORMED", "DROPPED", "TIMEOUT"])
        monkeypatch.setattr("sys.argv", ["markethelm", "--no-screener", "--quote-only"])

        with patch("src.cli.commands.StockTrackerWorkflow", return_value=workflow):
            exit_code = cli_main()

    assert exit_code == 1
    assert not list_market_bar_dates(data_dir=tmp_path, limit=10)
    assert not list_projection_dates(data_dir=tmp_path, limit=10)
    assert not list(tmp_path.glob("projections_*.csv"))
    assert server.request_counts["MALFORMED"] >= 3
    assert server.request_counts["DROPPED"] >= 3
    assert server.request_counts["TIMEOUT"] >= 3


def test_scheduled_collection_populates_hosted_db_and_survives_restart(monkeypatch, tmp_path):
    settings = MarketRefreshSettings(min_symbols=2)
    now = refresh_store.utc_now()
    session = refresh_store.expected_session(now, settings)
    with _finnhub_stub() as server:
        server.quote_timestamp = int(get_market_calendar().session_close(session).timestamp())
        with patch(
            "src.workflows.tracker.AlertEngine.from_config",
            side_effect=AssertionError("collector must not load local delivery configuration"),
        ):
            workflow = _workflow(
                monkeypatch, tmp_path, server, ["GOOD", "THROTTLED", "MALFORMED"], load_alerts=False
            )
        monkeypatch.setenv(
            "MARKETHELM_DATABASE_URL", f"sqlite:///{(tmp_path/'hosted.db').as_posix()}"
        )
        monkeypatch.setenv("MARKETHELM_MARKET_REFRESH_ENABLED", "true")
        init_database()
        monkeypatch.setattr(refresh_worker, "StockTrackerWorkflow", lambda **kwargs: workflow)
        # Exercise the actual workflow/provider/storage boundary in this process;
        # the separate watchdog/timeout tests exercise the subprocess boundary.
        monkeypatch.setattr(
            refresh_worker,
            "_run_collector",
            lambda config, _session=None: (
                None if refresh_worker._collect(config) == 0 else "collector_failed"
            ),
        )
        assert refresh_store.market_data_health(now, settings)["ok"] is False
        with patch.object(workflow, "_run_alerts") as local_delivery:
            result = refresh_worker.run_once(settings)
        assert result["status"] == "success"
        assert result["quote_count"] == 2
        local_delivery.assert_not_called()
        assert server.request_counts["THROTTLED"] == 2
        calls = server.request_counts.copy()
        init_database()
        assert refresh_worker.run_once(settings)["status"] == "already_collected"
        assert server.request_counts == calls
        assert refresh_store.market_data_health(refresh_store.utc_now(), settings)["ok"] is True
        monkeypatch.setenv("MARKET_REFRESH_MIN_SYMBOLS", "2")
        response = TestClient(app).get("/health/market-data")
        assert response.status_code == 200
        assert response.json()["quote_count"] == 2
        assert response.json()["data_session"] == session
        assert "owner" not in response.json()


def test_scheduled_total_provider_failure_is_persisted_and_not_retried_immediately(
    monkeypatch, tmp_path
):
    settings = MarketRefreshSettings(min_symbols=1)
    with _finnhub_stub() as server:
        workflow = _workflow(monkeypatch, tmp_path, server, ["MALFORMED"])
        monkeypatch.setenv(
            "MARKETHELM_DATABASE_URL", f"sqlite:///{(tmp_path/'failed.db').as_posix()}"
        )
        init_database()
        monkeypatch.setattr(refresh_worker, "StockTrackerWorkflow", lambda **kwargs: workflow)
        monkeypatch.setattr(
            refresh_worker,
            "_run_collector",
            lambda config, _session=None: (
                None if refresh_worker._collect(config) == 0 else "collector_failed"
            ),
        )
        assert refresh_worker.run_once(settings)["status"] == "error"
        assert refresh_store.load_state()["error_code"] == "collector_failed"
        calls = server.request_counts.copy()
        assert refresh_worker.run_once(settings)["status"] == "retry_pending"
        assert server.request_counts == calls


@pytest.mark.parametrize("second_symbol", ["NEW", "MALFORMED"])
@pytest.mark.parametrize("with_candidate", [False, True])
def test_forward_ledger_collects_due_outcome_after_symbol_leaves_screener(
    monkeypatch, tmp_path, second_symbol, with_candidate
):
    """Real HTTP -> provider -> tracker -> immutable forecasts -> later exact outcome."""
    calendar = get_market_calendar()
    if with_candidate:
        anchor = calendar.date_to_session(
            bundled_candidate().frozen_at.date().isoformat(), direction="next"
        )
        origin = calendar.next_session(anchor).date().isoformat()
    else:
        origin = "2026-07-06"
    observed = calendar.sessions_window(origin, 7)[-1].date().isoformat()
    model_count = 5 if with_candidate else 4
    clock = {"now": calendar.session_close(origin).to_pydatetime() + timedelta(hours=1)}

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = clock["now"]
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)

    monkeypatch.setattr("src.services.api_client.datetime", FrozenDateTime)
    monkeypatch.setattr("src.analysis.projector.datetime", FrozenDateTime)
    monkeypatch.setattr("src.storage.forecast_ledger.utc_now", lambda: clock["now"])
    monkeypatch.setattr(
        "src.storage.data_storage._data_date_for_filename", lambda: clock["now"].date()
    )
    symbols = ["GOOD"]
    with _finnhub_stub() as server:
        server.quote_timestamp = int(calendar.session_close(origin).timestamp())
        workflow = _workflow(monkeypatch, tmp_path, server, symbols, load_alerts=False)
        first = workflow.run(use_screener=False)
        assert first["success"] is True
        assert first["metadata"]["forward_forecasts"]["new_cases"] == 1
        symbols[:] = [second_symbol]
        clock["now"] = calendar.session_close(observed).to_pydatetime() + timedelta(hours=1)
        server.quote_timestamp = int(calendar.session_close(observed).timestamp())
        second = workflow.run(use_screener=False)
        if second_symbol == "NEW":
            assert second["success"] is True
            assert set(second["projections"]) == {"NEW"}
            assert {row["symbol"] for row in second["data"]} == {"NEW"}
            assert second["metadata"]["forward_forecasts"]["new_scores"] == model_count
            assert server.request_counts["NEW"] == 1
        else:
            assert second["success"] is False
        assert server.request_counts["GOOD"] == 2  # initial input and due-only outcome
    snapshot = ledger_snapshot(data_dir=tmp_path)
    assert len(snapshot["forecast_cases"]) == (2 if second_symbol == "NEW" else 1)
    assert len(snapshot["forecast_scores"]) == model_count
    report = forward_report(data_dir=tmp_path, now=clock["now"])
    assert report["models"]["heuristic-v1"]["metrics"]["scoredCount"] == 1
    if with_candidate:
        shadow = report["models"][MODEL_VERSION]
        assert shadow["metrics"]["scoredCount"] == 1
        assert shadow["comparisons"]["heuristic-v1"]["scoredPairCount"] == 1
        assert first["projections"]["GOOD"]["projection_model_version"] == "heuristic-v1"
