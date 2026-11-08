"""Editable Telegram screens with per-message, persistent navigation history."""

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaDocument


class Screen:
    def __init__(self, message, store, uid, message_id=None):
        self.message, self.store, self.uid = message, store, uid
        self.message_id = message_id
        state = store.screen_get(uid, message_id) if message_id else None
        self.current = state["current"] if state else None
        self.history = state["history"] if state else []
        self.pending = self.current
        self.pending_history = list(self.history)
        self.rewinding = False
        self.notice = None
        self.media = bool(state.get("media")) if state else bool(message.document or message.photo)

    def __getattr__(self, name):
        return getattr(self.message, name)

    @staticmethod
    def identity(route):
        # Pagination/refresh replaces the current choice; it isn't a new navigation level.
        return {
            key: value
            for key, value in route.items()
            if key not in ("page", "text_page")
            and not (
                route["action"] == "week"
                and (key == "monday" or (route.get("scope") == "day" and key == "day"))
            )
        }

    def route(self, action, **payload):
        route = {"action": action, **payload}
        history = list(self.history)
        if action == "home":
            history = []
        elif self.rewinding:
            history = list(self.pending_history)
        elif self.current:
            identity = self.identity(route)
            if identity == self.identity(self.current):
                pass
            else:
                match = next(
                    (
                        i
                        for i in range(len(history) - 1, -1, -1)
                        if self.identity(history[i]) == identity
                    ),
                    None,
                )
                if match is not None:
                    history = history[:match]
                elif self.current["action"] not in (
                    "preview",
                    "person_preview",
                    "pick_person",
                    "pick_confirm",
                ):
                    history.append(self.current)
        else:
            history = [{"action": "home"}]
        self.pending, self.pending_history = route, history[-30:]

    def back(self):
        history = list(self.history)
        route = history.pop() if history else {"action": "home"}
        self.pending_history = history
        self.rewinding = True
        return route

    def return_to(self, action, **payload):
        """Return after a mutation to its existing list/card, keeping that list's page."""
        for route in reversed(self.history):
            if route["action"] == action and all(route.get(k) == v for k, v in payload.items()):
                return route
        # A legacy button or a shortcut may not have opened its destination before.
        # Never make Back re-enter a completed mutation/input flow.
        self.pending_history = [
            route
            for route in self.history
            if route["action"]
            not in (
                "write",
                "person",
                "delete",
                "preview",
                "person_preview",
                "pick_person",
                "pick_confirm",
            )
        ]
        if not self.pending_history:
            self.pending_history = [{"action": "home"}]
        self.rewinding = True
        return {"action": action, **payload}

    async def answer(self, text, reply_markup=None, document=None, **kwargs):
        rows = list(reply_markup.inline_keyboard) if reply_markup else []
        if self.pending_history:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="⬅️ Назад", callback_data=self.store.token(self.uid, "back")
                    )
                ]
            )
        markup = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
        if self.notice:
            text = self.notice + "\n\n" + text
            self.notice = None
        old_id = self.message_id
        replace = self.media and document is None

        async def send_new():
            if document is not None:
                return await self.message.answer_document(
                    document, caption=text, reply_markup=markup
                )
            return await self.message.answer(text, reply_markup=markup, **kwargs)

        if old_id is not None and not replace:
            try:
                if document is not None:
                    result = await self.message.bot.edit_message_media(
                        chat_id=self.message.chat.id,
                        message_id=old_id,
                        media=InputMediaDocument(media=document, caption=text, parse_mode="HTML"),
                        reply_markup=markup,
                    )
                else:
                    result = await self.message.bot.edit_message_text(
                        text=text,
                        chat_id=self.message.chat.id,
                        message_id=old_id,
                        reply_markup=markup,
                        **kwargs,
                    )
            except TelegramBadRequest as exc:
                error = exc.message.lower()
                if "message is not modified" in error:
                    result = self.message
                elif any(
                    reason in error
                    for reason in (
                        "message to edit not found",
                        "message can't be edited",
                        "message can not be edited",
                    )
                ):
                    result = await send_new()
                    replace = True
                else:
                    raise
        else:
            result = await send_new()
        if (
            result is not True
            and getattr(result, "message_id", None)
            and (replace or old_id is None)
        ):
            self.message_id = result.message_id
        if replace and old_id is not None and old_id != self.message_id:
            try:
                await self.message.bot.delete_message(self.message.chat.id, old_id)
            except TelegramBadRequest:
                # If Telegram refuses deletion, at least retire its keyboard.
                try:
                    await self.message.bot.edit_message_reply_markup(
                        chat_id=self.message.chat.id, message_id=old_id, reply_markup=None
                    )
                except TelegramBadRequest:
                    pass
        self.media = document is not None
        self.current, self.history = self.pending, self.pending_history
        self.store.screen_put(
            self.uid, self.message_id, self.current, self.history, media=self.media
        )
        return result

    def bind_input(self):
        state = self.store.session_get(self.uid)
        if state:
            self.store.session_set(self.uid, {**state, "screen_id": self.message_id})
