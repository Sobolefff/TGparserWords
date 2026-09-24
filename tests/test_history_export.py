"""Выгрузка полной истории чата (JSON, HTML+медиа) — на подставном клиенте, без сети."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl  # noqa: E402

import export as export_mod  # noqa: E402
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


class TestMediaKind(unittest.TestCase):
    def test_kinds(self):
        now = datetime.now(timezone.utc)
        self.assertEqual(engine.media_kind(FakeMessage(1, "", now, photo=True, media=True)), "photo")
        self.assertEqual(engine.media_kind(FakeMessage(2, "", now, video=True, media=True)), "video")
        self.assertEqual(engine.media_kind(FakeMessage(3, "", now, voice=True, media=True)), "audio")
        self.assertEqual(engine.media_kind(FakeMessage(4, "", now, sticker=True, media=True)), "sticker")
        self.assertEqual(engine.media_kind(FakeMessage(5, "", now, document=True, media=True)), "document")
        self.assertIsNone(engine.media_kind(FakeMessage(6, "", now)))


class TestExportHistory(unittest.IsolatedAsyncioTestCase):
    async def test_export_yields_all_messages_as_dicts(self):
        messages = [
            FakeMessage(10, "первое", datetime(2026, 1, 1, tzinfo=timezone.utc)),
            FakeMessage(9, "второе https://a.com", datetime(2026, 1, 2, tzinfo=timezone.utc)),
        ]
        parser = build_parser(messages)
        items = [item async for item in parser.export_history(CHANNEL)]
        # export_history идёт от старых сообщений к новым (Telethon reverse=True)
        self.assertEqual([i["id"] for i in items], [9, 10])
        self.assertEqual(items[0]["links"], ["https://a.com"])
        self.assertEqual(items[1]["message_link"], "https://t.me/testchat/10")
        self.assertIsNone(items[1]["media"])


class TestExportModule(unittest.IsolatedAsyncioTestCase):
    async def test_export_json_streams_valid_file(self):
        messages = [
            FakeMessage(1, "первое", datetime(2026, 1, 1, tzinfo=timezone.utc)),
            FakeMessage(2, "второе", datetime(2026, 1, 2, tzinfo=timezone.utc)),
        ]
        parser = build_parser(messages)
        with tempfile.TemporaryDirectory() as tmp:
            out_path = os.path.join(tmp, "history.json")
            count = await export_mod.export_json(parser, CHANNEL, out_path)
            self.assertEqual(count, 2)
            with open(out_path, encoding="utf-8") as f:
                payload = json.load(f)
            self.assertEqual(payload["messages_count"], 2)
            self.assertEqual(payload["chat"]["title"], "Тестовый чат")
            self.assertEqual(len(payload["messages"]), 2)

    async def test_export_html_embeds_media_and_skips_oversized(self):
        photo_media = type("MessageMediaPhoto", (), {})()
        big_media = type("MessageMediaDocument", (), {})()
        messages = [
            FakeMessage(
                1, "с картинкой", datetime(2026, 1, 1, tzinfo=timezone.utc),
                media=photo_media, photo=True,
            ),
            FakeMessage(2, "просто текст", datetime(2026, 1, 2, tzinfo=timezone.utc)),
            FakeMessage(
                3, "огромный файл", datetime(2026, 1, 3, tzinfo=timezone.utc),
                media=big_media, document=True,
                file=type("F", (), {"size": export_mod.MAX_MEDIA_SIZE + 1, "name": "big.bin"})(),
            ),
        ]
        parser = build_parser(messages)
        with tempfile.TemporaryDirectory() as tmp:
            html_path, count = await export_mod.export_html(parser, CHANNEL, tmp)
            self.assertEqual(count, 3)
            with open(html_path, encoding="utf-8") as f:
                content = f.read()
            self.assertIn("<img", content)
            self.assertIn("просто текст", content)
            self.assertIn("слишком большое", content)
            media_files = os.listdir(os.path.join(tmp, export_mod.MEDIA_SUBDIR))
            self.assertEqual(len(media_files), 1)  # большой файл не скачан


if __name__ == "__main__":
    unittest.main()
