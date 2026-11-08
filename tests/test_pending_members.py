import time

import pytest

from rea_bot.db import UserError


def test_bulk_registration_claim_and_username_change(store):
    names = store.parse_usernames("@future_one, @FUTURE_TWO\n@future_one")
    assert names == ["future_one", "future_two"]
    plan = store.prepare_members(2, 1, names)
    assert store.add_prepared_members(2, 1, plan) == (0, 2)
    store.register(101, "future_one", "First student")
    assert store.membership(101) is None
    assert store.activate_pending(101)
    assert store.membership(101)["group_id"] == 1
    assert store.membership(101)["role"] == "student"
    assert not store.activate_pending(101)
    store.register(101, "renamed_user", "First student")
    store.register(102, "future_one", "Different account")
    assert not store.activate_pending(102)
    assert store.membership(101)["group_id"] == 1


def test_pending_conflicts_expiration_cancellation_and_revoked_creator(store):
    store.add_prepared_members(2, 1, store.prepare_members(2, 1, ["future_one"]))
    with pytest.raises(UserError):
        store.prepare_members(3, 2, ["future_one"])
    with pytest.raises(UserError):
        store.cancel_pending(3, 1, "future_one")
    store.cancel_pending(2, 1, "future_one")
    assert not store.pending_members(2, 1)
    store.add_prepared_members(2, 1, store.prepare_members(2, 1, ["future_one"]))
    store.conn.execute("UPDATE pending_members SET expires=?", (time.time() - 1,))
    store.register(101, "future_one", "First student")
    assert not store.activate_pending(101)
    store.add_prepared_members(2, 1, store.prepare_members(2, 1, ["future_two"]))
    store.revoke_head(1, 2)
    store.register(102, "future_two", "Second student")
    assert not store.activate_pending(102)


def test_bulk_is_atomic_and_cannot_move_existing_student(store):
    with pytest.raises(UserError):
        store.prepare_members(2, 1, ["future_one", "student_b"])
    assert not store.pending_members(2, 1)
    with pytest.raises(UserError):
        store.prepare_members(6, 1, ["future_one"])
    plan = store.prepare_members(2, 1, ["future_one"])
    store.register(101, "future_one", "Newly registered")
    with pytest.raises(UserError):
        store.add_prepared_members(2, 1, plan)
    assert store.membership(101) is None
    assert store.add_prepared_members(2, 1, store.prepare_members(2, 1, ["future_one"])) == (1, 0)
    assert store.add_prepared_members(2, 1, store.prepare_members(2, 1, ["future_one"])) == (0, 0)
