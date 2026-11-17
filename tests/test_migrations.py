"""Upgrade real GitHub-era schema, including WAL data and interrupted upgrades."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from rea_bot import migrations
from rea_bot.db import Store, UserError, homework_attachments

LEGACY_SCHEMA = (Path(__file__).parent / "fixtures/legacy_schema.sql").read_text()


@pytest.fixture
def legacy(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path, isolation_level=None)
    connection.executescript(LEGACY_SCHEMA)
    connection.execute("INSERT INTO groups VALUES(8,'Group A','F','1','B')")
    connection.execute("INSERT INTO groups VALUES(9,'Group B','F','1','B')")
    connection.executemany(
        "INSERT INTO users VALUES(?,?,?,?,?)",
        [
            (10, "head_a", "Head", 1.0, 1),
            (11, "editor_a", "Editor", 1.0, 0),
            (12, "student_a", "Student", 1.0, 0),
            (13, "outsider", "Other group", 1.0, 0),
        ],
    )
    connection.executemany(
        "INSERT INTO members VALUES(?,?,?)",
        [(10, 8, "head"), (11, 8, "editor"), (12, 8, "student"), (13, 9, "student")],
    )
    connection.execute(
        "INSERT INTO lessons VALUES(40,8,'source-1','2026-11-02',1,'09:00','10:30',"
        "'Математика','Практика','Аудитория 528',1)"
    )
    connection.executemany(
        "INSERT INTO homework VALUES(?,40,?,?,?,?,10,100,200,3,?)",
        [
            (51, "Документ и описание", "file-document", "document", 10, 0),
            (52, "Фотография", "file-photo", "photo", 10, 0),
            (53, "Только текст", None, None, 11, 0),
            (54, "Удалённое задание", "deleted-file", "document", 10, 1),
        ],
    )
    connection.execute("INSERT INTO audit VALUES(77,10,'edit_homework','51','{}',200)")
    connection.execute("INSERT INTO cache VALUES('week:8:2026-11-02','{\"lessons\":[]}',200)")
    connection.execute("INSERT INTO pending_members VALUES('future_student',8,10,100,9999999999)")
    draft = {
        "state": "write",
        "lid": 40,
        "body": "Черновик",
        "file_id": "draft-file",
        "file_kind": "document",
    }
    connection.execute("INSERT INTO sessions VALUES(10,?,9999999999)", (json.dumps(draft),))
    connection.execute(
        "INSERT INTO ui_tokens VALUES('legacy',10,'{\"action\":\"home\"}',9999999999)"
    )
    connection.execute("INSERT INTO ui_screens VALUES(10,99,'{}',200)")
    connection.execute("INSERT INTO document_cache VALUES('photo','document')")
    yield path, connection
    connection.close()


def snapshot(connection):
    return {
        row[0]: connection.execute(f'SELECT * FROM "{row[0]}" ORDER BY rowid').fetchall()
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def test_legacy_upgrade_preserves_every_record_and_access(legacy):
    path, connection = legacy
    before = snapshot(connection)
    with closing(Store(str(path), {1})) as db:
        assert db.migration.previous_version == "base"
        assert db.migration.version == migrations.LATEST_VERSION
        for table, rows in before.items():
            if table == "homework":
                assert [
                    tuple(row)[:-1] for row in db.conn.execute("SELECT * FROM homework")
                ] == rows
            else:
                assert [tuple(row) for row in db.conn.execute(f'SELECT * FROM "{table}"')] == rows
        assert homework_attachments(db.homework_item(12, 51)) == [
            {"file_id": "file-document", "file_kind": "document"}
        ]
        assert homework_attachments(db.homework_item(12, 52)) == [
            {"file_id": "file-photo", "file_kind": "photo"}
        ]
        assert homework_attachments(db.homework_item(12, 53)) == []
        assert len(db.homework(12, 40)) == 3
        with pytest.raises(UserError):
            db.homework_item(13, 51)
        assert db.role(10, 8) == "head"
        assert db.role(11, 8) == "editor"
        assert db.role(12, 8) == "student"
        db.save_homework(
            11,
            40,
            "Новые файлы",
            attachments=[
                {"file_id": "file-1", "file_kind": "photo"},
                {"file_id": "file-2", "file_kind": "document", "name": "Задачи.pdf"},
            ],
        )
        assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
        backup = db.migration.backup
    assert backup.is_file()
    with closing(sqlite3.connect(backup)) as saved:
        # Includes records still held in the source's WAL, not just its main .sqlite3 file.
        assert snapshot(saved) == before
    with closing(Store(str(path))) as reopened:
        assert reopened.migration.applied == ()
        assert reopened.migration.backup is None
    assert len(list((path.parent / "backups").glob("*.sqlite3"))) == 1


def test_adopt_current_unversioned_schema_preserves_multiple_files_and_materials(legacy):
    path, connection = legacy
    connection.execute("ALTER TABLE homework ADD COLUMN attachments TEXT")
    files = json.dumps(
        [
            {"file_id": "first", "file_kind": "photo", "name": "Фото"},
            {"file_id": "second", "file_kind": "document", "name": "Задачи.pdf"},
        ]
    )
    connection.execute("UPDATE homework SET attachments=? WHERE id=51", (files,))
    # Intermediate version already has course materials, but no soft deletion column.
    connection.execute("""CREATE TABLE subject_materials (
        id INTEGER PRIMARY KEY, group_id INTEGER NOT NULL REFERENCES groups(id),
        subject_key TEXT NOT NULL, file_id TEXT NOT NULL, name TEXT NOT NULL,
        file_kind TEXT NOT NULL, UNIQUE(group_id,subject_key,file_id))""")
    connection.execute(
        "INSERT INTO subject_materials VALUES(70,8,'математика','material','Книга','document')"
    )
    with closing(Store(str(path))) as db:
        assert db.one("SELECT attachments FROM homework WHERE id=51")["attachments"] == files
        assert db.one("SELECT * FROM subject_materials WHERE id=70")["deleted"] == 0
        assert db.one("SELECT * FROM subject_materials WHERE id=70")["file_id"] == "material"


def test_current_soft_deleted_material_stays_deleted(legacy):
    path, connection = legacy
    connection.execute("""CREATE TABLE subject_materials (
        id INTEGER PRIMARY KEY, group_id INTEGER NOT NULL REFERENCES groups(id),
        subject_key TEXT NOT NULL, file_id TEXT NOT NULL, name TEXT NOT NULL,
        file_kind TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
        UNIQUE(group_id,subject_key,file_id))""")
    connection.execute(
        "INSERT INTO subject_materials VALUES(70,8,'математика','material','Книга','document',1)"
    )
    with closing(Store(str(path))) as db:
        assert db.one("SELECT deleted FROM subject_materials WHERE id=70")["deleted"] == 1


def test_failure_rolls_back_schema_data_and_versions_and_can_retry(legacy, monkeypatch):
    path, connection = legacy
    before = snapshot(connection)
    schema = connection.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()

    from alembic.runtime.migration import MigrationContext

    original = MigrationContext.run_migrations

    def fail(context, **kwargs):
        original(context, **kwargs)
        context.connection.exec_driver_sql("UPDATE homework SET body='lost' WHERE id=51")
        context.connection.exec_driver_sql("CREATE TABLE incomplete(id INTEGER)")
        raise RuntimeError("interrupted")

    monkeypatch.setattr(MigrationContext, "run_migrations", fail)
    with pytest.raises(RuntimeError, match="interrupted"):
        Store(str(path))
    assert snapshot(connection) == before
    assert connection.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall() == schema
    monkeypatch.setattr(MigrationContext, "run_migrations", original)
    with closing(Store(str(path))) as db:
        assert db.migration.version == migrations.LATEST_VERSION


def test_backup_failure_aborts_before_changes(legacy, monkeypatch):
    path, connection = legacy
    before = snapshot(connection)

    def fail(conn, target):
        raise OSError("no space")

    monkeypatch.setattr(migrations, "backup_database", fail)
    with pytest.raises(OSError, match="no space"):
        Store(str(path))
    assert snapshot(connection) == before


def test_reject_future_schema_without_changes(legacy):
    path, connection = legacy
    connection.execute(
        "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT, applied_at REAL)"
    )
    connection.execute("INSERT INTO schema_migrations VALUES(999,'future',1)")
    before = snapshot(connection)
    with pytest.raises(migrations.MigrationError, match="более новая"):
        Store(str(path))
    assert snapshot(connection) == before
    assert not (path.parent / "backups").exists()


def test_fresh_database_and_repeat_start_do_not_need_backup(tmp_path):
    path = tmp_path / "new.sqlite3"
    with closing(Store(str(path))) as db:
        assert db.migration.version == migrations.LATEST_VERSION
        assert db.migration.backup is None
    with closing(Store(str(path))) as db:
        assert db.migration.applied == ()
    assert not (path.parent / "backups").exists()


def test_migrate_cli_uses_explicit_database_without_starting_bot(legacy, monkeypatch, capsys):
    from rea_bot.__main__ import main

    path, _connection = legacy
    monkeypatch.setattr("sys.argv", ["rea-bot", "migrate-db", "--database", str(path)])
    main()
    output = capsys.readouterr().out
    assert f"base → {migrations.LATEST_VERSION}" in output
    assert "Резервная копия:" in output


def test_explicit_empty_attachment_list_is_not_replaced_by_legacy_field(legacy):
    path, connection = legacy
    connection.execute("ALTER TABLE homework ADD COLUMN attachments TEXT")
    connection.execute("UPDATE homework SET attachments='[]' WHERE id=51")
    with closing(Store(str(path))) as db:
        assert homework_attachments(db.one("SELECT * FROM homework WHERE id=51")) == []


def test_adopt_pre_alembic_history_without_losing_it(legacy):
    path, connection = legacy
    connection.execute(
        "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT, applied_at REAL)"
    )
    connection.execute("INSERT INTO schema_migrations VALUES(1,'baseline',1)")
    with closing(Store(str(path))) as db:
        assert db.migration.previous_version == "base"
        assert db.migration.applied == (
            "0001_initial",
            "0002_homework_attachments",
            "0003_course_materials",
        )
        assert db.one("SELECT applied_at FROM schema_migrations WHERE version=1")["applied_at"] == 1


def test_broken_foreign_keys_roll_back_schema_and_data(legacy):
    path, connection = legacy
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("UPDATE homework SET author=99999 WHERE id=51")
    before = snapshot(connection)
    with pytest.raises(migrations.MigrationError, match="связи данных"):
        Store(str(path))
    assert snapshot(connection) == before
    assert "attachments" not in {
        row[1] for row in connection.execute("PRAGMA table_info(homework)")
    }


def test_migrate_cli_does_not_create_empty_database_on_wrong_path(tmp_path, monkeypatch):
    from rea_bot.__main__ import main

    path = tmp_path / "missing.sqlite3"
    monkeypatch.setattr("sys.argv", ["rea-bot", "migrate-db", "--database", str(path)])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert not path.exists()


def test_alembic_revision_is_persisted_and_only_pending_revisions_run(legacy):
    from alembic import command

    path, connection = legacy
    engine = migrations.open_database(str(path))
    try:
        with engine.begin() as bind:
            config = migrations.alembic_config()
            config.attributes["connection"] = bind
            command.upgrade(config, "0001_initial")
    finally:
        engine.dispose()
    assert (
        connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        == "0001_initial"
    )
    with closing(Store(str(path))) as db:
        assert db.migration.previous_version == "0001_initial"
        assert db.migration.applied == ("0002_homework_attachments", "0003_course_materials")
        assert (
            db.one("SELECT version_num FROM alembic_version")["version_num"]
            == migrations.LATEST_VERSION
        )


def test_unknown_alembic_revision_cannot_modify_the_database(legacy):
    path, connection = legacy
    connection.execute("CREATE TABLE alembic_version(version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
    connection.execute("INSERT INTO alembic_version VALUES('unknown_future_revision')")
    before = snapshot(connection)
    with pytest.raises(migrations.MigrationError, match="Неизвестная ревизия"):
        Store(str(path))
    assert snapshot(connection) == before
    assert not (path.parent / "backups").exists()


def test_alembic_cli_works_outside_project_directory_and_creates_backup(legacy, tmp_path):
    import subprocess
    import sys

    path, connection = legacy
    config = Path(__file__).resolve().parents[1] / "alembic.ini"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(config),
            "-x",
            f"database={path}",
            "upgrade",
            "head",
        ],
        check=False,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert (
        connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        == migrations.LATEST_VERSION
    )
    assert len(list((path.parent / "backups").glob("*.sqlite3"))) == 1


def test_container_entrypoint_migrates_before_executing_command(legacy, monkeypatch):
    from rea_bot import container_entrypoint
    from rea_bot.config import Settings

    path, connection = legacy
    monkeypatch.setattr(
        container_entrypoint.Settings, "from_env", lambda: Settings(database=str(path))
    )
    monkeypatch.setattr("sys.argv", ["container-entrypoint", "python", "-m", "rea_bot"])
    executed = []

    def execute(program, argv):
        assert (
            connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            == migrations.LATEST_VERSION
        )
        executed.append((program, argv))

    monkeypatch.setattr(container_entrypoint.os, "execvp", execute)
    container_entrypoint.main()
    assert executed == [("python", ["python", "-m", "rea_bot"])]


def test_container_entrypoint_does_not_start_bot_after_migration_error(legacy, monkeypatch):
    from rea_bot import container_entrypoint
    from rea_bot.config import Settings

    path, connection = legacy
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("UPDATE homework SET author=99999 WHERE id=51")
    before = snapshot(connection)
    monkeypatch.setattr(
        container_entrypoint.Settings, "from_env", lambda: Settings(database=str(path))
    )
    executed = []
    monkeypatch.setattr(container_entrypoint.os, "execvp", lambda *args: executed.append(args))
    with pytest.raises(migrations.MigrationError, match="связи данных"):
        container_entrypoint.main()
    assert not executed
    assert snapshot(connection) == before
