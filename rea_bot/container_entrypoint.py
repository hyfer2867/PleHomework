"""Upgrade the mounted database before executing the container command."""

import logging
import os
import sys
from contextlib import closing

from .config import Settings
from .db import Store


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings.from_env()
    # Any exception exits the container before polling can start. The second
    # Store opened by the application finds the recorded Alembic head and is a no-op.
    with closing(Store(settings.database)):
        pass
    command = sys.argv[1:] or [sys.executable, "-m", "rea_bot"]
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
