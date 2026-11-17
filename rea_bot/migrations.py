"""Alembic configuration and safe upgrades for the bot's SQLite database."""

import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import URL, create_engine, event
from sqlalchemy.pool import NullPool, StaticPool

LATEST_VERSION = "0003_course_materials"


class MigrationError(RuntimeError):
    """Database could not be upgraded safely."""


@dataclass(frozen=True)
class MigrationResult:
    previous_version: str
    version: str
    applied: tuple[str, ...] = ()
    backup: Path | None = None


def alembic_config():
    # Resolve inside the installed package: no dependence on the working directory
    # or on alembic.ini being present in the Docker image.
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).parent / "alembic").replace("%", "%%")
    )
    return config


def open_database(path):
    engine = create_engine(
        URL.create("sqlite", database=str(path)),
        poolclass=StaticPool if str(path) == ":memory:" else NullPool,
        connect_args={"timeout": 5},
    )

    @event.listens_for(engine, "connect")
    def configure(dbapi_connection, _record):
        # Explicit BEGIN below makes SQLite DDL transactional on Python 3.12/3.13
        # too, rather than letting the driver's legacy transaction mode commit DDL.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()

    @event.listens_for(engine, "begin")
    def begin(connection):
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def migration_version(connection):
    heads = MigrationContext.configure(connection).get_current_heads()
    if len(heads) > 1:
        raise MigrationError("База содержит несколько ревизий Alembic. Обновление отменено.")
    return heads[0] if heads else "base"


def validate_legacy_history(connection):
    exists = connection.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if exists:
        actual = [
            tuple(row)
            for row in connection.exec_driver_sql(
                "SELECT version,name FROM schema_migrations ORDER BY version"
            )
        ]
        expected = [(1, "baseline"), (2, "multiple_attachments"), (3, "course_materials")]
        if actual != expected[: len(actual)]:
            raise MigrationError(
                "Неизвестная или более новая версия базы. Используйте совместимую версию бота."
            )


def backup_database(connection, target):
    database = next(
        row[2] for row in connection.exec_driver_sql("PRAGMA database_list") if row[1] == "main"
    )
    if not database:
        return None
    source_path = Path(database)
    directory = source_path.parent / "backups"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    path = directory / f"{source_path.name}.pre-{target}.{stamp}.{uuid4().hex[:8]}.sqlite3"
    path.touch(mode=0o600, exist_ok=False)
    try:
        with (
            closing(sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)) as source,
            closing(sqlite3.connect(path)) as destination,
        ):
            source.backup(destination)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def prepare_upgrade(connection, config, destination):
    """Called by env.py inside one transaction, also for the standard Alembic CLI."""
    previous = migration_version(connection)
    script = ScriptDirectory.from_config(config)
    try:
        if previous != "base":
            script.get_revision(previous)
        target = script.as_revision_number(destination) if destination is not None else None
    except Exception as exc:
        raise MigrationError(
            "Неизвестная ревизия Alembic. Используйте совместимую версию бота."
        ) from exc
    validate_legacy_history(connection)
    backup = None
    if target is not None and previous != target:
        if connection.exec_driver_sql("PRAGMA quick_check").scalar() != "ok":
            raise MigrationError("Проверка целостности базы не пройдена.")
        if connection.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
        ).fetchone():
            backup = backup_database(connection, target)
    return previous, backup


def finish_upgrade(connection, config, previous, backup, applied):
    if applied and connection.exec_driver_sql("PRAGMA foreign_key_check").fetchone():
        raise MigrationError("Нарушены связи данных. Обновление базы отменено.")
    result = MigrationResult(previous, migration_version(connection), tuple(applied), backup)
    config.attributes["migration_result"] = result
    if applied:
        logging.getLogger(__name__).info(
            "Alembic migrated %s -> %s; backup: %s",
            previous,
            result.version,
            backup or "not needed",
        )


def migrate(connection):
    """Upgrade to head using an externally managed SQLAlchemy transaction."""
    config = alembic_config()
    config.attributes["connection"] = connection
    command.upgrade(config, "head")
    return config.attributes["migration_result"]
