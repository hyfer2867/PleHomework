"""SQLite storage and authorization boundary. No Telegram handler bypasses these checks."""

import json
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .migrations import migrate, open_database

MAX_ATTACHMENTS = 10


def homework_attachments(record):
    attachments = record.get("attachments")
    if attachments is not None:
        return json.loads(attachments) if isinstance(attachments, str) else attachments
    if record.get("file_id"):
        return [{"file_id": record["file_id"], "file_kind": record["file_kind"]}]
    return []


def attachment_name(file, index):
    if file.get("name"):
        return file["name"]
    if file["file_kind"] == "photo":
        return f"Фото {index + 1}.jpg"
    return f"Файл {index + 1}"


class UserError(Exception):
    """Safe, user-facing error."""


class Store:
    def __init__(self, path, admins=()):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.admins = frozenset(admins)
        self._engine = open_database(path)
        try:
            with self._engine.begin() as connection:
                self.migration = migrate(connection)
            # The engine owns the connection lifetime. Runtime queries retain the
            # existing sqlite3 API and authorization/transaction boundaries.
            self._raw = self._engine.raw_connection()
            self.conn = self._raw.driver_connection
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL")
        except BaseException:
            self._engine.dispose()
            raise

    def close(self):
        self._raw.close()
        self._engine.dispose()

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

    @staticmethod
    def material_scope(lesson):
        return lesson["group_id"], " ".join(lesson["subject"].split()).casefold()

    def subject_materials(self, uid, lid):
        lesson = self.lesson(uid, lid)
        return self.all(
            "SELECT * FROM subject_materials WHERE group_id=? AND subject_key=? AND deleted=0 ORDER BY id DESC",
            self.material_scope(lesson),
        )

    def subject_material(self, uid, lid, material_id, level="read"):
        lesson = self.lesson(uid, lid, level)
        material = self.one(
            "SELECT * FROM subject_materials WHERE group_id=? AND subject_key=? AND id=? AND deleted=0",
            (*self.material_scope(lesson), material_id),
        )
        if not material:
            raise UserError("Файл в материалах этого предмета не найден.")
        return material

    def homework_files_for_edit(self, uid, hid, revision, level="write"):
        homework = self.homework_item(uid, hid, level)
        if homework["revision"] != revision:
            raise UserError("Список файлов изменился. Откройте его заново.")
        return homework, homework_attachments(homework)

    def save_subject_material(self, uid, hid, index, revision):
        with self.transaction():
            homework, files = self.homework_files_for_edit(uid, hid, revision, "manage")
            if not 0 <= index < len(files):
                raise UserError("Список файлов изменился. Откройте его заново.")
            lesson = self.lesson(uid, homework["lesson_id"])
            file = files[index]
            return self._save_material(
                uid, lesson, file, attachment_name(file, index), {"homework": hid}
            )

    def upload_subject_material(self, uid, lid, file):
        with self.transaction():
            lesson = self.lesson(uid, lid, "manage")
            return self._save_material(uid, lesson, file, file["name"], {"lesson": lid})

    def _save_material(self, uid, lesson, file, name, source):
        material = self.one(
            """INSERT INTO subject_materials(group_id,subject_key,file_id,name,file_kind)
            VALUES(?,?,?,?,?) ON CONFLICT(group_id,subject_key,file_id)
            DO UPDATE SET deleted=0,name=excluded.name WHERE subject_materials.deleted=1
            RETURNING id""",
            (*self.material_scope(lesson), file["file_id"], name, file["file_kind"]),
        )
        if material:
            self.log(uid, "save_subject_material", material["id"], source)
        return material is not None

    def delete_subject_material(self, uid, lid, material_id):
        with self.transaction():
            self.subject_material(uid, lid, material_id, "manage")
            self.conn.execute("UPDATE subject_materials SET deleted=1 WHERE id=?", (material_id,))
            self.log(uid, "delete_subject_material", material_id, {"lesson": lid})

    def set_subject_material_attachment(self, uid, hid, revision, material_id, detach=False):
        homework, files = self.homework_files_for_edit(uid, hid, revision, "manage")
        lid = homework["lesson_id"]
        material = self.subject_material(uid, lid, material_id)
        other_files = [file for file in files if file["file_id"] != material["file_id"]]
        already_attached = len(other_files) != len(files)
        if detach:
            if not already_attached:
                raise UserError("Этот файл уже откреплён от ДЗ.")
            if not other_files and not homework["body"].strip():
                raise UserError(
                    "Это единственный файл в ДЗ. Сначала добавьте текст или другой файл."
                )
            files = other_files
        else:
            if already_attached:
                raise UserError("Этот файл уже прикреплён к ДЗ.")
            files.append({key: material[key] for key in ("file_id", "file_kind", "name")})
        self.save_homework(
            uid, lid, homework["body"], hid=hid, revision=revision, attachments=files
        )

    def save_homework(
        self,
        uid,
        lid,
        body,
        file_id=None,
        file_kind=None,
        hid=None,
        revision=None,
        token=None,
        attachments=None,
    ):
        body = body.strip()
        if (file_id is None) != (file_kind is None) or file_kind not in (None, "photo", "document"):
            raise UserError("Неподдерживаемый тип вложения.")
        files = homework_attachments(
            {"attachments": attachments, "file_id": file_id, "file_kind": file_kind}
        )
        if (not body and not files) or len(body) > 3000:
            raise UserError("Добавьте текст до 3000 символов или вложение.")
        if len(files) > MAX_ATTACHMENTS:
            raise UserError(f"К одному ДЗ можно прикрепить до {MAX_ATTACHMENTS} файлов.")
        if any(
            not isinstance(file, dict)
            or not isinstance(file.get("file_id"), str)
            or not file["file_id"]
            or file.get("file_kind") not in ("photo", "document")
            for file in files
        ):
            raise UserError("Неподдерживаемый тип вложения.")
        first = files[0] if files else {"file_id": None, "file_kind": None}
        serialized = json.dumps(files)
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
                if attachments is not None:
                    expected.pop("file_id")
                    expected.pop("file_kind")
                    expected["attachments"] = attachments
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
                    """UPDATE homework SET body=?,file_id=?,file_kind=?,attachments=?,updated_by=?,updated=?,revision=revision+1
                  WHERE id=?""",
                    (body, first["file_id"], first["file_kind"], serialized, uid, now, hid),
                )
                self.log(uid, "edit_homework", hid, old)
            else:
                hid = self.conn.execute(
                    """INSERT INTO homework(lesson_id,body,file_id,file_kind,attachments,author,updated_by,created,updated)
                  VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        lid,
                        body,
                        first["file_id"],
                        first["file_kind"],
                        serialized,
                        uid,
                        uid,
                        now,
                        now,
                    ),
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
