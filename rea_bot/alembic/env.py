"""Alembic environment shared by container startup, migrate-db and the Alembic CLI."""

from pathlib import Path

from alembic import context

from rea_bot.config import Settings
from rea_bot.migrations import finish_upgrade, open_database, prepare_upgrade

config = context.config


def run_migrations(connection):
    previous, backup = prepare_upgrade(connection, config, context.get_revision_argument())
    applied = []

    def record_revision(ctx, step, heads, run_args):
        applied.append(step.up_revision_id)

    context.configure(
        connection=connection,
        target_metadata=None,
        transactional_ddl=True,
        on_version_apply=record_revision,
    )
    with context.begin_transaction():
        context.run_migrations()
    finish_upgrade(connection, config, previous, backup, applied)


if context.is_offline_mode():
    raise RuntimeError(
        "These migrations inspect and back up live SQLite data; use online upgrade head."
    )
elif config.attributes.get("connection") is not None:
    run_migrations(config.attributes["connection"])
else:
    path = (
        context.get_x_argument(as_dictionary=True).get("database") or Settings.from_env().database
    )
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    engine = open_database(path)
    try:
        with engine.begin() as connection:
            run_migrations(connection)
    finally:
        engine.dispose()
