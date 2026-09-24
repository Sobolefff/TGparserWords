"""Поиск по подставному клиенту Telegram — без сети и без аккаунта."""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telethon.tl.types import Channel  # noqa: E402

import parser as engine  # noqa: E402
from config import Config  # noqa: E402


class FakeSender:
    username = "tester"


class FakeMessage:
    def __init__(
        self, msg_id: int, text: str, date: datetime, entities=None, media=None, file=None,
        photo=None, video=None, audio=None, voice=None, video_note=None, gif=None,
        sticker=None, document=None,
    ):
        self.id = msg_id
        self.raw_text = text
        self.date = date
        self.entities = entities
        self.media = media
        self.file = file
        self.photo = photo
        self.video = video
        self.audio = audio
        self.voice = voice
        self.video_note = video_note
        self.gif = gif
        self.sticker = sticker
        self.document = document

    async def get_sender(self):
        return FakeSender()


class FakeClient:
    """Отдаёт сообщения от новых к старым, как настоящий iter_messages."""

    def __init__(self, messages):
        self.messages = messages
        self.calls = []

    def iter_messages(self, entity, limit=None, search=None, reverse=False):
        self.calls.append(search)
        # грубая имитация полнотекстового поиска Telegram: по основе слова
        needle = engine.stem(engine.normalize_text(search)) if search else None
        chosen = [
            m for m in self.messages
            if needle is None or needle in engine.normalize_text(m.raw_text or "")
        ]
        if reverse:
            chosen = list(reversed(chosen))

        async def gen():
            for msg in chosen[:limit] if limit else chosen:
                yield msg

        return gen()

    async def download_media(self, msg, file=None):
        path = f"{file}.bin"
        with open(path, "wb") as f:
            f.write(b"data")
        return path


def build_parser(messages):
    cfg = Config(api_id=1, api_hash="x", bot_token="y", request_delay=0, max_flood_wait=10)
    parser = object.__new__(engine.Parser)
    parser.cfg = cfg
    parser.client = FakeClient(messages)
    parser._entity_cache = {}
    return parser


NOW = datetime.now(timezone.utc)
CHANNEL = Channel(
    id=1234567, title="Тестовый чат", photo=None, date=NOW, username="testchat"
)
MESSAGES = [
    FakeMessage(30, "Сдаю квартиру в аренду, недорого", NOW - timedelta(days=1)),
    FakeMessage(29, "Просто болтовня без ключей", NOW - timedelta(days=2)),
    FakeMessage(28, "Ищу подрядчика на ремонт", NOW - timedelta(days=3)),
    FakeMessage(27, "Аренда офиса — старое сообщение", NOW - timedelta(days=90)),
]


class TestSearch(unittest.IsolatedAsyncioTestCase):
    async def collect(self, **kwargs):
        parser = build_parser(MESSAGES)
        params = dict(
            keywords=["аренда", "ищу подрядчика"],
            since=NOW - timedelta(days=30),
            limit=100,
            match_mode="smart",
            scan_mode=False,
        )
        params.update(kwargs)
        hits = [hit async for hit in parser.search_chat(CHANNEL, **params)]
        return hits, parser

    async def test_server_search(self):
        hits, parser = await self.collect()
        self.assertEqual({h.message_id for h in hits}, {30, 28})
        self.assertEqual(parser.client.calls, ["аренда", "ищу подрядчика"])

    async def test_scan_mode_finds_same_messages(self):
        hits, parser = await self.collect(scan_mode=True)
        self.assertEqual({h.message_id for h in hits}, {30, 28})
        self.assertEqual(parser.client.calls, [None])

    async def test_date_cutoff_stops_iteration(self):
        hits, _ = await self.collect(since=NOW - timedelta(days=2), scan_mode=True)
        self.assertEqual({h.message_id for h in hits}, {30})

    async def test_hit_fields(self):
        hits, _ = await self.collect(keywords=["аренда"])
        hit = next(h for h in hits if h.message_id == 30)
        self.assertEqual(hit.chat_tg_id, -1001234567)
        self.assertEqual(hit.chat_title, "Тестовый чат")
        self.assertEqual(hit.sender, "@tester")
        self.assertEqual(hit.keyword, "аренда")
        self.assertEqual(hit.link, "https://t.me/testchat/30")


if __name__ == "__main__":
    unittest.main()
