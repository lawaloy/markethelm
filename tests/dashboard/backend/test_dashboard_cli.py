"""Tests for the markethelm-web console entrypoint."""

from unittest.mock import patch

import pytest

from dashboard.backend.dashboard_cli import main, parse_port


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, 8000),
        ("", 8000),
        ("9000", 9000),
        ("not-a-port", 8000),
        ("0", 8000),
        ("65536", 8000),
        ("-1", 8000),
    ],
)
def test_parse_port_soft_fails_invalid_values(raw: str | None, expected: int) -> None:
    assert parse_port(raw) == expected


def test_dashboard_cli_passes_env_to_uvicorn(monkeypatch) -> None:
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.setenv("PORT", "9000")
    monkeypatch.setenv("UVICORN_RELOAD", "yes")

    with patch("uvicorn.run") as run:
        main()

    run.assert_called_once_with(
        "dashboard.backend.main:app",
        host="127.0.0.1",
        port=9000,
        reload=True,
    )


def test_dashboard_cli_defaults_and_reload_false(monkeypatch) -> None:
    monkeypatch.delenv("HOST", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.setenv("UVICORN_RELOAD", "no")

    with patch("uvicorn.run") as run:
        main()

    run.assert_called_once_with(
        "dashboard.backend.main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
    )


def test_dashboard_cli_falls_back_when_port_invalid(monkeypatch) -> None:
    monkeypatch.setenv("PORT", "abc")
    monkeypatch.delenv("HOST", raising=False)
    monkeypatch.delenv("UVICORN_RELOAD", raising=False)

    with patch("uvicorn.run") as run:
        main()

    run.assert_called_once_with(
        "dashboard.backend.main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
    )
