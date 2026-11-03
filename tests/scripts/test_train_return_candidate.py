"""Public offline trainer against a real synthetic SQLite archive."""

import json
import os
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone

from src.storage.market_bars import market_bars_connection, upsert_market_bars
from src.storage.projections_store import upsert_projections
from tests.helpers.forecasts import projection, stock, training_samples


def test_offline_training_cli_freezes_a_bundle_and_refuses_to_replace_it(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", "")
    archive, bundle = tmp_path / "archive", tmp_path / "bundle"
    origins, observations = defaultdict(list), defaultdict(list)
    for row in training_samples():
        origins[row["runDate"]].append(
            projection(
                datetime.fromisoformat(row["generatedAt"]),
                symbol=row["symbol"],
                target_mid=row["predicted"],
                confidence=60,
            )
        )
        observations[row["outcomeAvailableAt"]].append(
            stock(
                symbol=row["symbol"], outcome_session=row["targetDate"], outcome_close=row["actual"]
            )
        )
    for day, rows in origins.items():
        upsert_projections(rows, day, data_dir=archive)
    for available, rows in observations.items():
        day = available[:10]
        upsert_market_bars(rows, day, data_dir=archive)
        with market_bars_connection(data_dir=archive) as conn:
            conn.execute("UPDATE market_bars SET written_at=? WHERE trade_date=?", (available, day))
    env = {**os.environ, "MARKETHELM_DATABASE_URL": "", "FINNHUB_API_KEY": "", "OPENAI_API_KEY": ""}
    command = [
        sys.executable,
        "scripts/train_return_candidate.py",
        "--data-dir",
        str(archive),
        "--output-dir",
        str(bundle),
        "--frozen-at",
        datetime.now(timezone.utc).isoformat(),
    ]
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr
    artifact_file = bundle / "ridge_return_v1.json"
    original = artifact_file.read_bytes()
    artifact = json.loads(original)
    report = json.loads((bundle / "diagnostic.json").read_text())
    assert artifact["training"]["sample_count"] == 450
    assert len(artifact["coefficients"]) == 2
    assert report["heldOut"]["candidate"]["sampleCount"] == 150
    assert artifact["deployment"] == "shadow_only"
    again = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
    assert again.returncode == 1
    assert artifact_file.read_bytes() == original
