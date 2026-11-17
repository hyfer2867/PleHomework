"""Create course materials or adopt the existing intermediate schema."""

import sqlalchemy as sa
from alembic import op

revision = "0003_course_materials"
down_revision = "0002_homework_attachments"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    connection.exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS subject_materials (
          id INTEGER PRIMARY KEY, group_id INTEGER NOT NULL REFERENCES groups(id),
          subject_key TEXT NOT NULL, file_id TEXT NOT NULL, name TEXT NOT NULL,
          file_kind TEXT NOT NULL CHECK(file_kind IN ('photo','document')),
          deleted INTEGER NOT NULL DEFAULT 0,
          UNIQUE(group_id,subject_key,file_id))
    """)
    columns = {column["name"] for column in sa.inspect(connection).get_columns("subject_materials")}
    if "deleted" not in columns:
        op.add_column(
            "subject_materials",
            sa.Column("deleted", sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade():
    raise RuntimeError("Removing course materials would lose data. Restore a database backup.")
