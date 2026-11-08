from pathlib import Path

import pytest

from rea_bot.config import FORESIGHT
from rea_bot.db import Store
from rea_bot.schedule import parse_week

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def store():
    db = Store(":memory:", {1})
    for uid, name in [
        (1, "admin"),
        (2, "head_a"),
        (3, "head_b"),
        (4, "student_a"),
        (5, "student_b"),
        (6, "editor_a"),
        (7, "outsider"),
    ]:
        db.register(uid, name, name)
    a = db.catalog_group(1, "Group A", FORESIGHT, "1-й курс", "Бакалавр")["id"]
    b = db.catalog_group(1, "Group B", FORESIGHT, "1-й курс", "Бакалавр")["id"]
    db.appoint_head(1, 2)
    db.claim(2, a)
    db.appoint_head(1, 3)
    db.claim(3, b)
    db.set_member(2, a, 4, "add")
    db.set_member(3, b, 5, "add")
    db.set_member(2, a, 6, "add")
    db.set_member(2, a, 6, "editor")
    week = parse_week((FIXTURES / "schedule.html").read_text())
    db.sync_lessons(2, a, week)
    db.sync_lessons(3, b, week)
    yield db
    db.close()
