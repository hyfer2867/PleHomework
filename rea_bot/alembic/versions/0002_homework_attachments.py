"""Convert legacy single-file homework into attachment lists."""

import json

import sqlalchemy as sa
from alembic import op

revision = "0002_homework_attachments"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    columns = {column["name"] for column in sa.inspect(connection).get_columns("homework")}
    if "attachments" not in columns:
        op.add_column("homework", sa.Column("attachments", sa.Text(), nullable=True))
    # Preserve existing lists, including an explicit empty list. Keep old columns
    # as compatibility fields, and leave IDs, revisions and timestamps unchanged.
    rows = connection.exec_driver_sql(
        "SELECT id,file_id,file_kind FROM homework WHERE attachments IS NULL"
    ).fetchall()
    if rows:
        connection.exec_driver_sql(
            "UPDATE homework SET attachments=? WHERE id=?",
            [
                (json.dumps([{"file_id": row[1], "file_kind": row[2]}] if row[1] else []), row[0])
                for row in rows
            ],
        )


def downgrade():
    raise RuntimeError("Attachment lists cannot be reduced without data loss. Restore a backup.")
