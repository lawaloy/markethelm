#!/usr/bin/env python3
"""Live smoke test for alert notifiers (log, webhook, email).

Usage (from repo root):
  python scripts/smoke_alerts_live.py
  python scripts/smoke_alerts_live.py --webhook-url https://hooks.slack.com/...
  python scripts/smoke_alerts_live.py --discord-webhook-url https://discord.com/api/webhooks/...

Requires SMTP_* and ALERT_EMAIL_TO in .env or ~/.markethelm/.env for email.
Webhook defaults to https://httpbin.org/post when ALERT_WEBHOOK_URL is unset.
Discord uses --discord-webhook-url or DISCORD_WEBHOOK_URL (falls back to httpbin).
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from dotenv import load_dotenv

try:
    from scripts import _repo_path
except ImportError:  # run as ``python scripts/<name>.py``: repo root not on sys.path yet
    import _repo_path

from src.alerts.alert_paths import user_config_dir
from src.cli.alerts_commands import cmd_test
from src.core.logger import configure_logging

ROOT = _repo_path.ROOT


def load_env() -> None:
    load_dotenv(ROOT / ".env")
    user_env = user_config_dir() / ".env"
    if user_env.exists():
        load_dotenv(user_env, override=True)


def build_smoke_config(
    webhook_url: str,
    include_email: bool,
    discord_webhook_url: str | None = None,
) -> dict:
    alerts = [
        {
            "id": "smoke_log",
            "name": "Smoke Test (log)",
            "enabled": True,
            "notifications": ["log"],
            "condition": {
                "type": "price_threshold",
                "symbol": "TEST",
                "operator": "less_than",
                "value": 1,
            },
        },
        {
            "id": "smoke_webhook_json",
            "name": "Smoke Test (webhook JSON)",
            "enabled": True,
            "notifications": ["webhook"],
            "webhook_url": webhook_url,
            "webhook_format": "json",
            "condition": {
                "type": "price_threshold",
                "symbol": "TEST",
                "operator": "less_than",
                "value": 1,
            },
        },
        {
            "id": "smoke_webhook_slack",
            "name": "Smoke Test (webhook Slack format)",
            "enabled": True,
            "notifications": ["webhook"],
            "webhook_url": webhook_url,
            "webhook_format": "slack",
            "condition": {
                "type": "price_threshold",
                "symbol": "TEST",
                "operator": "less_than",
                "value": 1,
            },
        },
    ]
    if discord_webhook_url:
        alerts.append(
            {
                "id": "smoke_webhook_discord",
                "name": "Smoke Test (webhook Discord format)",
                "enabled": True,
                "notifications": ["webhook"],
                "webhook_url": discord_webhook_url,
                "webhook_format": "discord",
                "condition": {
                    "type": "price_threshold",
                    "symbol": "TEST",
                    "operator": "less_than",
                    "value": 1,
                },
            }
        )
    if include_email:
        alerts.append(
            {
                "id": "smoke_email",
                "name": "Smoke Test (email)",
                "enabled": True,
                "notifications": ["email"],
                "email_to": os.environ.get("ALERT_EMAIL_TO"),
                "condition": {
                    "type": "price_threshold",
                    "symbol": "TEST",
                    "operator": "less_than",
                    "value": 1,
                },
            }
        )
    return {"alerts": alerts}


def smtp_configured() -> bool:
    required = ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "ALERT_EMAIL_TO")
    return all(os.environ.get(key) for key in required)


def main() -> int:
    parser = argparse.ArgumentParser(description="Live alert notification smoke test")
    parser.add_argument(
        "--webhook-url",
        default=os.environ.get("ALERT_WEBHOOK_URL", "https://httpbin.org/post"),
        help="Webhook URL for JSON + Slack-format tests (default: httpbin.org/post)",
    )
    parser.add_argument(
        "--discord-webhook-url",
        default=os.environ.get("DISCORD_WEBHOOK_URL", "https://httpbin.org/post"),
        help="Discord webhook URL for Discord-format test (default: httpbin.org/post)",
    )
    parser.add_argument("--skip-webhook", action="store_true")
    parser.add_argument("--skip-email", action="store_true")
    args = parser.parse_args()

    load_env()
    configure_logging()
    if args.webhook_url == "https://httpbin.org/post" and not os.environ.get("ALERT_WEBHOOK_URL"):
        print("Webhook: using https://httpbin.org/post (HTTP 200 = success).")
        print("  For Slack, pass --webhook-url with your incoming webhook URL.\n")
    if args.discord_webhook_url == "https://httpbin.org/post" and not os.environ.get(
        "DISCORD_WEBHOOK_URL"
    ):
        print("Discord: using https://httpbin.org/post (HTTP 200 = success).")
        print(
            "  For a real channel message, pass --discord-webhook-url or set DISCORD_WEBHOOK_URL.\n"
        )

    include_email = not args.skip_email and smtp_configured()
    if not args.skip_email and not include_email:
        print("Email: SKIPPED — set SMTP_HOST, SMTP_USER, SMTP_PASSWORD, ALERT_EMAIL_TO in .env\n")

    config = build_smoke_config(
        args.webhook_url,
        include_email=include_email,
        discord_webhook_url=None if args.skip_webhook else args.discord_webhook_url,
    )
    config_path = Path(tempfile.mkdtemp(prefix="markethelm-alerts-smoke-")) / "alerts.json"
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    print(f"Smoke config: {config_path}\n")

    tests = ["smoke_log"]
    if not args.skip_webhook:
        tests.extend(["smoke_webhook_json", "smoke_webhook_slack", "smoke_webhook_discord"])
    if include_email:
        tests.append("smoke_email")

    failed = 0
    for alert_id in tests:
        print(f"--- Live test: {alert_id} ---")
        code = cmd_test(alert_id, dry_run=False, config_path=config_path)
        if code != 0:
            failed += 1
            print(f"FAILED ({code})\n")
        else:
            print("OK\n")

    print("=" * 60)
    if failed:
        print(f"Smoke finished with {failed} failure(s).")
        return 1
    print("Smoke finished — check logs, httpbin/Slack/Discord, and inbox if email ran.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
