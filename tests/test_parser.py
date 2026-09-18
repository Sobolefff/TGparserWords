"""Тесты разбора ссылок и сопоставления ключевых слов: python -m unittest discover tests"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import parser as engine  # noqa: E402
import storage  # noqa: E402


class TestLinks(unittest.TestCase):
    def test_usernames(self):
        cases = {
            "https://t.me/durov": ("username", "durov"),
            "http://t.me/durov/": ("username", "durov"),
            "t.me/some_chat": ("username", "some_chat"),
            "@some_chat": ("username", "some_chat"),
            "https://telegram.me/some_chat?single": ("username", "some_chat"),
            "https://t.me/some_chat/1234": ("username", "some_chat"),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(engine.normalize_link(raw), expected)

    def test_invites_and_ids(self):
        self.assertEqual(engine.normalize_link("https://t.me/+AbCdEf12"), ("invite", "AbCdEf12"))
        self.assertEqual(engine.normalize_link("t.me/joinchat/AbCdEf12"), ("invite", "AbCdEf12"))
        self.assertEqual(engine.normalize_link("https://t.me/c/1234567/89"), ("id", "-1001234567"))
        self.assertEqual(engine.normalize_link("-1001234567"), ("id", "-1001234567"))

    def test_rejects_garbage(self):
        for raw in ["", "   ", "привет", "https://example.com/chat", "@ab", "t.me/addstickers/pack"]:
            with self.subTest(raw=raw):
                self.assertIsNone(engine.normalize_link(raw))

    def test_extract_ignores_plain_words(self):
        self.assertEqual(engine.extract_links("привет как дела hello"), [])

    def test_extract_many(self):
        text = "https://t.me/one\n@two_chat, t.me/three_chat\nне ссылка\nhttps://t.me/+Hash1234"
        self.assertEqual(
            engine.extract_links(text),
            ["https://t.me/one", "@two_chat", "t.me/three_chat", "https://t.me/+Hash1234"],
        )


class TestMatching(unittest.TestCase):
    def match(self, keyword, text, mode="smart"):
        return bool(engine.build_pattern(keyword, mode).search(engine.normalize_text(text)))

    def test_smart_catches_word_forms(self):
        self.assertTrue(self.match("аренда", "Сдаю в Аренду студию"))
        self.assertTrue(self.match("купить", "хочу КУПИТЬ авто"))

    def test_smart_ignores_other_words(self):
        self.assertFalse(self.match("кот", "который час"))
        self.assertFalse(self.match("аренда", "субаренда помещения"))

    def test_exact_and_substring(self):
        self.assertFalse(self.match("аренда", "сдам в аренду", mode="exact"))
        self.assertTrue(self.match("аренда", "сдам в аренда", mode="exact"))
        self.assertTrue(self.match("ренд", "субаренда", mode="substring"))

    def test_phrases_and_yo(self):
        self.assertTrue(self.match("ищу подрядчика", "Срочно  ищу\nподрядчика на объект"))
        self.assertTrue(self.match("ёлка", "Продаю елки оптом"))


class TestStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        storage.init(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_chats_and_keywords_unique(self):
        self.assertTrue(storage.add_chat("https://t.me/one"))
        self.assertFalse(storage.add_chat("https://t.me/one"))
        self.assertEqual(storage.count_chats(), 1)
        self.assertTrue(storage.add_keyword("аренда"))
        self.assertFalse(storage.add_keyword("аренда"))
        self.assertEqual(storage.list_keywords(), ["аренда"])
        self.assertTrue(storage.delete_keyword("АРЕНДА"))
        self.assertEqual(storage.list_keywords(), [])

    def test_hits_are_deduplicated(self):
        payload = dict(
            chat_tg_id=-100123, message_id=7, keyword="аренда", chat_title="Чат",
            sender="@user", text="сдам в аренду", date="2026-01-01 10:00", link="https://t.me/c/123/7",
        )
        self.assertTrue(storage.save_hit(**payload))
        self.assertFalse(storage.save_hit(**payload))
        self.assertTrue(storage.save_hit(**{**payload, "keyword": "сдам"}))
        self.assertEqual(storage.count_hits(), 2)

    def test_settings_roundtrip(self):
        self.assertIsNone(storage.get_setting("days"))
        storage.set_setting("days", 60)
        storage.set_setting("days", 90)
        self.assertEqual(storage.get_setting("days"), "90")

    def test_chat_status_update(self):
        storage.add_chat("https://t.me/one")
        chat = storage.list_chats()[0]
        storage.update_chat(chat["id"], status="ok", tg_id=-1001, title="Один")
        updated = storage.list_chats()[0]
        self.assertEqual((updated["status"], updated["title"]), ("ok", "Один"))
        self.assertEqual(storage.monitored_chat_ids(), {-1001})


if __name__ == "__main__":
    unittest.main()
