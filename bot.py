"""Телеграм-бот управления парсером: ссылки -> ключевые слова -> поиск."""
from __future__ import annotations

import asyncio
import csv
import html
import io
import logging
import os
import tempfile
from datetime import timedelta

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, FSInputFile, KeyboardButton, Message, ReplyKeyboardMarkup

import export
import parser as engine
import storage
from config import Config, load_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logging.getLogger("telethon").setLevel(logging.WARNING)
log = logging.getLogger("bot")

BTN_CHATS = "🔗 Чаты"
BTN_WORDS = "🔑 Ключевые слова"
BTN_SEARCH = "🚀 Запустить поиск"
BTN_MONITOR = "📡 Мониторинг"
BTN_RESULTS = "📄 Результаты"
BTN_EXPORT = "📤 Экспорт CSV"
BTN_HISTORY_JSON = "🗂 История (JSON)"
BTN_HISTORY_HTML = "🖼 История (HTML+медиа)"
BTN_SETTINGS = "⚙️ Настройки"
BUTTONS = {
    BTN_CHATS, BTN_WORDS, BTN_SEARCH, BTN_MONITOR, BTN_RESULTS, BTN_EXPORT,
    BTN_HISTORY_JSON, BTN_HISTORY_HTML, BTN_SETTINGS,
}

MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_CHATS), KeyboardButton(text=BTN_WORDS)],
        [KeyboardButton(text=BTN_SEARCH), KeyboardButton(text=BTN_MONITOR)],
        [KeyboardButton(text=BTN_RESULTS), KeyboardButton(text=BTN_EXPORT)],
        [KeyboardButton(text=BTN_HISTORY_JSON), KeyboardButton(text=BTN_HISTORY_HTML)],
        [KeyboardButton(text=BTN_SETTINGS)],
    ],
    resize_keyboard=True,
)

PREVIEW_LIMIT = 10      # сколько находок показать прямо в чате
TEXT_PREVIEW = 350      # сколько символов текста показывать
MAX_MESSAGE = 3900      # запас до лимита Telegram в 4096 символов

# Обычный текст пользователя (не команда и не кнопка меню) — только он попадает
# в обработчики состояний, иначе состояние перехватывало бы навигацию.
PLAIN_TEXT = F.text & ~F.text.startswith("/") & ~F.text.in_(BUTTONS)

cfg: Config
parser: engine.Parser
bot: Bot
search_lock = asyncio.Lock()
current_task: asyncio.Task | None = None
dp = Dispatcher()


def start_task(coro) -> asyncio.Task:
    """create_task + запоминаем ссылку, чтобы операцию можно было прервать через /cancel."""
    global current_task
    task = asyncio.create_task(coro)
    current_task = task

    def _clear(done: asyncio.Task) -> None:
        global current_task
        if current_task is done:
            current_task = None

    task.add_done_callback(_clear)
    return task


class Flow(StatesGroup):
    chats = State()
    words = State()
    history_json = State()
    history_html = State()


# ------------------------------------------------------------- доступ
class AccessMiddleware(BaseMiddleware):
    def __init__(self, owners: set[int]) -> None:
        self.owners = owners

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if self.owners and (user is None or user.id not in self.owners):
            if isinstance(event, Message):
                await event.answer(
                    f"⛔️ Доступ закрыт.\nВаш Telegram ID: <code>{user.id if user else '?'}</code>\n"
                    "Добавьте его в OWNER_IDS в файле .env."
                )
            return None
        return await handler(event, data)


# ---------------------------------------------------------- настройки
def setting(key: str, default):
    raw = storage.get_setting(key)
    if raw is None:
        return default
    if isinstance(default, bool):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(default, int):
        try:
            return int(raw)
        except ValueError:
            return default
    return raw


def current_settings() -> dict:
    return {
        "days": setting("days", cfg.search_days),
        "limit": setting("limit", cfg.per_chat_limit),
        "mode": setting("mode", cfg.match_mode),
        "scan": setting("scan", cfg.scan_mode),
        "monitor": setting("monitor", False),
    }


HELP = (
    "<b>Парсер чатов Telegram</b>\n\n"
    "1️⃣ <b>Чаты</b> — пришлите ссылки (можно 50 штук сразу, каждая с новой строки).\n"
    "2️⃣ <b>Ключевые слова</b> — через запятую или с новой строки.\n"
    "3️⃣ <b>Запустить поиск</b> — пройду по всем чатам и соберу совпадения.\n"
    "4️⃣ <b>Мониторинг</b> — новые сообщения по словам приходят сразу.\n"
    "5️⃣ <b>История (JSON)</b> / <b>История (HTML+медиа)</b> — вся история одного "
    "публичного канала/чата целиком, без привязки к ключевым словам: JSON — компактный "
    "файл с текстом и метаданными, HTML — читаемая страница с картинками, видео и файлами.\n\n"
    "<b>Команды</b>\n"
    "/chats — список чатов, /delchat N — удалить, /clearchats — очистить\n"
    "/words — список слов, /delword слово, /clearwords\n"
    "/search — поиск, /results — последние находки, /export — CSV\n"
    "/monitor on|off — мониторинг новых сообщений\n"
    "/exporthistory — выгрузка истории чата в JSON\n"
    "/exporthistoryhtml — выгрузка истории чата в HTML+медиа (zip-архив)\n"
    "/cancel — прервать текущий поиск или выгрузку\n"
    "/settings — параметры, /set ключ значение — изменить\n"
    "/clearresults — очистить базу находок"
)


# ------------------------------------------------------------ команды
@dp.message(CommandStart())
@dp.message(Command("help"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(HELP, reply_markup=MENU)


@dp.message(Command("chats"))
@dp.message(F.text == BTN_CHATS)
async def cmd_chats(message: Message, state: FSMContext) -> None:
    await state.set_state(Flow.chats)
    rows = storage.list_chats()
    if not rows:
        await message.answer(
            "Список чатов пуст.\n\nПришлите ссылки — каждая с новой строки:\n"
            "<code>https://t.me/chatname\n@another_chat\nhttps://t.me/+AbCdEf123</code>",
            reply_markup=MENU,
        )
        return
    lines = []
    for row in rows:
        mark = {"ok": "✅", "error": "⚠️"}.get(row["status"], "•")
        name = row["title"] or row["link"]
        line = f"{mark} <b>{row['id']}</b>. {html.escape(str(name))}"
        if row["status"] == "error" and row["error"]:
            line += f"\n     <i>{html.escape(row['error'])}</i>"
        lines.append(line)
    text = (
        f"<b>Чаты ({len(rows)}):</b>\n" + "\n".join(lines) +
        "\n\nПришлите новые ссылки, чтобы добавить.\n/delchat N — удалить, /clearchats — очистить."
    )
    await send_long(message, text)


@dp.message(Command("delchat"))
async def cmd_delchat(message: Message, command: CommandObject) -> None:
    args = (command.args or "").strip()
    if not args.isdigit() or not storage.delete_chat(int(args)):
        await message.answer("Нужен номер чата из /chats, например: <code>/delchat 3</code>")
        return
    await message.answer(f"Чат {args} удалён. Осталось: {storage.count_chats()}")


@dp.message(Command("clearchats"))
async def cmd_clearchats(message: Message) -> None:
    await message.answer(f"Удалено чатов: {storage.clear_chats()}")


@dp.message(Command("words"))
@dp.message(F.text == BTN_WORDS)
async def cmd_words(message: Message, state: FSMContext) -> None:
    await state.set_state(Flow.words)
    words = storage.list_keywords()
    if not words:
        await message.answer(
            "Ключевых слов пока нет.\n\nПришлите их через запятую или с новой строки:\n"
            "<code>купить квартиру, ищу подрядчика, аренда</code>",
            reply_markup=MENU,
        )
        return
    listing = "\n".join(f"• {html.escape(w)}" for w in words)
    await send_long(
        message,
        f"<b>Ключевые слова ({len(words)}):</b>\n{listing}\n\n"
        "Пришлите новые, чтобы добавить.\n/delword слово — удалить, /clearwords — очистить.",
    )


@dp.message(Command("delword"))
async def cmd_delword(message: Message, command: CommandObject) -> None:
    word = (command.args or "").strip()
    if not word or not storage.delete_keyword(word):
        await message.answer("Укажите слово из списка: <code>/delword аренда</code>")
        return
    await message.answer(f"Слово «{html.escape(word)}» удалено.")


@dp.message(Command("clearwords"))
async def cmd_clearwords(message: Message) -> None:
    await message.answer(f"Удалено слов: {storage.clear_keywords()}")


# ------------------------------------------------------------- поиск
@dp.message(Command("search"))
@dp.message(F.text == BTN_SEARCH)
async def cmd_search(message: Message, state: FSMContext) -> None:
    await state.clear()
    if search_lock.locked():
        await message.answer("Поиск уже идёт, дождитесь окончания (или /cancel, чтобы прервать).")
        return
    chats = storage.list_chats()
    words = storage.list_keywords()
    if not chats:
        await message.answer("Сначала добавьте чаты — кнопка «🔗 Чаты».", reply_markup=MENU)
        return
    if not words:
        await message.answer("Сначала добавьте ключевые слова — «🔑 Ключевые слова».", reply_markup=MENU)
        return
    start_task(run_search(message, chats, words))


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    if current_task and not current_task.done():
        current_task.cancel()
        await message.answer("⛔️ Прерываю текущую операцию…", reply_markup=MENU)
    else:
        await message.answer("Сейчас ничего не выполняется.", reply_markup=MENU)


async def run_search(message: Message, chats, words) -> None:
    async with search_lock:
        opts = current_settings()
        since = engine.utc_now() - timedelta(days=opts["days"]) if opts["days"] else None
        status = await message.answer(
            f"🔎 Ищу <b>{len(words)}</b> слов в <b>{len(chats)}</b> чатах "
            f"(глубина: {opts['days'] or '∞'} дн., до {opts['limit']} сообщений на чат)…"
        )
        fresh: list[engine.Hit] = []
        total = 0
        errors: list[str] = []

        for index, chat in enumerate(chats, start=1):
            try:
                entity = await parser.resolve(chat["link"])
            except Exception as exc:
                note = f"{chat['link']} — {exc or exc.__class__.__name__}"
                errors.append(note)
                storage.update_chat(chat["id"], status="error", error=str(exc)[:200])
                await edit_status(status, f"🔎 Чат {index}/{len(chats)}: недоступен")
                continue

            storage.update_chat(
                chat["id"],
                status="ok",
                error=None,
                title=engine.title_of(entity),
                tg_id=engine.chat_id_of(entity),
                username=getattr(entity, "username", None),
            )

            try:
                async for hit in parser.search_chat(
                    entity, words, since, opts["limit"], opts["mode"], opts["scan"]
                ):
                    total += 1
                    if save_hit(hit):
                        fresh.append(hit)
            except Exception as exc:
                log.exception("ошибка при поиске в %s", chat["link"])
                errors.append(f"{chat['link']} — {exc or exc.__class__.__name__}")
                storage.update_chat(chat["id"], status="error", error=str(exc)[:200])

            await edit_status(
                status,
                f"🔎 Чат {index}/{len(chats)}: {html.escape(engine.title_of(entity))}\n"
                f"Совпадений: <b>{total}</b> (новых: {len(fresh)})",
            )
            await asyncio.sleep(cfg.request_delay)

        summary = (
            "✅ <b>Поиск завершён</b>\n"
            f"Чатов обработано: {len(chats) - len(errors)}/{len(chats)}\n"
            f"Совпадений: <b>{total}</b>, новых: <b>{len(fresh)}</b>\n"
            f"Всего в базе: {storage.count_hits()}"
        )
        if errors:
            shown = "\n".join(f"• {html.escape(e)}" for e in errors[:10])
            summary += f"\n\n⚠️ Недоступно чатов: {len(errors)}\n{shown}"
        await edit_status(status, summary)

        for hit in fresh[:PREVIEW_LIMIT]:
            await message.answer(format_hit_obj(hit), disable_web_page_preview=True)
        if len(fresh) > PREVIEW_LIMIT:
            await message.answer(
                f"…и ещё {len(fresh) - PREVIEW_LIMIT}. Полный список — «📤 Экспорт CSV».",
                reply_markup=MENU,
            )


def save_hit(hit: engine.Hit) -> bool:
    return storage.save_hit(
        chat_tg_id=hit.chat_tg_id,
        message_id=hit.message_id,
        keyword=hit.keyword,
        chat_title=hit.chat_title,
        sender=hit.sender,
        text=hit.text,
        date=hit.date.isoformat(sep=" ", timespec="seconds") if hit.date else None,
        link=hit.link,
    )


# -------------------------------------------------------- мониторинг
@dp.message(Command("monitor"))
@dp.message(F.text == BTN_MONITOR)
async def cmd_monitor(message: Message, state: FSMContext) -> None:
    await state.clear()
    arg = ""
    if message.text and message.text.startswith("/monitor"):
        arg = message.text.partition(" ")[2].strip().lower()
    if arg in {"on", "вкл", "1"}:
        enabled = True
    elif arg in {"off", "выкл", "0"}:
        enabled = False
    else:
        enabled = not setting("monitor", False)
    storage.set_setting("monitor", "1" if enabled else "0")
    storage.set_setting("monitor_chat", message.chat.id)
    await message.answer(
        "📡 Мониторинг <b>включён</b> — новые сообщения по ключевым словам буду присылать сюда.\n"
        "Чат должен быть распознан: запустите поиск хотя бы раз, чтобы я знал его ID."
        if enabled
        else "📡 Мониторинг <b>выключен</b>.",
        reply_markup=MENU,
    )


async def on_new_message(event) -> None:
    """Обработчик новых сообщений во всех чатах аккаунта (Telethon)."""
    if not setting("monitor", False):
        return
    if event.chat_id not in storage.monitored_chat_ids():
        return
    text = event.raw_text or ""
    if not text:
        return
    mode = setting("mode", cfg.match_mode)
    norm = engine.normalize_text(text)
    for word in storage.list_keywords():
        if not engine.build_pattern(word, mode).search(norm):
            continue
        chat = await event.get_chat()
        hit = engine.Hit(
            chat_tg_id=event.chat_id,
            chat_title=engine.title_of(chat),
            message_id=event.id,
            keyword=word,
            sender=await engine.sender_name(event.message),
            text=text,
            date=event.message.date,
            link=engine.message_link(chat, event.id),
        )
        if save_hit(hit):
            target = setting("monitor_chat", 0)
            if target:
                await bot.send_message(
                    int(target),
                    "📡 <b>Новое сообщение</b>\n" + format_hit_obj(hit),
                    disable_web_page_preview=True,
                )
        return


# ---------------------------------------------------------- результаты
@dp.message(Command("results"))
@dp.message(F.text == BTN_RESULTS)
async def cmd_results(message: Message, state: FSMContext) -> None:
    await state.clear()
    rows = storage.list_hits(limit=PREVIEW_LIMIT)
    if not rows:
        await message.answer("Находок пока нет. Запустите поиск.", reply_markup=MENU)
        return
    await message.answer(
        f"Всего находок в базе: <b>{storage.count_hits()}</b>. Последние {len(rows)}:",
        reply_markup=MENU,
    )
    for row in rows:
        await message.answer(format_hit(row), disable_web_page_preview=True)


@dp.message(Command("export"))
@dp.message(F.text == BTN_EXPORT)
async def cmd_export(message: Message, state: FSMContext) -> None:
    await state.clear()
    rows = storage.list_hits()
    if not rows:
        await message.answer("Экспортировать нечего — находок нет.", reply_markup=MENU)
        return
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(["Дата", "Чат", "Автор", "Ключевое слово", "Текст", "Ссылка"])
    for row in rows:
        writer.writerow([
            row["date"] or "",
            row["chat_title"] or "",
            row["sender"] or "",
            row["keyword"],
            (row["text"] or "").replace("\n", " "),
            row["link"] or "",
        ])
    data = buffer.getvalue().encode("utf-8-sig")  # BOM, чтобы Excel не сломал кириллицу
    await message.answer_document(
        BufferedInputFile(data, filename="results.csv"),
        caption=f"Найдено сообщений: {len(rows)}",
        reply_markup=MENU,
    )


@dp.message(Command("clearresults"))
async def cmd_clearresults(message: Message) -> None:
    await message.answer(f"Удалено находок: {storage.clear_hits()}")


# --------------------------------------------------- выгрузка истории
@dp.message(Command("exporthistory"))
@dp.message(F.text == BTN_HISTORY_JSON)
async def cmd_export_history_json(message: Message, state: FSMContext) -> None:
    if search_lock.locked():
        await message.answer("Сейчас идёт другая операция (поиск или выгрузка) — дождитесь окончания (или /cancel).")
        return
    await state.set_state(Flow.history_json)
    await message.answer(
        "Пришлите ссылку на <b>публичный</b> канал или чат, историю которого нужно выгрузить "
        "(например <code>https://t.me/durov</code> или <code>@durov</code>).\n\n"
        "Соберу всю доступную историю сообщений в один JSON-файл: дату и время, текст, "
        "ссылки из сообщения, ID сообщений и метаданные вложений (если есть). Для больших "
        "каналов это может занять время.",
        reply_markup=MENU,
    )


@dp.message(Command("exporthistoryhtml"))
@dp.message(F.text == BTN_HISTORY_HTML)
async def cmd_export_history_html(message: Message, state: FSMContext) -> None:
    if search_lock.locked():
        await message.answer("Сейчас идёт другая операция (поиск или выгрузка) — дождитесь окончания (или /cancel).")
        return
    await state.set_state(Flow.history_html)
    await message.answer(
        "Пришлите ссылку на <b>публичный</b> канал или чат, историю которого нужно выгрузить "
        "(например <code>https://t.me/durov</code> или <code>@durov</code>).\n\n"
        "Соберу всю доступную историю в HTML-страницу с картинками, видео, аудио и файлами "
        "(скачаю и упакую вместе с ней в zip-архив). Файлы крупнее 20 МБ не скачиваю — вместо "
        "них будет ссылка на оригинальное сообщение. Для больших каналов это может занять "
        "заметное время и место на диске.",
        reply_markup=MENU,
    )


@dp.message(Flow.history_json, PLAIN_TEXT)
async def do_export_history_json(message: Message, state: FSMContext) -> None:
    await state.clear()
    link = _pick_history_link(message.text or "")
    if link is None:
        await message.answer(
            "Не похоже на ссылку на чат/канал. Попробуйте ещё раз — «🗂 История (JSON)».",
            reply_markup=MENU,
        )
        return
    if search_lock.locked():
        await message.answer("Сейчас идёт другая операция — попробуйте чуть позже (или /cancel, чтобы прервать её).")
        return
    start_task(run_export_json(message, link))


@dp.message(Flow.history_html, PLAIN_TEXT)
async def do_export_history_html(message: Message, state: FSMContext) -> None:
    await state.clear()
    link = _pick_history_link(message.text or "")
    if link is None:
        await message.answer(
            "Не похоже на ссылку на чат/канал. Попробуйте ещё раз — «🖼 История (HTML+медиа)».",
            reply_markup=MENU,
        )
        return
    if search_lock.locked():
        await message.answer("Сейчас идёт другая операция — попробуйте чуть позже (или /cancel, чтобы прервать её).")
        return
    start_task(run_export_html(message, link))


def _pick_history_link(text: str) -> str | None:
    links = engine.extract_links(text)
    link = links[0] if links else text.strip()
    return link if engine.normalize_link(link) else None


async def _resolve_for_export(message: Message, status: Message, link: str):
    try:
        return await parser.resolve(link)
    except Exception as exc:
        await edit_status(status, f"⚠️ Не удалось открыть чат: {exc or exc.__class__.__name__}")
        return None


async def _send_export_file(message: Message, path: str, filename: str, caption: str) -> None:
    try:
        await message.answer_document(
            FSInputFile(path, filename=filename), caption=caption, reply_markup=MENU,
        )
    except Exception as exc:
        log.exception("не удалось отправить файл выгрузки")
        await message.answer(
            f"⚠️ Не удалось отправить файл: {exc or exc.__class__.__name__}\n"
            "Возможно, превышен лимит Telegram на документы для ботов (~50 МБ).",
            reply_markup=MENU,
        )


async def _send_export_parts(message: Message, paths: list[str], base_caption: str) -> None:
    """Отправляет один или несколько файлов подряд; для нескольких — с номером части в подписи."""
    total = len(paths)
    for i, path in enumerate(paths, start=1):
        caption = base_caption if total == 1 else f"{base_caption} Часть {i}/{total}."
        await _send_export_file(message, path, os.path.basename(path), caption)
        if i < total:
            await asyncio.sleep(1)  # не долбить Telegram сериями документов без паузы


def _finalize_json_parts(part_paths: list[str], stem) -> list[str]:
    """Переименовывает временные .partNNN-файлы в финальные history_<stem>[.partXofY].json."""
    total = len(part_paths)
    result = []
    for i, path in enumerate(part_paths, start=1):
        suffix = f".part{i:03d}of{total:03d}" if total > 1 else ""
        new_path = os.path.join(os.path.dirname(path), f"history_{stem}{suffix}.json")
        os.replace(path, new_path)
        result.append(new_path)
    return result


async def run_export_json(message: Message, link: str) -> None:
    async with search_lock:
        status = await message.answer(f"📥 Подключаюсь к {html.escape(link)}…")
        entity = await _resolve_for_export(message, status, link)
        if entity is None:
            return
        title = engine.title_of(entity)

        with tempfile.TemporaryDirectory(prefix="tgexport_") as tmp:
            out_path = os.path.join(tmp, "history.json")

            async def progress(count: int) -> None:
                await edit_status(status, f"📥 {html.escape(title)}: выгружено {count} сообщений…")

            try:
                part_paths, count = await export.export_json(
                    parser, entity, out_path, progress=progress,
                    max_bytes=cfg.max_upload_mb * 1024 * 1024,
                )
            except Exception as exc:
                log.exception("ошибка выгрузки истории (json) %s", link)
                await edit_status(status, f"⚠️ Ошибка при выгрузке: {exc or exc.__class__.__name__}")
                return

            total = len(part_paths)
            note = f", файлов: {total}" if total > 1 else ""
            await edit_status(status, f"✅ Готово: {count} сообщений{note}. Отправляю…")
            stem = getattr(entity, "username", None) or engine.chat_id_of(entity)
            final_paths = _finalize_json_parts(part_paths, stem)
            await _send_export_parts(message, final_paths, f"История «{title}»: {count} сообщений.")


async def run_export_html(message: Message, link: str) -> None:
    async with search_lock:
        status = await message.answer(f"📥 Подключаюсь к {html.escape(link)}…")
        entity = await _resolve_for_export(message, status, link)
        if entity is None:
            return
        title = engine.title_of(entity)

        with tempfile.TemporaryDirectory(prefix="tgexport_") as tmp:

            async def progress(count: int) -> None:
                await edit_status(
                    status, f"📥 {html.escape(title)}: выгружено {count} сообщений (со вложениями)…"
                )

            try:
                _html_path, count = await export.export_html(parser, entity, tmp, progress=progress)
            except Exception as exc:
                log.exception("ошибка выгрузки истории (html) %s", link)
                await edit_status(status, f"⚠️ Ошибка при выгрузке: {exc or exc.__class__.__name__}")
                return

            await edit_status(status, f"📦 Собираю архив ({count} сообщений)…")
            stem = getattr(entity, "username", None) or engine.chat_id_of(entity)
            zip_base = os.path.join(tmp, f"history_{stem}.zip")
            part_paths = export.zip_dir_split(tmp, zip_base, max_bytes=cfg.max_upload_mb * 1024 * 1024)

            total = len(part_paths)
            note = f" ({total} файла(ов))" if total > 1 else ""
            await edit_status(status, f"✅ Готово{note}. Отправляю…")
            caption = f"История «{title}»: {count} сообщений, HTML + медиа (index.html внутри архива)."
            if total > 1:
                caption += " Распакуйте ВСЕ части в одну папку — медиа и страница соберутся вместе."
            await _send_export_parts(message, part_paths, caption)


# ----------------------------------------------------------- настройки
@dp.message(Command("settings"))
@dp.message(F.text == BTN_SETTINGS)
async def cmd_settings(message: Message, state: FSMContext) -> None:
    await state.clear()
    opts = current_settings()
    await message.answer(
        "<b>Настройки поиска</b>\n"
        f"• <code>days</code> = {opts['days']} — за сколько дней искать (0 = без ограничения)\n"
        f"• <code>limit</code> = {opts['limit']} — максимум сообщений на чат\n"
        f"• <code>mode</code> = {opts['mode']} — smart (слово с любым окончанием), "
        "exact (точное слово), substring (любое вхождение)\n"
        f"• <code>scan</code> = {'on' if opts['scan'] else 'off'} — off: быстрый поиск силами Telegram, "
        "on: полное сканирование истории\n"
        f"• <code>monitor</code> = {'on' if opts['monitor'] else 'off'}\n\n"
        "Изменить: <code>/set days 60</code>, <code>/set limit 1000</code>, "
        "<code>/set mode exact</code>, <code>/set scan on</code>",
        reply_markup=MENU,
    )


@dp.message(Command("set"))
async def cmd_set(message: Message, command: CommandObject) -> None:
    parts = (command.args or "").split()
    if len(parts) != 2:
        await message.answer("Формат: <code>/set ключ значение</code>")
        return
    key, value = parts[0].lower(), parts[1].lower()
    if key in {"days", "limit"}:
        if not value.isdigit():
            await message.answer("Значение должно быть числом.")
            return
        storage.set_setting(key, int(value))
    elif key == "mode":
        if value not in {"smart", "exact", "substring"}:
            await message.answer("Допустимо: smart, exact, substring.")
            return
        storage.set_setting("mode", value)
    elif key in {"scan", "monitor"}:
        storage.set_setting(key, "1" if value in {"on", "1", "true", "вкл"} else "0")
    else:
        await message.answer("Неизвестный параметр. Доступны: days, limit, mode, scan, monitor.")
        return
    await message.answer(f"✅ <code>{key}</code> = <code>{value}</code>")


# ------------------------------------- ввод данных (состояния) и фолбэк
@dp.message(Flow.chats, PLAIN_TEXT)
async def add_chats(message: Message) -> None:
    links = engine.extract_links(message.text or "")
    if not links:
        await message.answer(
            "Не нашёл ни одной ссылки на чат. Пример: <code>https://t.me/durov</code>"
        )
        return
    added = sum(1 for link in links if storage.add_chat(link))
    await message.answer(
        f"Добавлено: <b>{added}</b>, повторов: {len(links) - added}.\n"
        f"Всего чатов в списке: <b>{storage.count_chats()}</b>.",
        reply_markup=MENU,
    )


@dp.message(Flow.words, PLAIN_TEXT)
async def add_words(message: Message) -> None:
    raw = (message.text or "").replace("\n", ",")
    words = [w.strip() for w in raw.split(",") if w.strip()]
    if not words:
        await message.answer("Пустой список. Пришлите слова через запятую.")
        return
    added = sum(1 for w in words if storage.add_keyword(w))
    await message.answer(
        f"Добавлено слов: <b>{added}</b>, повторов: {len(words) - added}.\n"
        f"Всего: <b>{len(storage.list_keywords())}</b>.",
        reply_markup=MENU,
    )


@dp.message(PLAIN_TEXT)
async def fallback(message: Message) -> None:
    if engine.extract_links(message.text or ""):
        await add_chats(message)
        return
    await message.answer("Не понял сообщение. /help — список возможностей.", reply_markup=MENU)


# ------------------------------------------------------------ helpers
def format_hit(row) -> str:
    return format_parts(
        row["date"], row["chat_title"], row["sender"], row["keyword"], row["text"], row["link"]
    )


def format_hit_obj(hit: engine.Hit) -> str:
    date = hit.date.strftime("%Y-%m-%d %H:%M") if hit.date else ""
    return format_parts(date, hit.chat_title, hit.sender, hit.keyword, hit.text, hit.link)


def format_parts(date, chat_title, sender, keyword, text, link) -> str:
    text = (text or "").strip()
    if len(text) > TEXT_PREVIEW:
        text = text[:TEXT_PREVIEW] + "…"
    parts = [
        f"🔑 <b>{html.escape(str(keyword))}</b>",
        f"💬 {html.escape(str(chat_title or '—'))} · 👤 {html.escape(str(sender or '—'))}",
        f"🕒 {html.escape(str(date or '—'))}",
        "",
        html.escape(text),
    ]
    if link:
        parts.append(f"\n🔗 {link}")
    return "\n".join(parts)


async def send_long(message: Message, text: str) -> None:
    """Режет текст по строкам: Telegram не принимает больше 4096 символов."""
    chunk = ""
    for line in text.split("\n"):
        line = line[:MAX_MESSAGE]
        if len(chunk) + len(line) + 1 > MAX_MESSAGE:
            await message.answer(chunk, reply_markup=MENU)
            chunk = ""
        chunk = f"{chunk}\n{line}" if chunk else line
    if chunk:
        await message.answer(chunk, reply_markup=MENU)


async def edit_status(status: Message, text: str) -> None:
    try:
        await status.edit_text(text)
    except Exception:  # текст не изменился или сообщение устарело — не важно
        pass


# --------------------------------------------------------------- main
async def main() -> None:
    global cfg, parser, bot
    cfg = load_config()
    storage.init(cfg.db_path)

    parser = engine.Parser(cfg)
    account = await parser.start()
    log.info("Telethon авторизован как %s", account)

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp.message.middleware(AccessMiddleware(cfg.owner_ids))
    parser.add_monitor(on_new_message)

    me = await bot.get_me()
    log.info("Бот запущен: @%s", me.username)
    try:
        await asyncio.gather(
            dp.start_polling(bot),
            parser.client.run_until_disconnected(),
        )
    finally:
        await parser.stop()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Остановлено.")
    except SystemExit as exc:
        if isinstance(exc.code, str):     # понятная подсказка вместо трейсбека
            print(exc.code)
            raise SystemExit(1) from None
        raise
