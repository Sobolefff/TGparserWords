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
import zipfile
from datetime import datetime, timezone
from typing import Awaitable, Callable

import parser as engine

log = logging.getLogger("export")

ProgressCB = Callable[[int], Awaitable[None]] | None
PROGRESS_EVERY = 200

MEDIA_SUBDIR = "media"
MAX_MEDIA_SIZE = 20 * 1024 * 1024  # 20 МБ — крупные файлы не тянем, оставляем ссылку на оригинал

_URL_RE = re.compile(r"https?://\S+")


def _chat_meta(entity) -> dict:
    return {
        "title": engine.title_of(entity),
        "username": getattr(entity, "username", None),
        "id": engine.chat_id_of(entity),
    }


# --------------------------------------------------------------- JSON
async def export_json(parser, entity, out_path: str, progress: ProgressCB = None) -> int:
    """Пишет JSON потоково в out_path, отдаёт число выгруженных сообщений."""
    count = 0
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("{\n")
        f.write(f'  "chat": {json.dumps(_chat_meta(entity), ensure_ascii=False)},\n')
        f.write(f'  "exported_at": {json.dumps(datetime.now(timezone.utc).isoformat())},\n')
        f.write('  "messages": [\n')
        first = True
        async for item in parser.export_history(entity):
            if not first:
                f.write(",\n")
            f.write("    " + json.dumps(item, ensure_ascii=False))
            first = False
            count += 1
            if progress and count % PROGRESS_EVERY == 0:
                await progress(count)
        f.write("\n  ],\n")
        f.write(f'  "messages_count": {count}\n')
        f.write("}\n")
    return count


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
            if progress and count % PROGRESS_EVERY == 0:
                await progress(count)

        f.write(_HTML_TAIL.format(count=count))

    return html_path, count


def zip_dir(src_dir: str, zip_path: str, exclude: set[str] | None = None) -> None:
    """Упаковывает содержимое src_dir в zip_path (пути внутри архива — относительные)."""
    exclude = exclude or set()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(src_dir):
            for name in files:
                if name in exclude:
                    continue
                abs_path = os.path.join(root, name)
                rel_path = os.path.relpath(abs_path, src_dir)
                zf.write(abs_path, rel_path)
