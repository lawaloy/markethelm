"""Tests for alert job processor (evaluate + deliver)."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from src.alerts.alert_engine import AlertEngine, LogNotifier
from src.alerts.job_processor import _process_deliver, process_job_queue
from src.alerts.notifiers.email_notifier import EmailNotifier
from src.alerts.notifiers.webhook_notifier import WebhookNotifier
from src.storage.alert_jobs import (
    JOB_DELIVER,
    JOB_EVALUATE_SYMBOL,
    STATUS_COMPLETED,
    STATUS_FAILED,
    claim_jobs,
    enqueue_job,
    pending_job_count,
)
from src.storage.alert_watches import record_trigger, sync_watches_from_config
from src.storage.database import get_connection, init_database
from src.storage.user_alerts import save_user_alerts_config
from src.storage.users import create_user


@pytest.fixture
def db_user(tmp_path, monkeypatch):
    db_path = tmp_path / "processor.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    init_database()
    user = create_user("proc@example.com", "password123")
    return user["id"]


def _watch_config():
    return {
        "defaults": {},
        "alerts": [
            {
                "id": "aapl-low",
                "name": "AAPL low",
                "enabled": True,
                "cooldown_minutes": 0,
                "condition": {
                    "type": "price_threshold",
                    "symbol": "AAPL",
                    "operator": "less_than",
                    "value": 200,
                },
                "notifiers": [{"type": "console"}],
            }
        ],
    }


class TestJobProcessor:
    def test_evaluate_enqueues_and_processes_deliver(self, db_user):
        sync_watches_from_config(db_user, _watch_config())
        enqueue_job(JOB_EVALUATE_SYMBOL, {"symbol": "AAPL", "price": 150.0, "tick_id": "t1"})

        with patch("src.alerts.alert_engine.LogNotifier.send", return_value=True):
            stats = process_job_queue("test-worker")

        assert stats["evaluated"] == 1
        assert stats["delivered"] == 1
        assert stats["failed"] == 0
        assert pending_job_count([JOB_DELIVER]) == 0

    def test_process_job_queue_drains_ready_jobs_beyond_limit(self, db_user):
        sync_watches_from_config(db_user, _watch_config())
        for index in range(3):
            enqueue_job(
                JOB_EVALUATE_SYMBOL,
                {"symbol": "AAPL", "price": 150.0, "tick_id": f"t{index}"},
            )

        with patch("src.alerts.alert_engine.LogNotifier.send", return_value=True):
            stats = process_job_queue("test-worker", limit=2)

        assert stats["evaluated"] == 3
        assert stats["delivered"] == 3
        assert stats["failed"] == 0
        assert pending_job_count([JOB_EVALUATE_SYMBOL]) == 0
        assert pending_job_count([JOB_DELIVER]) == 0

    def test_evaluate_fans_out_deliveries_for_all_users_watching_symbol(self, db_user):
        other_user = create_user("other-proc@example.com", "password123")["id"]
        sync_watches_from_config(db_user, _watch_config())
        other_config = _watch_config()
        other_config["alerts"][0]["id"] = "other-aapl-low"
        sync_watches_from_config(other_user, other_config)
        enqueue_job(JOB_EVALUATE_SYMBOL, {"symbol": "AAPL", "price": 150.0, "tick_id": "t1"})

        with patch("src.alerts.alert_engine.LogNotifier.send", return_value=True):
            stats = process_job_queue("test-worker")

        assert stats["evaluated"] == 1
        assert stats["delivered"] == 2
        assert stats["failed"] == 0
        with get_connection() as conn:
            rows = conn.execute("""
                SELECT user_id, alert_id FROM alert_trigger_state
                ORDER BY user_id, alert_id
                """).fetchall()
        assert {(row["user_id"], row["alert_id"]) for row in rows} == {
            (db_user, "aapl-low"),
            (other_user, "other-aapl-low"),
        }

    def test_evaluate_does_not_deliver_when_threshold_not_met(self, db_user):
        sync_watches_from_config(db_user, _watch_config())
        enqueue_job(JOB_EVALUATE_SYMBOL, {"symbol": "AAPL", "price": 250.0, "tick_id": "t1"})

        stats = process_job_queue("test-worker")

        assert stats["evaluated"] == 1
        assert stats["delivered"] == 0
        assert stats["failed"] == 0
        assert pending_job_count([JOB_DELIVER]) == 0
        with get_connection() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM alert_trigger_state").fetchone()
        assert row["n"] == 0

    def test_deliver_records_trigger(self, db_user):
        sync_watches_from_config(db_user, _watch_config())
        event = {
            "alert_id": "aapl-low",
            "alert_name": "AAPL low",
            "symbols": ["AAPL"],
            "timestamp": "2026-06-09T12:00:00+00:00",
            "condition_type": "price_threshold",
            "user_id": db_user,
        }
        enqueue_job(
            JOB_DELIVER,
            {"user_id": db_user, "alert_id": "aapl-low", "event": event},
        )

        with patch("src.alerts.alert_engine.LogNotifier.send", return_value=True):
            stats = process_job_queue("test-worker")

        assert stats["delivered"] == 1
        with get_connection() as conn:
            row = conn.execute(
                "SELECT last_triggered_at FROM alert_trigger_state WHERE user_id = ? AND alert_id "
                "= ?",
                (db_user, "aapl-low"),
            ).fetchone()
        assert row is not None

    def test_process_job_queue_recovers_and_delivers_stale_job(self, db_user):
        sync_watches_from_config(db_user, _watch_config())
        event = {
            "alert_id": "aapl-low",
            "alert_name": "AAPL low",
            "symbols": ["AAPL"],
            "timestamp": "2026-06-09T12:00:00+00:00",
            "condition_type": "price_threshold",
            "user_id": db_user,
        }
        job_id = enqueue_job(
            JOB_DELIVER,
            {"user_id": db_user, "alert_id": "aapl-low", "event": event},
        )
        claimed = claim_jobs([JOB_DELIVER], "crashed-worker")
        assert [job["id"] for job in claimed] == [job_id]
        with get_connection() as conn:
            conn.execute(
                "UPDATE alert_jobs SET locked_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00+00:00", job_id),
            )

        with patch("src.alerts.alert_engine.LogNotifier.send", return_value=True):
            stats = process_job_queue("recovery-worker")

        assert stats == {"evaluated": 0, "delivered": 1, "failed": 0}
        with get_connection() as conn:
            job = conn.execute(
                "SELECT status, attempts, locked_at, locked_by FROM alert_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            trigger = conn.execute(
                "SELECT 1 FROM alert_trigger_state WHERE user_id = ? AND alert_id = ?",
                (db_user, "aapl-low"),
            ).fetchone()
        assert job["status"] == STATUS_COMPLETED
        assert job["attempts"] == 2
        assert job["locked_at"] is None
        assert job["locked_by"] is None
        assert trigger is not None

    def test_stale_recovery_does_not_redeliver_completed_event(self, db_user):
        sync_watches_from_config(db_user, _watch_config())
        event = {
            "alert_id": "aapl-low",
            "alert_name": "AAPL low",
            "symbols": ["AAPL"],
            "timestamp": "2026-06-09T12:00:00Z",
            "condition_type": "price_threshold",
            "user_id": db_user,
        }
        job_id = enqueue_job(
            JOB_DELIVER,
            {"user_id": db_user, "alert_id": "aapl-low", "event": event},
        )
        claimed = claim_jobs([JOB_DELIVER], "crashed-after-delivery")

        with patch("src.alerts.alert_engine.LogNotifier.send", return_value=True) as mock_send:
            assert _process_deliver(claimed[0]) is True
            with get_connection() as conn:
                trigger_before = conn.execute(
                    """
                    SELECT last_triggered_at FROM alert_trigger_state
                    WHERE user_id = ? AND alert_id = ?
                    """,
                    (db_user, "aapl-low"),
                ).fetchone()["last_triggered_at"]
                conn.execute(
                    "UPDATE alert_jobs SET locked_at = ? WHERE id = ?",
                    ("2000-01-01T00:00:00+00:00", job_id),
                )

            stats = process_job_queue("recovery-worker")

        assert stats == {"evaluated": 0, "delivered": 0, "failed": 0}
        assert mock_send.call_count == 1
        with get_connection() as conn:
            job = conn.execute(
                "SELECT status, attempts FROM alert_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            trigger_after = conn.execute(
                """
                SELECT last_triggered_at FROM alert_trigger_state
                WHERE user_id = ? AND alert_id = ?
                """,
                (db_user, "aapl-low"),
            ).fetchone()["last_triggered_at"]
        assert job["status"] == STATUS_COMPLETED
        assert job["attempts"] == 2
        assert trigger_after == trigger_before

    def test_failed_delivery_does_not_record_trigger(self, db_user):
        sync_watches_from_config(db_user, _watch_config())
        event = {
            "alert_id": "aapl-low",
            "alert_name": "AAPL low",
            "symbols": ["AAPL"],
            "timestamp": "2026-06-09T12:00:00+00:00",
            "condition_type": "price_threshold",
            "user_id": db_user,
        }
        job_id = enqueue_job(
            JOB_DELIVER,
            {"user_id": db_user, "alert_id": "aapl-low", "event": event},
            max_attempts=1,
        )

        with patch("src.alerts.alert_engine.LogNotifier.send", return_value=False):
            stats = process_job_queue("test-worker")

        assert stats["delivered"] == 0
        assert stats["failed"] == 1
        with get_connection() as conn:
            trigger = conn.execute(
                "SELECT last_triggered_at FROM alert_trigger_state WHERE user_id = ? AND alert_id "
                "= ?",
                (db_user, "aapl-low"),
            ).fetchone()
            job = conn.execute(
                "SELECT status, last_error FROM alert_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        assert trigger is None
        assert job["status"] == STATUS_FAILED
        assert "Delivery failed" in job["last_error"]

    def test_queued_deliver_retries_when_user_channels_fail_despite_log(self, db_user):
        """Settings prepends log; a log hit must not complete a failed email/webhook send."""
        sync_watches_from_config(db_user, _watch_config())
        event = {
            "alert_id": "aapl-low",
            "alert_name": "AAPL low",
            "symbols": ["AAPL"],
            "timestamp": "2026-06-09T12:00:00+00:00",
            "condition_type": "price_threshold",
            "user_id": db_user,
        }
        job_id = enqueue_job(
            JOB_DELIVER,
            {"user_id": db_user, "alert_id": "aapl-low", "event": event},
            max_attempts=1,
        )

        class _FailingEmail(EmailNotifier):
            def __init__(self):
                pass

            def send(self, _event):
                return False

        class _FailingWebhook(WebhookNotifier):
            def __init__(self):
                pass

            def send(self, _event):
                return False

        with patch.object(
            AlertEngine,
            "_build_notifiers",
            return_value=[LogNotifier(), _FailingEmail(), _FailingWebhook()],
        ):
            stats = process_job_queue("test-worker")

        assert stats["delivered"] == 0
        assert stats["failed"] == 1
        with get_connection() as conn:
            trigger = conn.execute(
                "SELECT last_triggered_at FROM alert_trigger_state WHERE user_id = ? AND alert_id "
                "= ?",
                (db_user, "aapl-low"),
            ).fetchone()
            job = conn.execute(
                "SELECT status, last_error FROM alert_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        assert trigger is None
        assert job["status"] == STATUS_FAILED
        assert "Delivery failed" in job["last_error"]

    def test_removed_watch_fails_queued_delivery_without_recording_trigger(self, db_user):
        sync_watches_from_config(db_user, _watch_config())
        event = {
            "alert_id": "aapl-low",
            "alert_name": "AAPL low",
            "symbols": ["AAPL"],
            "timestamp": "2026-06-09T12:00:00+00:00",
            "condition_type": "price_threshold",
            "user_id": db_user,
        }
        job_id = enqueue_job(
            JOB_DELIVER,
            {"user_id": db_user, "alert_id": "aapl-low", "event": event},
            max_attempts=1,
        )
        save_user_alerts_config(db_user, {"defaults": {}, "alerts": []})

        stats = process_job_queue("test-worker")

        assert stats == {"evaluated": 0, "delivered": 0, "failed": 1}
        with get_connection() as conn:
            trigger = conn.execute(
                "SELECT 1 FROM alert_trigger_state WHERE user_id = ? AND alert_id = ?",
                (db_user, "aapl-low"),
            ).fetchone()
            job = conn.execute(
                "SELECT status, last_error FROM alert_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        assert trigger is None
        assert job["status"] == STATUS_FAILED
        assert "not found" in job["last_error"]
        assert pending_job_count([JOB_DELIVER]) == 0

    def test_deliver_failure_retries_without_recording_trigger(self, db_user):
        sync_watches_from_config(db_user, _watch_config())
        event = {
            "alert_id": "aapl-low",
            "alert_name": "AAPL low",
            "symbols": ["AAPL"],
            "timestamp": "2026-06-09T12:00:00+00:00",
            "condition_type": "price_threshold",
            "user_id": db_user,
        }
        enqueue_job(
            JOB_DELIVER,
            {"user_id": db_user, "alert_id": "aapl-low", "event": event},
        )

        with patch("src.alerts.alert_engine.LogNotifier.send", return_value=False):
            stats = process_job_queue("test-worker")

        assert stats["delivered"] == 0
        assert stats["failed"] == 1
        with get_connection() as conn:
            trigger = conn.execute(
                "SELECT last_triggered_at FROM alert_trigger_state WHERE user_id = ? AND alert_id "
                "= ?",
                (db_user, "aapl-low"),
            ).fetchone()
            job = conn.execute(
                "SELECT status, attempts, last_error FROM alert_jobs WHERE job_type = ?",
                (JOB_DELIVER,),
            ).fetchone()
        assert trigger is None
        assert job["status"] == "pending"
        assert job["attempts"] == 1
        assert "Delivery failed" in job["last_error"]

    def test_evaluate_skips_cooldown(self, db_user):
        config = _watch_config()
        config["alerts"][0]["cooldown_minutes"] = 60
        sync_watches_from_config(db_user, config)
        record_trigger(db_user, "aapl-low")
        enqueue_job(JOB_EVALUATE_SYMBOL, {"symbol": "AAPL", "price": 150.0})

        stats = process_job_queue("test-worker")
        assert stats["evaluated"] == 1
        assert pending_job_count([JOB_DELIVER]) == 0

    def test_evaluate_skips_cooldown_with_naive_last_triggered(self, db_user):
        """Naive ISO markers must not TypeError the whole evaluate job."""

        config = _watch_config()
        config["alerts"][0]["cooldown_minutes"] = 60
        sync_watches_from_config(db_user, config)
        naive_ts = (
            (datetime.now(timezone.utc) - timedelta(minutes=5)).replace(tzinfo=None).isoformat()
        )
        record_trigger(db_user, "aapl-low", timestamp=naive_ts)
        enqueue_job(JOB_EVALUATE_SYMBOL, {"symbol": "AAPL", "price": 150.0})

        stats = process_job_queue("test-worker")
        assert stats["evaluated"] == 1
        assert stats["failed"] == 0
        assert pending_job_count([JOB_DELIVER]) == 0

    def test_invalid_watch_does_not_block_symbol_for_other_users(self, db_user):
        bad_user = create_user("bad-watch@example.com", "password123")["id"]
        bad_config = _watch_config()
        bad_config["alerts"][0]["id"] = "bad-aapl"
        bad_config["alerts"][0]["condition"]["operator"] = "below"
        sync_watches_from_config(bad_user, bad_config)
        sync_watches_from_config(db_user, _watch_config())
        enqueue_job(JOB_EVALUATE_SYMBOL, {"symbol": "AAPL", "price": 150.0})

        with patch("src.alerts.alert_engine.LogNotifier.send", return_value=True):
            stats = process_job_queue("test-worker")

        assert stats["evaluated"] == 1
        assert stats["delivered"] == 1
        assert stats["failed"] == 0

    def test_deliver_uses_user_webhooks_not_global_env(self, db_user, monkeypatch):
        user_b = create_user("proc-b@example.com", "password123")
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://hooks.example/global")

        config_a = _watch_config()
        config_a["defaults"] = {
            "webhook_url": "https://hooks.example/user-a",
            "webhook_format": "json",
        }
        config_a["alerts"][0]["notifications"] = ["webhook"]
        save_user_alerts_config(db_user, config_a)

        config_b = _watch_config()
        config_b["defaults"] = {
            "webhook_url": "https://hooks.example/user-b",
            "webhook_format": "json",
        }
        config_b["alerts"][0]["notifications"] = ["webhook"]
        save_user_alerts_config(user_b["id"], config_b)

        enqueue_job(JOB_EVALUATE_SYMBOL, {"symbol": "AAPL", "price": 150.0, "tick_id": "t1"})

        with patch("src.alerts.notifiers.webhook_notifier.requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            stats = process_job_queue("test-worker")

        assert stats["evaluated"] == 1
        assert stats["delivered"] == 2
        urls = {call.args[0] for call in mock_post.call_args_list}
        assert urls == {"https://hooks.example/user-a", "https://hooks.example/user-b"}

    def test_evaluate_strips_padded_payload_symbol(self, db_user):
        """Whitespace around enqueue symbols must still match stored watches."""
        sync_watches_from_config(db_user, _watch_config())
        enqueue_job(
            JOB_EVALUATE_SYMBOL,
            {"symbol": "  aapl  ", "price": 150.0, "tick_id": "pad"},
        )

        with patch("src.alerts.alert_engine.LogNotifier.send", return_value=True):
            stats = process_job_queue("test-worker")

        assert stats == {"evaluated": 1, "delivered": 1, "failed": 0}
        with get_connection() as conn:
            job = conn.execute(
                "SELECT payload_json FROM alert_jobs WHERE job_type = ?",
                (JOB_DELIVER,),
            ).fetchone()
        event = json.loads(job["payload_json"])["event"]
        assert event["symbols"] == ["AAPL"]

    @pytest.mark.parametrize(
        "raw_symbol",
        [None, float("nan"), "nan", "NONE", "  ", ""],
    )
    def test_evaluate_skips_sentinel_payload_symbols(self, db_user, raw_symbol):
        """Poison/sentinel symbols must soft-skip, not look up fake NONE/NAN watches."""
        sync_watches_from_config(db_user, _watch_config())
        enqueue_job(
            JOB_EVALUATE_SYMBOL,
            {"symbol": raw_symbol, "price": 150.0, "tick_id": "sentinel"},
        )

        stats = process_job_queue("test-worker")

        assert stats == {"evaluated": 1, "delivered": 0, "failed": 0}
        assert pending_job_count([JOB_DELIVER]) == 0
        with get_connection() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM alert_trigger_state").fetchone()
        assert row["n"] == 0

    @pytest.mark.parametrize(
        "bad_price",
        ["x", None, float("nan"), float("inf"), float("-inf")],
    )
    def test_evaluate_skips_invalid_or_nonfinite_price(self, db_user, bad_price):
        """Bad/Inf price must soft-skip the job, not ValueError the worker."""
        sync_watches_from_config(db_user, _watch_config())
        enqueue_job(
            JOB_EVALUATE_SYMBOL,
            {"symbol": "AAPL", "price": bad_price, "tick_id": "bad-price"},
        )

        stats = process_job_queue("test-worker")

        assert stats == {"evaluated": 1, "delivered": 0, "failed": 0}
        assert pending_job_count([JOB_DELIVER]) == 0

    def test_evaluate_skips_missing_price_key(self, db_user):
        sync_watches_from_config(db_user, _watch_config())
        enqueue_job(JOB_EVALUATE_SYMBOL, {"symbol": "AAPL", "tick_id": "no-price"})

        stats = process_job_queue("test-worker")

        assert stats == {"evaluated": 1, "delivered": 0, "failed": 0}
        assert pending_job_count([JOB_DELIVER]) == 0

    def test_evaluate_skips_non_dict_condition_without_blocking_siblings(self, db_user):
        """Truthy list/string conditions AttributeError'd and aborted sibling watches."""
        bad_user = create_user("bad-condition@example.com", "password123")["id"]
        bad_config = _watch_config()
        bad_config["alerts"][0]["id"] = "bad-condition"
        sync_watches_from_config(bad_user, bad_config)
        sync_watches_from_config(db_user, _watch_config())

        with get_connection() as conn:
            poison = json.dumps(
                {
                    "id": "bad-condition",
                    "name": "Bad",
                    "enabled": True,
                    "condition": ["not", "a", "dict"],
                    "notifications": ["log"],
                }
            )
            conn.execute(
                "UPDATE alert_watches SET alert_json = ? WHERE user_id = ? AND alert_id = ?",
                (poison, bad_user, "bad-condition"),
            )
            conn.commit()

        enqueue_job(JOB_EVALUATE_SYMBOL, {"symbol": "AAPL", "price": 150.0})

        with patch("src.alerts.alert_engine.LogNotifier.send", return_value=True):
            stats = process_job_queue("test-worker")

        assert stats["evaluated"] == 1
        assert stats["delivered"] == 1
        assert stats["failed"] == 0
        with get_connection() as conn:
            rows = conn.execute("SELECT user_id, alert_id FROM alert_trigger_state").fetchall()
        assert {(row["user_id"], row["alert_id"]) for row in rows} == {
            (db_user, "aapl-low"),
        }

    def test_poison_evaluate_payload_skips_and_drains_siblings(self, db_user):
        """Missing price soft-skips; sibling evaluate jobs still process."""
        sync_watches_from_config(db_user, _watch_config())
        poison_id = enqueue_job(
            JOB_EVALUATE_SYMBOL,
            {"symbol": "AAPL", "tick_id": "poison"},  # missing price
            max_attempts=1,
        )
        enqueue_job(
            JOB_EVALUATE_SYMBOL,
            {"symbol": "AAPL", "price": 150.0, "tick_id": "ok"},
        )

        with patch("src.alerts.alert_engine.LogNotifier.send", return_value=True):
            stats = process_job_queue("test-worker")

        assert stats["evaluated"] == 2
        assert stats["delivered"] == 1
        assert stats["failed"] == 0
        assert pending_job_count([JOB_EVALUATE_SYMBOL]) == 0
        with get_connection() as conn:
            poison = conn.execute(
                "SELECT status FROM alert_jobs WHERE id = ?",
                (poison_id,),
            ).fetchone()
        assert poison["status"] == "completed"

    def test_process_job_queue_stops_after_max_batches(self, db_user, caplog):
        """A queue that never drains must stop after max_batches and warn."""
        safety_limit = 20
        claim_calls = {"eval": 0}

        def endless_claim(job_types, worker_id, limit=50):
            if JOB_EVALUATE_SYMBOL not in job_types:
                return []
            claim_calls["eval"] += 1
            if claim_calls["eval"] > safety_limit:
                raise AssertionError("process_job_queue did not stop at max_batches")
            return [{"id": f"synthetic-{claim_calls['eval']}", "payload": {}}]

        with (
            patch("src.alerts.job_processor.claim_jobs", side_effect=endless_claim),
            patch("src.alerts.job_processor._process_evaluate_symbol") as process_eval,
            patch("src.alerts.job_processor.complete_job", return_value=True),
            caplog.at_level("WARNING", logger="src.alerts.job_processor"),
        ):
            stats = process_job_queue(max_batches=3)

        assert claim_calls["eval"] == 3
        assert process_eval.call_count == 3
        assert stats == {"evaluated": 3, "delivered": 0, "failed": 0}
        assert "Stopped processing alert jobs after 3 batches" in caplog.text
