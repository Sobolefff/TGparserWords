"""Потоковая выгрузка полной истории чата: JSON и HTML+медиа.

Всё пишется на диск по мере получения сообщений от Telegram — в памяти
одновременно держится только одно сообщение, поэтому размер чата не
ограничен доступной оперативной памятью.
"""
from __future__ import annotations

import html as html_lib
import json
import logging
import os
import re
import time
import zipfile
from datetime import datetime, timezone
from typing import Awaitable, Callable

import parser as engine

log = logging.getLogger("export")

ProgressCB = Callable[[int], Awaitable[None]] | None
# Обновляем статус либо каждые N сообщений, либо раз в T секунд — что наступит раньше.
# Только счётчика недостаточно: в маленьком канале скачивание медиа может идти минутами,
# а сообщений там меньше порога — без таймера статус так и провисит на "подключаюсь".
PROGRESS_EVERY = 20
PROGRESS_SECONDS = 8.0


class _Progress:
    def __init__(self, cb: ProgressCB):
        self._cb = cb
        self._last_ts = time.monotonic()

    async def tick(self, count: int) -> None:
        if not self._cb:
            return
        now = time.monotonic()
        if count % PROGRESS_EVERY == 0 or now - self._last_ts >= PROGRESS_SECONDS:
            self._last_ts = now
            await self._cb(count)

MEDIA_SUBDIR = "media"
MAX_MEDIA_SIZE = 20 * 1024 * 1024  # 20 МБ — крупные файлы не тянем, оставляем ссылку на оригинал
DEFAULT_MAX_BYTES = 45 * 1024 * 1024  # дефолт, если вызывающий код не передал свой лимит

_URL_RE = re.compile(r"https?://\S+")


def _chat_meta(entity) -> dict:
    return {
        "title": engine.title_of(entity),
        "username": getattr(entity, "username", None),
        "id": engine.chat_id_of(entity),
    }


def _part_path(base_path: str, n: int) -> str:
    root, ext = os.path.splitext(base_path)
    return f"{root}.part{n:03d}{ext}"


# --------------------------------------------------------------- JSON
class _JsonPartWriter:
    """Пишет сообщения в JSON-файлы по частям — каждая часть не крупнее max_bytes.

    Части — самостоятельные валидные JSON-объекты (chat/part/messages), не один общий
    массив: так можно отправлять их по одному, не собирая всё в память для склейки.
    """

    def __init__(self, base_path: str, max_bytes: int, chat_meta: dict):
        self.base_path = base_path
        self.max_bytes = max_bytes
        self.chat_meta = chat_meta
        self.part_paths: list[str] = []
        self._f = None
        self._first_item = True
        self._bytes_in_part = 0
        self._count_in_part = 0
        self._part_num = 0
        self._open_new_part()

    def _open_new_part(self) -> None:
        self._part_num += 1
        path = _part_path(self.base_path, self._part_num)
        self.part_paths.append(path)
        self._f = open(path, "w", encoding="utf-8")
        header = (
            "{\n"
            f'  "chat": {json.dumps(self.chat_meta, ensure_ascii=False)},\n'
            f'  "part": {self._part_num},\n'
            '  "messages": [\n'
        )
        self._f.write(header)
        self._bytes_in_part = len(header.encode("utf-8"))
        self._first_item = True
        self._count_in_part = 0

    def write(self, item: dict) -> None:
        entry = json.dumps(item, ensure_ascii=False)
        piece = entry if self._first_item else f",\n    {entry}"
        piece_bytes = len(piece.encode("utf-8"))
        if not self._first_item and self._bytes_in_part + piece_bytes > self.max_bytes:
            self._close_part()
            self._open_new_part()
            piece = entry
            piece_bytes = len(piece.encode("utf-8"))
        if self._first_item:
            piece = "    " + piece
            piece_bytes = len(piece.encode("utf-8"))
        self._f.write(piece)
        self._bytes_in_part += piece_bytes
        self._first_item = False
        self._count_in_part += 1

    def _close_part(self) -> None:
        self._f.write(f'\n  ],\n  "messages_count": {self._count_in_part}\n}}\n')
        self._f.close()

    def close(self) -> list[str]:
        self._close_part()
        return self.part_paths


async def export_json(
    parser, entity, out_path: str, progress: ProgressCB = None, max_bytes: int = DEFAULT_MAX_BYTES,
) -> tuple[list[str], int]:
    """Пишет JSON потоково, разбивая на части по max_bytes. Отдаёт (пути частей, число сообщений)."""
    writer = _JsonPartWriter(out_path, max_bytes, _chat_meta(entity))
    count = 0
    tracker = _Progress(progress)
    async for item in parser.export_history(entity):
        writer.write(item)
        count += 1
        await tracker.tick(count)
    part_paths = writer.close()
    return part_paths, count


# --------------------------------------------------------------- HTML
def _linkify(text: str) -> str:
    """Экранирует текст и оборачивает URL в кликабельные ссылки."""
    escaped = html_lib.escape(text)

    def repl(m: re.Match) -> str:
        url = m.group(0)
        trail = ""
        while url and url[-1] in ").,;\"'":
            trail = url[-1] + trail
            url = url[:-1]
        return f'<a href="{url}" target="_blank" rel="noopener noreferrer">{url}</a>{trail}'

    return _URL_RE.sub(repl, escaped)


_HTML_HEAD = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — история чата</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif; background: #0e1621;
         color: #e4e7eb; margin: 0; padding: 0 12px 40px; }}
  header {{ padding: 20px 4px 12px; border-bottom: 1px solid #223; margin-bottom: 16px; }}
  header h1 {{ margin: 0 0 4px; font-size: 20px; }}
  header p {{ margin: 0; color: #8a97a8; font-size: 13px; }}
  .msg {{ max-width: 720px; margin: 0 auto 14px; background: #17212b; border-radius: 10px;
          padding: 10px 14px; }}
  .meta {{ font-size: 12px; color: #7c8794; margin-bottom: 6px; display: flex; gap: 10px; }}
  .meta a {{ color: #7c8794; text-decoration: none; }}
  .text {{ white-space: pre-wrap; word-wrap: break-word; line-height: 1.4; }}
  .text a {{ color: #6ab7ff; }}
  img, video {{ max-width: 100%; border-radius: 8px; display: block; margin-bottom: 8px; }}
  audio {{ width: 100%; margin-bottom: 8px; }}
  .file, .missing {{ display: inline-block; margin-bottom: 8px; padding: 6px 10px;
                      background: #223; border-radius: 6px; font-size: 13px; }}
  .file {{ color: #6ab7ff; text-decoration: none; }}
  .missing {{ color: #8a97a8; }}
  footer {{ max-width: 720px; margin: 20px auto; color: #8a97a8; font-size: 13px; }}
</style>
</head>
<body>
<header>
  <h1>{title}</h1>
  <p>Экспортировано: {exported_at}</p>
</header>
<main>
"""

_HTML_TAIL = """</main>
<footer>Всего сообщений: {count}</footer>
</body>
</html>
"""

_MEDIA_TAG = {
    "photo": '<img loading="lazy" src="{src}" alt="">',
    "video": '<video controls preload="none" src="{src}"></video>',
    "audio": '<audio controls src="{src}"></audio>',
    "sticker": '<img loading="lazy" src="{src}" alt="стикер">',
}


def _media_block(kind: str | None, rel_path: str | None, msg) -> str:
    if kind is None:
        return ""
    if rel_path is None:
        size = getattr(getattr(msg, "file", None), "size", None)
        note = "слишком большое" if size and size > MAX_MEDIA_SIZE else "не удалось скачать"
        return f'<div class="missing">📎 вложение ({html_lib.escape(kind)}), {note}</div>'
    if kind in _MEDIA_TAG:
        return _MEDIA_TAG[kind].format(src=rel_path)
    name = getattr(getattr(msg, "file", None), "name", None) or os.path.basename(rel_path)
    return f'<a class="file" href="{rel_path}" download>📎 {html_lib.escape(name)}</a>'


async def export_html(
    parser,
    entity,
    work_dir: str,
    progress: ProgressCB = None,
    download_media: bool = True,
) -> tuple[str, int]:
    """Строит work_dir/index.html (+ work_dir/media/*) потоково, отдаёт (html_path, число сообщений)."""
    media_dir = os.path.join(work_dir, MEDIA_SUBDIR)
    os.makedirs(media_dir, exist_ok=True)
    html_path = os.path.join(work_dir, "index.html")
    title = engine.title_of(entity)

    count = 0
    tracker = _Progress(progress)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(_HTML_HEAD.format(
            title=html_lib.escape(title),
            exported_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        ))

        async for msg in parser.iter_history(entity):
            kind = engine.media_kind(msg)
            rel_path = None
            if kind and download_media:
                try:
                    abs_path = await parser.download_media_file(msg, media_dir, max_size=MAX_MEDIA_SIZE)
                except Exception:
                    log.exception("ошибка скачивания медиа сообщения %s", msg.id)
                    abs_path = None
                if abs_path:
                    rel_path = f"{MEDIA_SUBDIR}/{os.path.basename(abs_path)}"

            date = msg.date.strftime("%Y-%m-%d %H:%M") if msg.date else "—"
            link = engine.message_link(entity, msg.id)
            text = msg.raw_text or ""

            f.write(f'<div class="msg" id="m{msg.id}">\n')
            f.write('  <div class="meta">'
                     f'<span>{html_lib.escape(date)}</span>'
                     f'<a href="{link or "#"}" target="_blank" rel="noopener">#{msg.id}</a>'
                     '</div>\n')
            media_html = _media_block(kind, rel_path, msg)
            if media_html:
                f.write(f"  {media_html}\n")
            if text:
                f.write(f'  <div class="text">{_linkify(text)}</div>\n')
            f.write("</div>\n")

            count += 1
            await tracker.tick(count)

        f.write(_HTML_TAIL.format(count=count))

    return html_path, count


def zip_dir_split(
    src_dir: str, base_zip_path: str, max_bytes: int = DEFAULT_MAX_BYTES,
) -> list[str]:
    """Пакует work_dir (index.html + media/) в один или несколько zip ≤ max_bytes.

    index.html копируется в каждую часть (он маленький), остальные файлы (медиа)
    раскладываются по частям жадным упаковщиком по размеру. Части нужно распаковать
    в одну и ту же папку — тогда все медиа лягут рядом с одной страницей index.html.
    """
    html_path = os.path.join(src_dir, "index.html")
    html_size = os.path.getsize(html_path) if os.path.isfile(html_path) else 0

    media_dir = os.path.join(src_dir, MEDIA_SUBDIR)
    media_files: list[tuple[str, int]] = []
    if os.path.isdir(media_dir):
        for name in sorted(os.listdir(media_dir)):
            path = os.path.join(media_dir, name)
            if os.path.isfile(path):
                media_files.append((path, os.path.getsize(path)))

    parts: list[list[str]] = [[]]
    sizes = [html_size]
    for path, size in media_files:
        if sizes[-1] + size > max_bytes and parts[-1]:
            parts.append([])
            sizes.append(html_size)
        parts[-1].append(path)
        sizes[-1] += size

    total = len(parts)
    root, _ext = os.path.splitext(base_zip_path)
    part_paths = []
    for i, files in enumerate(parts, start=1):
        zip_path = f"{root}.part{i:03d}of{total:03d}.zip" if total > 1 else f"{root}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            if os.path.isfile(html_path):
                zf.write(html_path, "index.html")
            for path in files:
                zf.write(path, os.path.relpath(path, src_dir))
        part_paths.append(zip_path)
    return part_paths
