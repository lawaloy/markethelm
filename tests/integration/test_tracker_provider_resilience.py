"""Service-boundary coverage for Finnhub failures in a complete tracker run."""

from __future__ import annotations

import json
import socket
import threading
import time
from collections import Counter
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from requests.adapters import HTTPAdapter

from src.analysis.backtesting import backtest_data_dir
from src.cli.commands import main as cli_main
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
                "t": int(time.time()),
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


def _workflow(monkeypatch, tmp_path: Path, server: _FinnhubStub, symbols: list[str]):
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
    workflow = StockTrackerWorkflow(include_profile=False)
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
