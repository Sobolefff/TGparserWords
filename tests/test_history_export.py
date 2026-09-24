"""Выгрузка полной истории чата в JSON — на подставном клиенте, без сети."""
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl  # noqa: E402

import parser as engine  # noqa: E402

from test_search import CHANNEL, FakeMessage, build_parser  # noqa: E402


class FakeFile:
    def __init__(self, name=None, mime_type=None, size=None):
        self.name = name
        self.mime_type = mime_type
        self.size = size


class FakeWebpage:
    def __init__(self, url, title):
        self.url = url
        self.title = title


class TestLinksExtraction(unittest.TestCase):
    def test_entities_and_plain_urls(self):
        text = "Смотри https://example.com/a и подробнее тут"
        entities = [
            MessageEntityUrl(offset=7, length=len("https://example.com/a")),
            MessageEntityTextUrl(offset=len(text) - 4, length=4, url="https://example.com/b"),
        ]
        msg = FakeMessage(1, text, datetime.now(timezone.utc), entities=entities)
        links = engine.extract_message_links(msg)
        self.assertEqual(links, ["https://example.com/a", "https://example.com/b"])

    def test_dedup_and_no_links(self):
        msg = FakeMessage(2, "без ссылок вообще", datetime.now(timezone.utc))
        self.assertEqual(engine.extract_message_links(msg), [])

        msg2 = FakeMessage(3, "дубль https://a.com и ещё раз https://a.com", datetime.now(timezone.utc))
        self.assertEqual(engine.extract_message_links(msg2), ["https://a.com"])


class TestMediaInfo(unittest.TestCase):
    def test_no_media(self):
        msg = FakeMessage(1, "текст", datetime.now(timezone.utc))
        self.assertIsNone(engine.media_info(msg))

    def test_document_media(self):
        media = type("MessageMediaDocument", (), {})()
        msg = FakeMessage(
            1, "файл", datetime.now(timezone.utc),
            media=media, file=FakeFile(name="a.pdf", mime_type="application/pdf", size=1234),
        )
        info = engine.media_info(msg)
        self.assertEqual(info["type"], "MessageMediaDocument")
        self.assertEqual(info["file_name"], "a.pdf")
        self.assertEqual(info["mime_type"], "application/pdf")
        self.assertEqual(info["size"], 1234)

    def test_webpage_media(self):
        media = type("MessageMediaWebPage", (), {})()
        media.webpage = FakeWebpage(url="https://a.com", title="Заголовок")
        msg = FakeMessage(1, "ссылка с превью", datetime.now(timezone.utc), media=media)
        info = engine.media_info(msg)
        self.assertEqual(info["url"], "https://a.com")
        self.assertEqual(info["title"], "Заголовок")


class TestExportHistory(unittest.IsolatedAsyncioTestCase):
    async def test_export_yields_all_messages_as_dicts(self):
        messages = [
            FakeMessage(10, "первое", datetime(2026, 1, 1, tzinfo=timezone.utc)),
            FakeMessage(9, "второе https://a.com", datetime(2026, 1, 2, tzinfo=timezone.utc)),
        ]
        parser = build_parser(messages)
        items = [item async for item in parser.export_history(CHANNEL)]
        self.assertEqual([i["id"] for i in items], [10, 9])
        self.assertEqual(items[1]["links"], ["https://a.com"])
        self.assertEqual(items[0]["message_link"], "https://t.me/testchat/10")
        self.assertIsNone(items[0]["media"])


if __name__ == "__main__":
    unittest.main()
