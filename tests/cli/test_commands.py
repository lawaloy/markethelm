"""CLI entry-point flag plumbing for the daily tracker."""

from unittest.mock import MagicMock, patch

import pytest

from src.cli.commands import main


@pytest.mark.parametrize(
    ("argv", "include_profile", "use_screener", "top_n"),
    [
        (["markethelm"], True, True, None),
        (["markethelm", "--quote-only"], False, True, None),
        (["markethelm", "--no-screener"], True, False, None),
        (["markethelm", "--top-n", "25"], True, True, 25),
        (
            ["markethelm", "--quote-only", "--no-screener", "--top-n", "10"],
            False,
            False,
            10,
        ),
    ],
)
def test_main_forwards_flags_to_workflow(argv, include_profile, use_screener, top_n, monkeypatch):
    monkeypatch.setattr("sys.argv", argv)
    workflow = MagicMock()
    workflow.run.return_value = {"success": True, "analysis": {}, "metadata": {}}

    with patch("src.cli.commands.StockTrackerWorkflow", return_value=workflow) as ctor:
        with patch("src.cli.commands.display_results") as display:
            result = main()

    ctor.assert_called_once_with(include_profile=include_profile)
    workflow.run.assert_called_once_with(use_screener=use_screener, top_n_stocks=top_n)
    display.assert_called_once_with(workflow.run.return_value)
    assert result == 0


def test_main_returns_failure_when_daily_workflow_fails(monkeypatch):
    monkeypatch.setattr("sys.argv", ["markethelm"])
    workflow = MagicMock()
    workflow.run.return_value = {"success": False, "error": "No data fetched"}

    with patch("src.cli.commands.StockTrackerWorkflow", return_value=workflow):
        with patch("src.cli.commands.display_results") as display:
            result = main()

    assert result == 1
    display.assert_called_once_with(workflow.run.return_value)


def test_main_dispatches_alerts_subcommand_without_daily_workflow(monkeypatch):
    """`markethelm alerts ...` must hand off argv and skip the daily tracker."""
    monkeypatch.setattr(
        "sys.argv",
        ["markethelm", "alerts", "run", "--loop", "--interval", "120"],
    )

    with patch("src.cli.alerts_commands.main") as alerts_main:
        with patch("src.cli.commands.StockTrackerWorkflow") as ctor:
            main()

    alerts_main.assert_called_once_with(["run", "--loop", "--interval", "120"])
    ctor.assert_not_called()


def test_main_dispatches_backtest_subcommand_without_daily_workflow(monkeypatch):
    monkeypatch.setattr("sys.argv", ["markethelm", "backtest", "--days", "30"])

    with patch("src.cli.backtest_commands.main", return_value=0) as backtest_main:
        with patch("src.cli.commands.StockTrackerWorkflow") as ctor:
            with pytest.raises(SystemExit, match="0"):
                main()

    backtest_main.assert_called_once_with(["--days", "30"])
    ctor.assert_not_called()
