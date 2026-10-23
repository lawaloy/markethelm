"""Unit tests for SQLite URL resolution and database enablement."""

from pathlib import Path

import pytest

from src.storage.database import (
    _MIGRATIONS,
    LATEST_SCHEMA_VERSION,
    POSTGRES_WRITE_MUTEX_KEY,
    MigrationError,
    _connect_postgresql,
    _migration_statements,
    _PostgresConnection,
    apply_migrations,
    database_backend,
    database_enabled,
    default_database_path,
    get_connection,
    init_database,
    resolve_database_path,
)


class TestDatabaseEnabled:
    def test_disabled_when_unset_or_blank(self, monkeypatch):
        monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
        assert database_enabled() is False

        monkeypatch.setenv("MARKETHELM_DATABASE_URL", "   ")
        assert database_enabled() is False

    def test_enabled_when_url_present(self, monkeypatch):
        monkeypatch.setenv("MARKETHELM_DATABASE_URL", "sqlite:////tmp/markethelm.db")
        assert database_enabled() is True


class TestResolveDatabasePath:
    def test_missing_url_raises(self, monkeypatch):
        monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
        with pytest.raises(RuntimeError, match="MARKETHELM_DATABASE_URL is not set"):
            resolve_database_path()

    def test_relative_sqlite_url(self, monkeypatch):
        monkeypatch.setenv("MARKETHELM_DATABASE_URL", "sqlite:///relative.db")
        assert resolve_database_path() == Path("/relative.db")

    def test_suite_style_absolute_url_is_usable(self, monkeypatch, tmp_path):
        """sqlite:///{absolute} is the form used by fixtures; connection must work."""
        db = tmp_path / "markethelm.db"
        monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db.as_posix()}")
        resolved = resolve_database_path()
        assert resolved.name == "markethelm.db"
        with get_connection() as conn:
            assert conn.execute("SELECT 1").fetchone()[0] == 1

    def test_four_slash_absolute_url(self, monkeypatch):
        # urlparse keeps an extra leading slash for sqlite:////abs/path URLs.
        monkeypatch.setenv(
            "MARKETHELM_DATABASE_URL",
            "sqlite:////var/lib/markethelm/markethelm.db",
        )
        assert resolve_database_path() == Path("//var/lib/markethelm/markethelm.db")

    def test_non_sqlite_scheme_rejected(self, monkeypatch):
        monkeypatch.setenv("MARKETHELM_DATABASE_URL", "postgres://localhost/db")
        with pytest.raises(ValueError, match="Expected a sqlite URL"):
            resolve_database_path()

    def test_sqlite_url_without_path_rejected(self, monkeypatch):
        monkeypatch.setenv("MARKETHELM_DATABASE_URL", "sqlite://")
        with pytest.raises(ValueError, match="Invalid SQLite URL"):
            resolve_database_path()

    def test_sqlite_url_with_host_rejected(self, monkeypatch):
        """Hosted-looking sqlite://host/path must not silently become a local file."""
        monkeypatch.setenv(
            "MARKETHELM_DATABASE_URL",
            "sqlite://evilhost/tmp/markethelm.db",
        )
        with pytest.raises(ValueError, match="without a host"):
            resolve_database_path()

    def test_sqlite_url_with_localhost_netloc_rejected(self, monkeypatch):
        monkeypatch.setenv(
            "MARKETHELM_DATABASE_URL",
            "sqlite://localhost/tmp/markethelm.db",
        )
        with pytest.raises(ValueError, match="without a host"):
            resolve_database_path()


class TestDatabaseBackend:
    @pytest.mark.parametrize("scheme", ["postgres", "postgresql"])
    def test_postgresql_aliases(self, monkeypatch, scheme):
        monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"{scheme}://db.example/markethelm")
        assert database_backend() == "postgresql"

    def test_sqlite_backend(self, monkeypatch):
        monkeypatch.setenv("MARKETHELM_DATABASE_URL", "sqlite:///markethelm.db")
        assert database_backend() == "sqlite"

    def test_unknown_backend_rejected(self, monkeypatch):
        monkeypatch.setenv("MARKETHELM_DATABASE_URL", "mysql://localhost/db")
        with pytest.raises(ValueError, match="Unsupported database URL scheme"):
            database_backend()

    def test_postgresql_schema_uses_portable_identity_and_collation(self):
        statements = _migration_statements("postgresql", _MIGRATIONS[0])
        schema = "\n".join(statements)
        assert "BIGSERIAL PRIMARY KEY" in schema
        assert "AUTOINCREMENT" not in schema
        assert "COLLATE NOCASE" not in schema

    def test_postgresql_adapter_translates_qmark_parameters(self):
        class FakeConnection:
            def __init__(self):
                self.call = None

            def execute(self, sql, params):
                self.call = (sql, params)
                return "cursor"

        raw = FakeConnection()
        connection = _PostgresConnection(raw)
        result = connection.execute(
            "SELECT * FROM users WHERE id = ? AND email = ?",
            ("user-1", "user@example.com"),
        )
        assert result == "cursor"
        assert raw.call == (
            "SELECT * FROM users WHERE id = %s AND email = %s",
            ("user-1", "user@example.com"),
        )

    def test_postgresql_begin_immediate_takes_write_mutex(self):
        """BEGIN IMMEDIATE is a writer mutex, not a transaction-start no-op.

        SELECT 1 would leave concurrent Postgres RMW sections unlocked, so
        overlapping deliver jobs could double-notify and a blank alert-config
        save could clobber a rotated webhook URL.
        """

        class FakeConnection:
            def __init__(self):
                self.sql = None

            def execute(self, sql, params):
                self.sql = sql
                return "cursor"

        raw = FakeConnection()
        _PostgresConnection(raw).execute("BEGIN IMMEDIATE")
        assert raw.sql == (f"SELECT pg_advisory_xact_lock({POSTGRES_WRITE_MUTEX_KEY})")
        assert "SELECT 1" not in raw.sql

    def test_postgresql_plain_begin_is_not_write_mutex(self):
        """Only BEGIN IMMEDIATE is the writer mutex; a plain BEGIN must stay a BEGIN."""

        class FakeConnection:
            def __init__(self):
                self.sql = None

            def execute(self, sql, params):
                self.sql = sql
                return "cursor"

        raw = FakeConnection()
        _PostgresConnection(raw).execute("BEGIN")
        assert raw.sql == "BEGIN"
        assert "pg_advisory_xact_lock" not in raw.sql

    def test_connect_postgresql_requires_psycopg(self, monkeypatch):
        """Hosted boot without psycopg must fail closed with an actionable error."""
        monkeypatch.setenv("MARKETHELM_DATABASE_URL", "postgresql://db.example/markethelm")

        monkeypatch.setattr("src.storage.database.psycopg", None)
        monkeypatch.setattr(
            "src.storage.database._psycopg_import_error",
            ImportError("simulated missing psycopg"),
        )
        with pytest.raises(RuntimeError, match="requires Psycopg"):
            _connect_postgresql()


class TestInitDatabase:
    def test_init_noop_when_disabled(self, monkeypatch):
        monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
        # Must not raise or create files when multi-user mode is off.
        init_database()

    def test_default_database_path_under_user_config(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "src.storage.database.user_config_dir",
            lambda: tmp_path,
        )
        assert default_database_path() == tmp_path / "markethelm.db"


class TestDatabaseMigrations:
    @staticmethod
    def configure_database(monkeypatch, tmp_path):
        database_path = tmp_path / "markethelm.db"
        monkeypatch.setenv(
            "MARKETHELM_DATABASE_URL",
            f"sqlite:///{database_path.as_posix()}",
        )

    def test_fresh_database_records_current_schema_version(self, monkeypatch, tmp_path):
        self.configure_database(monkeypatch, tmp_path)

        init_database()

        with get_connection() as conn:
            migration = conn.execute("""SELECT version, name, applied_at FROM schema_migrations
                   ORDER BY version DESC LIMIT 1""").fetchone()
            tables = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }

        assert migration["version"] == LATEST_SCHEMA_VERSION
        assert migration["name"] == "projections_and_summaries"
        assert migration["applied_at"]
        assert {"users", "alert_watches", "alert_jobs"}.issubset(tables)

    def test_existing_unversioned_database_is_upgraded_without_data_loss(
        self, monkeypatch, tmp_path
    ):
        self.configure_database(monkeypatch, tmp_path)
        with get_connection() as conn:
            conn.execute("""CREATE TABLE users (
                    id TEXT PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )""")
            conn.execute(
                "INSERT INTO users VALUES (?, ?, ?, ?)",
                ("user-1", "user@example.com", "hash", "2026-01-01T00:00:00Z"),
            )

        init_database()

        with get_connection() as conn:
            user = conn.execute("SELECT * FROM users WHERE id = 'user-1'").fetchone()
            versions = conn.execute("SELECT version FROM schema_migrations").fetchall()
        assert user["email"] == "user@example.com"
        assert [row["version"] for row in versions] == list(range(1, LATEST_SCHEMA_VERSION + 1))

    def test_repeated_initialization_is_idempotent(self, monkeypatch, tmp_path):
        self.configure_database(monkeypatch, tmp_path)

        init_database()
        init_database()

        with get_connection() as conn:
            count = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        assert count == LATEST_SCHEMA_VERSION

    def test_unknown_future_schema_version_fails_closed(self, monkeypatch, tmp_path):
        self.configure_database(monkeypatch, tmp_path)
        with get_connection() as conn:
            conn.execute("""CREATE TABLE schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )""")
            conn.execute(
                "INSERT INTO schema_migrations VALUES (?, ?, ?)",
                (LATEST_SCHEMA_VERSION + 1, "future", "2026-01-01T00:00:00Z"),
            )

        with pytest.raises(MigrationError, match="newer than this application"):
            init_database()

    def test_apply_migrations_takes_postgres_write_mutex(self):
        """Postgres apply_migrations must lock via BEGIN IMMEDIATE, not a no-op.

        Hosted startup used to call pg_advisory_xact_lock directly while RMW
        writers used BEGIN IMMEDIATE (rewritten to SELECT 1). Both paths now
        share the adapter mutex so schema changes cannot interleave with
        config/claim writers.
        """

        class FakeRaw:
            def __init__(self):
                self.statements = []

            def execute(self, sql, params=()):
                self.statements.append(sql)
                raise RuntimeError("stop after lock")

            def rollback(self):
                pass

        raw = FakeRaw()
        with pytest.raises(MigrationError, match="Failed to apply"):
            apply_migrations(_PostgresConnection(raw))
        assert raw.statements == [f"SELECT pg_advisory_xact_lock({POSTGRES_WRITE_MUTEX_KEY})"]
