"""SQLite storage and authorization boundary. No Telegram handler bypasses these checks."""

import json
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class UserError(Exception):
    """Safe, user-facing error."""


class Store:
    def __init__(self, path, admins=()):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.admins = frozenset(admins)
        self.conn.executescript("""
        PRAGMA foreign_keys=ON;
        PRAGMA journal_mode=WAL;
        PRAGMA busy_timeout=5000;
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
        """)

    def close(self):
        self.conn.close()

    @contextmanager
    def transaction(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def one(self, sql, args=()):
        row = self.conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    def all(self, sql, args=()):
        return [dict(row) for row in self.conn.execute(sql, args)]

    def log(self, actor, action, target, payload=None):
        self.conn.execute(
            "INSERT INTO audit(actor,action,target,payload,at) VALUES(?,?,?,?,?)",
            (actor, action, str(target), json.dumps(payload, ensure_ascii=False), time.time()),
        )

    def register(self, uid, username, name):
        # Usernames are mutable. Never inherit access from another account's username.
        username = username.lower() if username else None
        with self.transaction():
            if username:
                self.conn.execute(
                    "UPDATE users SET username=NULL WHERE username=? AND id<>?", (username, uid)
                )
            self.conn.execute(
                """INSERT INTO users(id,username,name,seen) VALUES(?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET username=excluded.username,name=excluded.name,seen=excluded.seen""",
                (uid, username, name, time.time()),
            )

    def user(self, uid):
        return self.one("SELECT * FROM users WHERE id=?", (uid,))

    def resolve(self, value):
        value = value.strip()
        if value.isdecimal():
            user = self.user(int(value))
        elif re.fullmatch(r"@?[A-Za-z0-9_]{5,32}", value):
            user = self.one("SELECT * FROM users WHERE username=?", (value.lstrip("@").lower(),))
            if user and time.time() - user["seen"] > 86400:
                raise UserError(
                    "Попросите пользователя снова отправить /start: нужно обновить его @username. Можно использовать числовой ID из /id."
                )
        else:
            raise UserError("Отправьте @username или числовой Telegram ID из команды /id.")
        if not user:
            raise UserError(
                "Пользователь ещё не зарегистрирован. Попросите его открыть бота и нажать /start, затем повторите добавление."
            )
        return user

    def is_admin(self, uid):
        return uid in self.admins

    def require_admin(self, uid):
        if not self.is_admin(uid):
            raise UserError("Недостаточно прав для этого действия.")

    def membership(self, uid):
        return self.one(
            "SELECT m.*,g.name FROM members m JOIN groups g ON g.id=m.group_id WHERE user_id=?",
            (uid,),
        )

    def role(self, uid, gid):
        if self.is_admin(uid):
            return "superadmin"
        member = self.membership(uid)
        return member["role"] if member and member["group_id"] == gid else None

    def require(self, uid, gid, level="read"):
        if not self.group(gid):
            raise UserError("Группа не найдена.")
        roles = {
            "read": {"student", "editor", "head", "superadmin"},
            "write": {"editor", "head", "superadmin"},
            "manage": {"head", "superadmin"},
        }
        if self.role(uid, gid) not in roles[level]:
            raise UserError(
                "Нет доступа. Доступ разрешён только к своей группе и в рамках выданных прав."
            )

    def can_catalog(self, uid):
        user = self.user(uid)
        return self.is_admin(uid) or bool(
            user and user["head_allowed"] and not self.membership(uid)
        )

    def require_catalog(self, uid):
        if not self.can_catalog(uid):
            raise UserError("Выбор группы сейчас недоступен.")

    def catalog_group(self, uid, name, faculty, course, level):
        self.require_catalog(uid)
        self.conn.execute(
            """INSERT INTO groups(name,faculty,course,level) VALUES(?,?,?,?)
          ON CONFLICT(name) DO UPDATE SET faculty=excluded.faculty,course=excluded.course,level=excluded.level""",
            (name, faculty, course, level),
        )
        return self.one("SELECT * FROM groups WHERE name=?", (name,))

    def group(self, gid):
        return self.one("SELECT * FROM groups WHERE id=?", (gid,))

    def visible_groups(self, uid):
        if self.is_admin(uid):
            return self.all("SELECT * FROM groups ORDER BY name")
        return self.all(
            "SELECT g.* FROM groups g JOIN members m ON m.group_id=g.id WHERE m.user_id=?", (uid,)
        )

    def appoint_head(self, actor, target):
        self.require_admin(actor)
        if target in self.admins:
            raise UserError("У этого пользователя уже есть все необходимые права.")
        if not self.user(target):
            raise UserError("Пользователь не найден.")
        with self.transaction():
            member = self.membership(target)
            if member and member["role"] != "head":
                existing = self.one(
                    "SELECT * FROM members WHERE group_id=? AND role='head'", (member["group_id"],)
                )
                if existing:
                    raise UserError(
                        "В этой группе уже есть староста. Сначала снимите его полномочия."
                    )
                self.conn.execute("UPDATE members SET role='head' WHERE user_id=?", (target,))
            self.conn.execute("UPDATE users SET head_allowed=1 WHERE id=?", (target,))
            self.log(actor, "appoint_head", target)

    def revoke_head(self, actor, target):
        self.require_admin(actor)
        with self.transaction():
            self.conn.execute("UPDATE users SET head_allowed=0 WHERE id=?", (target,))
            self.conn.execute(
                "UPDATE members SET role='student' WHERE user_id=? AND role='head'", (target,)
            )
            self.log(actor, "revoke_head", target)

    def claim(self, uid, gid):
        with self.transaction():
            self.require_catalog(uid)
            user = self.user(uid)
            if not user or not user["head_allowed"]:
                raise UserError("Сначала необходимо получить права старосты.")
            if not self.group(gid):
                raise UserError("Группа не найдена.")
            if self.one("SELECT 1 FROM members WHERE group_id=? AND role='head'", (gid,)):
                raise UserError("У этой группы уже есть староста.")
            if self.membership(uid):
                raise UserError("Вы уже закреплены за группой.")
            self.conn.execute("INSERT INTO members VALUES(?,?,'head')", (uid, gid))
            self.log(uid, "claim_group", gid)

    def set_member(self, actor, gid, target, action):
        with self.transaction():
            self.require(actor, gid, "manage")
            if target in self.admins or target == actor:
                raise UserError("Этим действием нельзя изменить права выбранного пользователя.")
            user, member = self.user(target), self.membership(target)
            if not user:
                raise UserError("Пользователь не найден.")
            if member and member["group_id"] != gid:
                raise UserError(
                    "Пользователь уже закреплён за другой группой. Сначала необходимо удалить старую привязку."
                )
            if (
                member
                and member["role"] == "head"
                and not (self.is_admin(actor) and action == "remove")
            ):
                raise UserError("Недостаточно прав для снятия полномочий старосты.")
            if action == "add":
                if member:
                    raise UserError("Пользователь уже состоит в этой группе.")
                if user["head_allowed"]:
                    raise UserError(
                        "Это назначенный староста: он должен самостоятельно выбрать группу."
                    )
                self.conn.execute("INSERT INTO members VALUES(?,?,'student')", (target, gid))
            elif action in ("editor", "student", "remove"):
                if not member:
                    raise UserError("Сначала добавьте пользователя в эту группу.")
                if action == "remove":
                    if member["role"] == "head":
                        self.conn.execute("UPDATE users SET head_allowed=0 WHERE id=?", (target,))
                        self.conn.execute(
                            "DELETE FROM pending_members WHERE created_by=?", (target,)
                        )
                        self.log(actor, "revoke_head", target)
                    self.conn.execute("DELETE FROM members WHERE user_id=?", (target,))
                else:
                    self.conn.execute("UPDATE members SET role=? WHERE user_id=?", (action, target))
            else:
                raise UserError("Неизвестное действие.")
            self.log(actor, action, target, {"group": gid})

    def members(self, uid, gid):
        self.require(uid, gid, "manage")
        return self.all(
            """SELECT u.id,u.name,u.username,m.role FROM members m
            JOIN users u ON u.id=m.user_id WHERE group_id=? ORDER BY m.role,u.name""",
            (gid,),
        )

    def heads(self, uid):
        self.require_admin(uid)
        return self.all("""SELECT u.id,u.name,u.username,g.name AS group_name FROM users u
            LEFT JOIN members m ON m.user_id=u.id LEFT JOIN groups g ON g.id=m.group_id
            WHERE u.head_allowed=1 ORDER BY u.name""")

    def cache_get(self, key):
        row = self.one("SELECT * FROM cache WHERE key=?", (key,))
        if row:
            row["value"] = json.loads(row["value"])
        return row

    def cache_put(self, key, value):
        self.conn.execute(
            "INSERT OR REPLACE INTO cache VALUES(?,?,?)",
            (key, json.dumps(value, ensure_ascii=False), time.time()),
        )

    def sync_lessons(self, uid, gid, week):
        self.require(uid, gid)
        with self.transaction():
            self.conn.execute(
                "UPDATE lessons SET active=0 WHERE group_id=? AND day BETWEEN ? AND ?",
                (gid, week["monday"], week["sunday"]),
            )
            for item in week["lessons"]:
                self.conn.execute(
                    """INSERT INTO lessons(group_id,source_key,day,slot,start,finish,subject,kind,details)
                  VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(group_id,source_key) DO UPDATE SET
                  start=excluded.start,finish=excluded.finish,details=excluded.details,active=1""",
                    (
                        gid,
                        *(
                            item[k]
                            for k in (
                                "source_key",
                                "day",
                                "slot",
                                "start",
                                "finish",
                                "subject",
                                "kind",
                                "details",
                            )
                        ),
                    ),
                )

    def week_lessons(self, uid, gid, monday, sunday):
        self.require(uid, gid)
        return self.all(
            """SELECT l.*, (SELECT count(*) FROM homework h WHERE h.lesson_id=l.id AND h.deleted=0) AS hw_count
          FROM lessons l WHERE group_id=? AND day BETWEEN ? AND ? AND
          (active=1 OR EXISTS(SELECT 1 FROM homework h WHERE h.lesson_id=l.id AND h.deleted=0))
          ORDER BY day,slot,subject,id""",
            (gid, monday, sunday),
        )

    def lesson(self, uid, lid, level="read"):
        row = self.one("SELECT * FROM lessons WHERE id=?", (lid,))
        if not row:
            raise UserError("Пара не найдена.")
        self.require(uid, row["group_id"], level)
        return row

    def homework(self, uid, lid):
        self.lesson(uid, lid)
        return self.all(
            "SELECT * FROM homework WHERE lesson_id=? AND deleted=0 ORDER BY id", (lid,)
        )

    def homework_item(self, uid, hid, level="read"):
        row = self.one("SELECT * FROM homework WHERE id=? AND deleted=0", (hid,))
        if not row:
            raise UserError("Задание уже удалено или не найдено.")
        self.lesson(uid, row["lesson_id"], level)
        return row

    def save_homework(
        self, uid, lid, body, file_id=None, file_kind=None, hid=None, revision=None, token=None
    ):
        body = body.strip()
        if (not body and not file_id) or len(body) > 3000:
            raise UserError("Отправьте текст до 3000 символов, фотографию или документ с подписью.")
        if (file_id is None) != (file_kind is None) or file_kind not in (None, "photo", "document"):
            raise UserError("Неподдерживаемый тип вложения.")
        with self.transaction():
            if token:
                saved = self.token_get(uid, token)
                expected = {
                    "action": "save",
                    "lid": lid,
                    "body": body,
                    "file_id": file_id,
                    "file_kind": file_kind,
                    "hid": hid,
                    "revision": revision,
                }
                # Body is normalized on write, but the immutable preview must still match.
                saved["body"] = saved.get("body", "").strip()
                if saved != expected:
                    raise UserError("Предпросмотр устарел. Создайте задание заново.")
            lesson = self.lesson(uid, lid, "write")
            if not lesson["active"]:
                raise UserError(
                    "Пара исчезла из расписания. Откройте актуальную пару и добавьте ДЗ туда."
                )
            now = time.time()
            if hid is not None:
                old = self.homework_item(uid, hid, "write")
                if old["lesson_id"] != lid or old["revision"] != revision:
                    raise UserError(
                        "Задание уже изменили. Откройте его заново, чтобы не затереть чужие изменения."
                    )
                self.conn.execute(
                    """UPDATE homework SET body=?,file_id=?,file_kind=?,updated_by=?,updated=?,revision=revision+1
                  WHERE id=?""",
                    (body, file_id, file_kind, uid, now, hid),
                )
                self.log(uid, "edit_homework", hid, old)
            else:
                hid = self.conn.execute(
                    """INSERT INTO homework(lesson_id,body,file_id,file_kind,author,updated_by,created,updated)
                  VALUES(?,?,?,?,?,?,?,?)""",
                    (lid, body, file_id, file_kind, uid, uid, now, now),
                ).lastrowid
                self.log(uid, "add_homework", hid, {"lesson": lid})
            if token:
                self.token_delete(token)
            return hid

    def delete_homework(self, uid, hid, revision):
        with self.transaction():
            row = self.homework_item(uid, hid, "write")
            if row["revision"] != revision:
                raise UserError("Задание изменилось. Откройте его заново.")
            self.conn.execute(
                "UPDATE homework SET deleted=1,revision=revision+1,updated_by=?,updated=? WHERE id=?",
                (uid, time.time(), hid),
            )
            self.log(uid, "delete_homework", hid, row)

    def token(self, uid, action, **payload):
        token = secrets.token_urlsafe(12)
        self.conn.execute("DELETE FROM ui_tokens WHERE expires<?", (time.time(),))
        self.conn.execute(
            "INSERT INTO ui_tokens VALUES(?,?,?,?)",
            (
                token,
                uid,
                json.dumps({"action": action, **payload}, ensure_ascii=False),
                time.time() + 86400,
            ),
        )
        return "u:" + token

    def token_get(self, uid, value):
        row = self.one(
            "SELECT * FROM ui_tokens WHERE token=? AND user_id=? AND expires>?",
            (value.removeprefix("u:"), uid, time.time()),
        )
        if not row:
            raise UserError("Кнопка устарела. Откройте /start заново.")
        return json.loads(row["payload"])

    def token_delete(self, value):
        self.conn.execute("DELETE FROM ui_tokens WHERE token=?", (value.removeprefix("u:"),))

    def session_set(self, uid, payload):
        self.conn.execute(
            "INSERT OR REPLACE INTO sessions VALUES(?,?,?)",
            (uid, json.dumps(payload, ensure_ascii=False), time.time() + 1800),
        )

    def session_get(self, uid):
        row = self.one("SELECT * FROM sessions WHERE user_id=? AND expires>?", (uid, time.time()))
        return json.loads(row["payload"]) if row else None

    def session_clear(self, uid):
        self.conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        # Cancelling input or navigating away invalidates all outstanding write/access previews.
        self.conn.execute(
            "DELETE FROM ui_tokens WHERE user_id=? AND json_extract(payload,'$.action') IN ('save','person_confirm','bulk_confirm','year_load')",
            (uid,),
        )

    def screen_get(self, uid, message_id):
        row = self.one(
            "SELECT payload FROM ui_screens WHERE user_id=? AND message_id=?", (uid, message_id)
        )
        return json.loads(row["payload"]) if row else None

    def screen_put(self, uid, message_id, current, history, media=False):
        self.conn.execute("DELETE FROM ui_screens WHERE updated<?", (time.time() - 7 * 86400,))
        self.conn.execute(
            "INSERT OR REPLACE INTO ui_screens VALUES(?,?,?,?)",
            (
                uid,
                message_id,
                json.dumps(
                    {"current": current, "history": history, **({"media": True} if media else {})},
                    ensure_ascii=False,
                ),
                time.time(),
            ),
        )

    def saved_weeks(self, uid, gid):
        self.require(uid, gid)
        return [
            {**json.loads(row["value"]), "fetched": row["fetched"]}
            for row in self.all(
                "SELECT value,fetched FROM cache WHERE key LIKE ? ORDER BY key", (f"week:{gid}:%",)
            )
        ]

    def parse_usernames(self, text):
        names = list(
            dict.fromkeys(
                part.lstrip("@").lower() for part in re.split(r"[\s,;]+", text.strip()) if part
            )
        )
        if (
            not names
            or len(names) > 100
            or any(not re.fullmatch(r"[a-zA-Z0-9_]{5,32}", name) for name in names)
        ):
            raise UserError(
                "Отправьте от 1 до 100 @username через пробел, запятую или с новой строки."
            )
        return names

    def prepare_members(self, actor, gid, names):
        self.require(actor, gid, "manage")
        plan = []
        for name in names:
            user = self.one("SELECT * FROM users WHERE username=?", (name,))
            pending = self.one(
                "SELECT * FROM pending_members WHERE username=? AND expires>?", (name, time.time())
            )
            if pending and pending["group_id"] != gid:
                raise UserError(f"@{name} уже ожидает вступления в другую группу.")
            if user:
                if time.time() - user["seen"] > 86400:
                    raise UserError(
                        f"@{name}: попросите пользователя обновить /start или добавьте его по числовому ID."
                    )
                member = self.membership(user["id"])
                if member and member["group_id"] != gid:
                    raise UserError(f"@{name} уже состоит в другой группе.")
                if user["id"] in self.admins or user["head_allowed"]:
                    raise UserError(
                        f"@{name}: этот пользователь не может быть добавлен как студент."
                    )
            plan.append({"username": name, "uid": user["id"] if user else None})
        return plan

    def add_prepared_members(self, actor, gid, plan):
        with self.transaction():
            self.require(actor, gid, "manage")
            current = self.prepare_members(actor, gid, [item["username"] for item in plan])
            if current != plan:
                raise UserError(
                    "Данные пользователей изменились. Отправьте список заново для проверки."
                )
            added = waiting = 0
            for item in plan:
                uid, name = item["uid"], item["username"]
                if uid is not None:
                    if not self.membership(uid):
                        self.conn.execute("INSERT INTO members VALUES(?,?,'student')", (uid, gid))
                        added += 1
                    self.conn.execute("DELETE FROM pending_members WHERE username=?", (name,))
                else:
                    existing = self.one(
                        "SELECT * FROM pending_members WHERE username=? AND expires>?",
                        (name, time.time()),
                    )
                    if not existing:
                        now = time.time()
                        self.conn.execute(
                            "INSERT OR REPLACE INTO pending_members VALUES(?,?,?,?,?)",
                            (name, gid, actor, now, now + 30 * 86400),
                        )
                    waiting += 1
            self.log(actor, "prepare_members", gid, plan)
            return added, waiting

    def activate_pending(self, uid):
        with self.transaction():
            user = self.user(uid)
            if (
                not user
                or not user["username"]
                or self.membership(uid)
                or uid in self.admins
                or user["head_allowed"]
            ):
                return False
            pending = self.one(
                "SELECT * FROM pending_members WHERE username=? AND expires>?",
                (user["username"], time.time()),
            )
            if not pending:
                return False
            try:
                self.require(pending["created_by"], pending["group_id"], "manage")
            except UserError:
                self.conn.execute(
                    "DELETE FROM pending_members WHERE username=?", (user["username"],)
                )
                return False
            self.conn.execute(
                "INSERT INTO members VALUES(?,?,'student')", (uid, pending["group_id"])
            )
            self.conn.execute("DELETE FROM pending_members WHERE username=?", (user["username"],))
            self.log(
                uid, "activate_membership", pending["group_id"], {"username": user["username"]}
            )
            return True

    def pending_members(self, uid, gid):
        self.require(uid, gid, "manage")
        return self.all(
            "SELECT * FROM pending_members WHERE group_id=? AND expires>? ORDER BY username",
            (gid, time.time()),
        )

    def cancel_pending(self, uid, gid, username):
        with self.transaction():
            self.require(uid, gid, "manage")
            self.conn.execute(
                "DELETE FROM pending_members WHERE username=? AND group_id=?", (username, gid)
            )
            self.log(uid, "cancel_pending", username, {"group": gid})

    def resolve_group_member(self, actor, gid, value):
        self.require(actor, gid, "manage")
        value = value.strip().lstrip("@")
        for member in self.members(actor, gid):
            if (value.isdecimal() and member["id"] == int(value)) or (
                member["username"] and member["username"].lower() == value.lower()
            ):
                return member
        raise UserError(
            "Пользователь не найден среди участников этой группы. Выберите его из списка."
        )
