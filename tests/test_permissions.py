import time

import pytest

from rea_bot.db import Store, UserError


def first(db, gid=1):
    return db.one("SELECT id FROM lessons WHERE group_id=? ORDER BY id", (gid,))["id"]


@pytest.mark.parametrize(
    "uid,level,allowed",
    [
        (1, "manage", True),
        (2, "manage", True),
        (4, "read", True),
        (4, "write", False),
        (6, "write", True),
        (6, "manage", False),
        (3, "read", False),
        (5, "read", False),
        (7, "read", False),
    ],
)
def test_roles(store, uid, level, allowed):
    if allowed:
        store.require(uid, 1, level)
    else:
        with pytest.raises(UserError):
            store.require(uid, 1, level)


def test_foreign_homework_and_attachment_cannot_be_read(store):
    hid = store.save_homework(2, first(store), "Secret A", "file-A", "document")
    for uid in (3, 5, 7):
        with pytest.raises(UserError):
            store.homework_item(uid, hid)
        with pytest.raises(UserError):
            store.homework(uid, first(store))
        with pytest.raises(UserError):
            store.week_lessons(uid, 1, "2026-11-02", "2026-11-08")
    assert store.homework_item(4, hid)["file_id"] == "file-A"
    assert store.homework_item(1, hid)["body"] == "Secret A"


def test_editor_cannot_add_people_or_appoint_head(store):
    with pytest.raises(UserError):
        store.set_member(6, 1, 7, "add")
    with pytest.raises(UserError):
        store.appoint_head(2, 7)
    with pytest.raises(UserError):
        store.revoke_head(2, 3)
    with pytest.raises(UserError):
        store.heads(2)


def test_one_group_and_one_head(store):
    with pytest.raises(UserError):
        store.set_member(2, 1, 5, "add")
    store.appoint_head(1, 7)
    with pytest.raises(UserError):
        store.claim(7, 1)
    assert store.membership(7) is None
    with pytest.raises(UserError):
        store.claim(2, 2)


def test_head_can_be_replaced(store):
    store.revoke_head(1, 2)
    assert store.membership(2)["role"] == "student"
    store.appoint_head(1, 4)
    assert store.membership(4)["role"] == "head"
    with pytest.raises(UserError):
        store.set_member(2, 1, 7, "add")


def test_cannot_demote_head_or_superadmin(store):
    with pytest.raises(UserError):
        store.set_member(2, 1, 2, "remove")
    with pytest.raises(UserError):
        store.set_member(2, 1, 1, "add")
    store.set_member(1, 1, 2, "remove")
    assert store.membership(2) is None
    assert not store.user(2)["head_allowed"]


def test_revocation_rechecked_for_existing_lesson_and_attachment(store):
    lid = first(store)
    hid = store.save_homework(6, lid, "test")
    store.set_member(2, 1, 6, "student")
    with pytest.raises(UserError):
        store.save_homework(6, lid, "after revocation")
    with pytest.raises(UserError):
        store.delete_homework(6, hid, 1)
    store.set_member(2, 1, 6, "remove")
    with pytest.raises(UserError):
        store.homework_item(6, hid)


def test_username_change_does_not_transfer_membership(store):
    store.register(4, "new_username", "Alice")
    store.register(7, "student_a", "Other account")
    assert store.resolve("@student_a")["id"] == 7
    assert store.membership(4)["group_id"] == 1
    assert store.membership(7) is None
    store.register(7, "new_username", "Other account")
    assert store.user(4)["username"] is None
    assert store.membership(4)["group_id"] == 1


def test_username_must_be_known_and_recent(store):
    with pytest.raises(UserError):
        store.resolve("@not_registered")
    store.conn.execute("UPDATE users SET seen=? WHERE id=4", (time.time() - 90000,))
    with pytest.raises(UserError):
        store.resolve("@student_a")
    assert store.resolve("4")["id"] == 4


def test_tokens_bound_to_user_and_expire(store):
    token = store.token(2, "lesson", lid=first(store))
    assert len(token.encode()) <= 64
    with pytest.raises(UserError):
        store.token_get(5, token)
    store.conn.execute("UPDATE ui_tokens SET expires=0")
    with pytest.raises(UserError):
        store.token_get(2, token)


def test_optimistic_edit_and_delete(store):
    lid = first(store)
    hid = store.save_homework(2, lid, "v1")
    store.save_homework(6, lid, "v2", hid=hid, revision=1)
    with pytest.raises(UserError):
        store.save_homework(2, lid, "lost update", hid=hid, revision=1)
    with pytest.raises(UserError):
        store.delete_homework(2, hid, 1)
    assert store.homework_item(4, hid)["body"] == "v2"
    store.delete_homework(2, hid, 2)
    with pytest.raises(UserError):
        store.homework_item(4, hid)
    assert store.one("SELECT payload FROM audit WHERE action='delete_homework'")


def test_duplicate_confirmations_and_cancel(store):
    lid = first(store)
    payload = {
        "lid": lid,
        "body": "hello",
        "file_id": None,
        "file_kind": None,
        "hid": None,
        "revision": None,
    }
    token = store.token(2, "save", **payload)
    store.save_homework(2, **payload, token=token)
    with pytest.raises(UserError):
        store.save_homework(2, **payload, token=token)
    assert len(store.homework(2, lid)) == 1
    token = store.token(2, "save", **payload)
    store.session_clear(2)
    with pytest.raises(UserError):
        store.token_get(2, token)


def test_restart_preserves_data(tmp_path):
    path = str(tmp_path / "db.sqlite3")
    db = Store(path, {1})
    db.register(1, "owner", "Owner")
    db.cache_put("test", {"ok": True})
    db.session_set(1, {"state": "person"})
    db.close()
    db = Store(path, {1})
    assert db.user(1)["username"] == "owner"
    assert db.cache_get("test")["value"] == {"ok": True}
    assert db.session_get(1)["state"] == "person"
    db.close()


def test_screen_history_persists_and_is_user_bound(tmp_path):
    path = str(tmp_path / "screens.sqlite3")
    db = Store(path)
    current = {"action": "week", "gid": 1, "monday": "2026-11-02", "scope": "hw", "page": 2}
    db.screen_put(7, 99, current, [{"action": "home"}])
    db.close()
    db = Store(path)
    assert db.screen_get(7, 99) == {"current": current, "history": [{"action": "home"}]}
    assert db.screen_get(8, 99) is None
    db.close()


def test_remove_lookup_uses_existing_membership_without_freshness(store):
    store.conn.execute("UPDATE users SET seen=0 WHERE id=4")
    assert store.resolve_group_member(2, 1, "@student_a")["id"] == 4
    with pytest.raises(UserError):
        store.resolve_group_member(3, 1, "@student_a")
    with pytest.raises(UserError):
        store.resolve_group_member(2, 1, "@student_b")
    store.set_member(2, 1, 4, "remove")
    with pytest.raises(UserError):
        store.resolve_group_member(2, 1, "4")
