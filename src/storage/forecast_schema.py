"""Shared append-only forecast schema for SQLite sidecars and hosted databases."""

TABLES = (
    "forecast_protocols",
    "forecast_models",
    "forecast_cases",
    "forecast_predictions",
    "forecast_outcomes",
    "forecast_scores",
)

TABLE_DDL = (
    """CREATE TABLE IF NOT EXISTS forecast_protocols (
        version TEXT PRIMARY KEY, definition_json TEXT NOT NULL,
        definition_sha256 TEXT NOT NULL, registered_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS forecast_models (
        version TEXT PRIMARY KEY, definition_json TEXT NOT NULL,
        definition_sha256 TEXT NOT NULL, registered_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS forecast_cases (
        id TEXT PRIMARY KEY,
        protocol_version TEXT NOT NULL REFERENCES forecast_protocols(version),
        symbol TEXT NOT NULL, origin_session TEXT NOT NULL, target_session TEXT NOT NULL,
        calendar TEXT NOT NULL, horizon_sessions INTEGER NOT NULL,
        generated_at TEXT NOT NULL, recorded_at TEXT NOT NULL,
        origin_price REAL NOT NULL, input_json TEXT NOT NULL, input_sha256 TEXT NOT NULL,
        input_verified INTEGER NOT NULL CHECK (input_verified IN (0, 1)),
        model_versions_json TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_forecast_cases_target
        ON forecast_cases(calendar, target_session, symbol)""",
    """CREATE INDEX IF NOT EXISTS idx_forecast_cases_origin
        ON forecast_cases(protocol_version, origin_session, symbol, recorded_at)""",
    """CREATE TABLE IF NOT EXISTS forecast_predictions (
        id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES forecast_cases(id),
        model_version TEXT NOT NULL REFERENCES forecast_models(version),
        kind TEXT NOT NULL CHECK (kind IN ('point', 'direction')),
        target_mid REAL, target_low REAL, target_high REAL, confidence REAL,
        direction INTEGER NOT NULL CHECK (direction IN (-1, 0, 1)),
        payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
        UNIQUE (case_id, model_version)
    )""",
    """CREATE TABLE IF NOT EXISTS forecast_outcomes (
        id TEXT PRIMARY KEY, symbol TEXT NOT NULL, session TEXT NOT NULL,
        calendar TEXT NOT NULL, close REAL NOT NULL,
        observed_at TEXT NOT NULL, quote_timestamp TEXT NOT NULL,
        provenance_json TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_forecast_outcomes_session
        ON forecast_outcomes(calendar, session, symbol, observed_at)""",
    """CREATE TABLE IF NOT EXISTS forecast_scores (
        prediction_id TEXT PRIMARY KEY REFERENCES forecast_predictions(id),
        outcome_id TEXT NOT NULL REFERENCES forecast_outcomes(id),
        rule_version TEXT NOT NULL, scored_at TEXT NOT NULL,
        abs_error_pct REAL, direction_correct INTEGER NOT NULL,
        band_hit INTEGER, brier_score REAL
    )""",
)


def schema_statements(backend="sqlite"):
    """Block mutation at the database boundary, including direct SQL writes."""
    if backend == "postgresql":
        function = """CREATE OR REPLACE FUNCTION markethelm_forecasts_append_only()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'forecast ledger is append-only'; END; $$"""
        triggers = tuple(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION markethelm_forecasts_append_only()"
            for table in TABLES
        )
        return (*TABLE_DDL, function, *triggers)
    triggers = tuple(
        f"CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()} "
        f"BEFORE {operation} ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'forecast ledger is append-only'); END"
        for table in TABLES
        for operation in ("UPDATE", "DELETE")
    )
    return (*TABLE_DDL, *triggers)


def ensure_sidecar_schema(conn):
    if getattr(conn, "backend", "sqlite") == "sqlite":
        for statement in schema_statements():
            conn.execute(statement)


def schema_exists(conn):
    if getattr(conn, "backend", "sqlite") == "postgresql":
        return True
    return (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'forecast_protocols'"
        ).fetchone()
        is not None
    )
