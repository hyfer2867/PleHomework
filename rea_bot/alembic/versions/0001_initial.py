"""Adopt the original SQLite schema without replacing existing records."""

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

BASE_SCHEMA = """
        CREATE TABLE IF NOT EXISTS groups (
          id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
          faculty TEXT NOT NULL, course TEXT NOT NULL, level TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY, username TEXT UNIQUE COLLATE NOCASE,
          name TEXT NOT NULL, seen REAL NOT NULL, head_allowed INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS members (
          user_id INTEGER PRIMARY KEY REFERENCES users(id),
          group_id INTEGER NOT NULL REFERENCES groups(id),
          role TEXT NOT NULL CHECK(role IN ('student','editor','head')));
        CREATE UNIQUE INDEX IF NOT EXISTS one_head_per_group ON members(group_id) WHERE role='head';
        CREATE TABLE IF NOT EXISTS lessons (
          id INTEGER PRIMARY KEY, group_id INTEGER NOT NULL REFERENCES groups(id),
          source_key TEXT NOT NULL, day TEXT NOT NULL, slot INTEGER NOT NULL,
          start TEXT NOT NULL, finish TEXT NOT NULL, subject TEXT NOT NULL,
          kind TEXT NOT NULL, details TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
          UNIQUE(group_id,source_key));
        CREATE INDEX IF NOT EXISTS lesson_days ON lessons(group_id,day);
        CREATE TABLE IF NOT EXISTS homework (
          id INTEGER PRIMARY KEY, lesson_id INTEGER NOT NULL REFERENCES lessons(id),
          body TEXT NOT NULL, file_id TEXT, file_kind TEXT,
          author INTEGER NOT NULL REFERENCES users(id), updated_by INTEGER NOT NULL REFERENCES users(id),
          created REAL NOT NULL, updated REAL NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
          deleted INTEGER NOT NULL DEFAULT 0,
          CHECK(file_kind IS NULL OR file_kind IN ('photo','document')));
        CREATE TABLE IF NOT EXISTS audit (
          id INTEGER PRIMARY KEY, actor INTEGER NOT NULL, action TEXT NOT NULL,
          target TEXT NOT NULL, payload TEXT NOT NULL, at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS cache (
          key TEXT PRIMARY KEY, value TEXT NOT NULL, fetched REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS ui_tokens (
          token TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
          payload TEXT NOT NULL, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS document_cache (
          source_id TEXT PRIMARY KEY, document_id TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ui_screens (
          user_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
          payload TEXT NOT NULL, updated REAL NOT NULL,
          PRIMARY KEY(user_id,message_id));
        CREATE TABLE IF NOT EXISTS pending_members (
          username TEXT PRIMARY KEY COLLATE NOCASE, group_id INTEGER NOT NULL REFERENCES groups(id),
          created_by INTEGER NOT NULL REFERENCES users(id), created REAL NOT NULL, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions (
          user_id INTEGER PRIMARY KEY, payload TEXT NOT NULL, expires REAL NOT NULL);
        
"""


def upgrade():
    for statement in BASE_SCHEMA.split(";"):
        if statement.strip():
            op.get_bind().exec_driver_sql(statement)


def downgrade():
    raise RuntimeError("Destructive downgrades are disabled. Restore a database backup instead.")
