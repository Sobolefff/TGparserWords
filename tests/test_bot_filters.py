"""Проверяем, что ввод данных (состояния) не перехватывает команды и кнопки меню."""
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiogram.types import Chat, Message  # noqa: E402

import bot as botmod  # noqa: E402


def make(text: str) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=1, type="private"),
        text=text,
    )


class TestPlainTextFilter(unittest.TestCase):
    def test_accepts_user_input(self):
        for text in ["https://t.me/chat", "аренда, ремонт", "@some_chat"]:
            with self.subTest(text=text):
                self.assertTrue(botmod.PLAIN_TEXT.resolve(make(text)))

    def test_rejects_commands_and_buttons(self):
        for text in ["/words", "/search", botmod.BTN_WORDS, botmod.BTN_SEARCH, botmod.BTN_EXPORT]:
            with self.subTest(text=text):
                self.assertFalse(botmod.PLAIN_TEXT.resolve(make(text)))


class TestHelpers(unittest.TestCase):
    def test_format_escapes_html(self):
        rendered = botmod.format_parts(
            "2026-01-01", "<b>Чат</b>", "@user", "аренда", "текст & <script>", "https://t.me/c/1/2"
        )
        self.assertIn("&lt;b&gt;Чат&lt;/b&gt;", rendered)
        self.assertIn("&amp;", rendered)
        self.assertIn("https://t.me/c/1/2", rendered)

    def test_long_text_is_trimmed(self):
        rendered = botmod.format_parts("", "Чат", "@u", "kw", "x" * 1000, "")
        self.assertIn("…", rendered)
        self.assertLess(len(rendered), 600)


if __name__ == "__main__":
    unittest.main()
