"""Движок парсера: Telethon-клиент, разбор ссылок, поиск по ключевым словам."""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator, Iterable

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
from telethon.tl.types import (
    Channel,
    Chat,
    ChatInviteAlready,
    MessageEntityTextUrl,
    MessageEntityUrl,
    User,
)

log = logging.getLogger("parser")

# Служебные пути t.me, которые не являются именами чатов
_RESERVED = {"joinchat", "c", "s", "addstickers", "proxy", "socks", "share", "iv"}


# --------------------------------------------------------------- ссылки
def normalize_link(raw: str) -> tuple[str, str] | None:
    """Приводит ссылку к виду ('username', 'durov') / ('invite', 'AbC') / ('id', '-100123').

    Понимает: @name, name, t.me/name, https://t.me/name/123 (ссылка на сообщение),
    t.me/+hash, t.me/joinchat/hash, t.me/c/123456/789.
    """
    s = (raw or "").strip()
    if not s:
        return None
    s = s.split("?", 1)[0].rstrip("/")

    if s.startswith("@"):
        name = s[1:]
        return ("username", name) if _valid_username(name) else None

    if s.lstrip("-").isdigit():
        return ("id", s)

    m = re.match(r"^(?:https?://)?(?:www\.)?(?:t(?:elegram)?\.me|telegram\.dog)/(.+)$", s, re.I)
    path = m.group(1) if m else s
    parts = [p for p in path.split("/") if p]
    if not parts:
        return None

    first = parts[0]
    if first.startswith("+"):
        return ("invite", first[1:])
    if first.lower() == "joinchat" and len(parts) > 1:
        return ("invite", parts[1])
    if first.lower() == "c" and len(parts) > 1 and parts[1].isdigit():
        return ("id", f"-100{parts[1]}")
    if first.lower() in _RESERVED:
        return None
    return ("username", first) if _valid_username(first) else None


def _valid_username(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{2,}", name or ""))


def extract_links(text: str) -> list[str]:
    """Достаёт ссылки/юзернеймы из сообщения (по строкам, пробелам и запятым).

    Принимаются только явные формы: t.me/..., @name или числовой id, — иначе
    обычное слово в сообщении можно случайно принять за ссылку на чат.
    """
    found: list[str] = []
    for chunk in re.split(r"[\s,;]+", text or ""):
        chunk = chunk.strip()
        if not chunk or not normalize_link(chunk):
            continue
        explicit = chunk.startswith("@") or chunk.lstrip("-").isdigit() or "t.me/" in chunk.lower()
        if explicit:
            found.append(chunk)
    return found


# ------------------------------------------------------- сопоставление
def normalize_text(text: str) -> str:
    return (text or "").lower().replace("ё", "е")


# Частые окончания: отсекаем их, чтобы «аренда» находила «аренду», «аренде» и т.д.
_ENDINGS_RU = (
    "ться", "тся", "ами", "ями", "иях", "ого", "его", "ому", "ему", "ыми", "ими",
    "ать", "ять", "ить", "еть", "ых", "их", "ая", "яя", "ое", "ее", "ые", "ие",
    "ой", "ей", "ом", "ем", "ах", "ях", "ов", "ев", "ью", "ья", "ти", "ть",
    "а", "я", "у", "ю", "ы", "и", "е", "о", "ь", "й",
)
_ENDINGS_EN = ("ings", "ing", "ies", "ed", "es", "s")
_MIN_STEM = 3          # короче основу не режем
_MAX_SUFFIX = 3        # сколько букв разрешаем дописать к основе


def stem(word: str) -> str:
    endings = _ENDINGS_EN if word.isascii() else _ENDINGS_RU
    for ending in endings:
        if word.endswith(ending) and len(word) - len(ending) >= _MIN_STEM:
            return word[: -len(ending)]
    return word


def build_pattern(keyword: str, mode: str = "smart") -> re.Pattern[str]:
    """smart — слово с любым окончанием, exact — точное слово, substring — подстрока.

    Пробелы в ключевой фразе соответствуют любым пробельным символам,
    поэтому «ищу подрядчика» найдётся и при переносе строки.
    """
    kw = normalize_text(keyword).strip()
    if not kw:
        return re.compile(r"(?!x)x")  # заведомо непустой шаблон, который ничего не находит

    if mode == "substring":
        body = re.escape(kw).replace(r"\ ", r"\s+")
        return re.compile(body, re.U)

    if mode == "exact":
        body = re.escape(kw).replace(r"\ ", r"\s+")
        return re.compile(rf"(?<!\w){body}(?!\w)", re.U)

    body = r"\s+".join(
        rf"{re.escape(stem(word))}\w{{0,{_MAX_SUFFIX}}}" for word in kw.split()
    )
    return re.compile(rf"(?<!\w){body}(?!\w)", re.U)


@dataclass
class Hit:
    chat_tg_id: int
    chat_title: str
    message_id: int
    keyword: str
    sender: str
    text: str
    date: datetime | None
    link: str


# ----------------------------------------------------------- сам парсер
class Parser:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.client = TelegramClient(cfg.session_name, cfg.api_id, cfg.api_hash)
        self._entity_cache: dict[str, object] = {}

    async def start(self) -> str:
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise SystemExit(
                "Аккаунт не авторизован. Сначала выполните: python login.py"
            )
        me = await self.client.get_me()
        return f"@{me.username}" if me.username else (me.first_name or str(me.id))

    async def stop(self) -> None:
        await self.client.disconnect()

    # --- ссылка -> сущность Telegram ---
    async def resolve(self, link: str):
        if link in self._entity_cache:
            return self._entity_cache[link]

        parsed = normalize_link(link)
        if not parsed:
            raise ValueError("не похоже на ссылку на чат")
        kind, value = parsed

        if kind == "invite":
            entity = await self._resolve_invite(value)
        elif kind == "id":
            entity = await self.client.get_entity(int(value))
        else:
            entity = await self.client.get_entity(value)
            if self.cfg.auto_join and isinstance(entity, Channel):
                try:
                    await self.client(JoinChannelRequest(entity))
                except Exception as exc:  # уже участник / нет прав — не критично
                    log.debug("join %s: %s", value, exc)

        self._entity_cache[link] = entity
        return entity

    async def _resolve_invite(self, invite_hash: str):
        info = await self.client(CheckChatInviteRequest(invite_hash))
        if isinstance(info, ChatInviteAlready):
            return info.chat
        if not self.cfg.auto_join:
            raise ValueError(
                "приватный чат: вступите в него вручную или включите AUTO_JOIN=true"
            )
        updates = await self.client(ImportChatInviteRequest(invite_hash))
        return updates.chats[0]

    # --- поиск ---
    async def search_chat(
        self,
        entity,
        keywords: Iterable[str],
        since: datetime | None,
        limit: int,
        match_mode: str,
        scan_mode: bool,
    ) -> AsyncIterator[Hit]:
        keywords = list(keywords)
        patterns = {kw: build_pattern(kw, match_mode) for kw in keywords}

        if scan_mode:
            async for hit in self._scan(entity, patterns, since, limit):
                yield hit
        else:
            for kw in keywords:
                async for hit in self._server_search(entity, kw, patterns[kw], since, limit):
                    yield hit
                await asyncio.sleep(self.cfg.request_delay)

    async def _server_search(self, entity, keyword, pattern, since, limit):
        """Поиск средствами Telegram (search=...) — быстро, грузит только нужное."""
        async for msg in self._iter(entity, limit=limit, search=keyword):
            if since and msg.date and msg.date < since:
                break
            text = msg.raw_text or ""
            if pattern.search(normalize_text(text)):
                yield await self._to_hit(entity, msg, keyword, text)

    async def _scan(self, entity, patterns, since, limit):
        """Локальное сканирование истории — медленнее, зато ловит любые вхождения."""
        async for msg in self._iter(entity, limit=limit):
            if since and msg.date and msg.date < since:
                break
            text = msg.raw_text or ""
            if not text:
                continue
            norm = normalize_text(text)
            for kw, pattern in patterns.items():
                if pattern.search(norm):
                    yield await self._to_hit(entity, msg, kw, text)

    async def _iter(self, entity, **kwargs):
        """iter_messages с обработкой FloodWait."""
        while True:
            try:
                async for msg in self.client.iter_messages(entity, **kwargs):
                    yield msg
                return
            except FloodWaitError as exc:
                if exc.seconds > self.cfg.max_flood_wait:
                    log.warning("FloodWait %s сек — чат пропущен", exc.seconds)
                    return
                log.info("FloodWait %s сек — ждём", exc.seconds)
                await asyncio.sleep(exc.seconds + 1)

    async def _to_hit(self, entity, msg, keyword: str, text: str) -> Hit:
        return Hit(
            chat_tg_id=chat_id_of(entity),
            chat_title=title_of(entity),
            message_id=msg.id,
            keyword=keyword,
            sender=await sender_name(msg),
            text=text,
            date=msg.date,
            link=message_link(entity, msg.id),
        )

    # --- выгрузка полной истории ---
    async def export_history(self, entity, limit: int | None = None) -> AsyncIterator[dict]:
        """Отдаёт всю доступную историю чата, сообщение за сообщением (от новых к старым)."""
        async for msg in self._iter(entity, limit=limit):
            yield message_to_dict(entity, msg)

    # --- мониторинг новых сообщений ---
    def add_monitor(self, callback) -> None:
        """callback(Hit) вызывается для каждого нового подходящего сообщения."""

        @self.client.on(events.NewMessage())
        async def _handler(event):  # pragma: no cover - вызывается Telegram'ом
            try:
                await callback(event)
            except Exception:
                log.exception("ошибка обработчика мониторинга")


# ----------------------------------------------------------- утилиты
def chat_id_of(entity) -> int:
    ident = getattr(entity, "id", 0)
    if isinstance(entity, Channel):
        return int(f"-100{ident}")
    if isinstance(entity, Chat):
        return -ident
    return ident


def title_of(entity) -> str:
    title = getattr(entity, "title", None)
    if title:
        return title
    if isinstance(entity, User):
        return " ".join(filter(None, [entity.first_name, entity.last_name])) or str(entity.id)
    return str(getattr(entity, "id", "?"))


def message_link(entity, message_id: int) -> str:
    username = getattr(entity, "username", None)
    if username:
        return f"https://t.me/{username}/{message_id}"
    ident = getattr(entity, "id", None)
    if isinstance(entity, Channel) and ident:
        return f"https://t.me/c/{ident}/{message_id}"
    return ""


_URL_RE = re.compile(r"https?://\S+")


def extract_message_links(msg) -> list[str]:
    """Ссылки внутри сообщения: явно размеченные Telegram'ом + найденные по тексту."""
    text = msg.raw_text or ""
    urls: list[str] = []
    for ent in msg.entities or ():
        if isinstance(ent, MessageEntityTextUrl):
            urls.append(ent.url)
        elif isinstance(ent, MessageEntityUrl):
            urls.append(text[ent.offset: ent.offset + ent.length])
    for m in _URL_RE.finditer(text):
        urls.append(m.group(0).rstrip(").,;"))

    seen: set[str] = set()
    result: list[str] = []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            result.append(u)
    return result


def media_info(msg) -> dict | None:
    """Краткое описание вложения: тип, имя файла, mime, размер — если есть медиа."""
    media = msg.media
    if media is None:
        return None
    info: dict = {"type": type(media).__name__}

    try:
        file = msg.file
    except Exception:
        file = None
    if file is not None:
        info["file_name"] = getattr(file, "name", None)
        info["mime_type"] = getattr(file, "mime_type", None)
        info["size"] = getattr(file, "size", None)

    webpage = getattr(media, "webpage", None)
    if webpage is not None:
        info["url"] = getattr(webpage, "url", None)
        info["title"] = getattr(webpage, "title", None)

    return info


def message_to_dict(entity, msg) -> dict:
    return {
        "id": msg.id,
        "date": msg.date.isoformat() if msg.date else None,
        "text": msg.raw_text or "",
        "links": extract_message_links(msg),
        "media": media_info(msg),
        "message_link": message_link(entity, msg.id),
    }


async def sender_name(msg) -> str:
    try:
        sender = await msg.get_sender()
    except Exception:
        sender = None
    if sender is None:
        return "—"
    if getattr(sender, "username", None):
        return f"@{sender.username}"
    name = " ".join(
        filter(None, [getattr(sender, "first_name", None), getattr(sender, "last_name", None)])
    )
    return name or getattr(sender, "title", None) or str(getattr(sender, "id", "—"))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
