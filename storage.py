"""Хранилище на SQLite: чаты, ключевые слова, найденные сообщения, настройки."""
from __future__ import annotations

import sqlite3
import threading
from typing import Any, Iterable

_conn: sqlite3.Connection | None = None
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    link       TEXT NOT NULL UNIQUE,
    title      TEXT,
    tg_id      INTEGER,
    username   TEXT,
    status     TEXT DEFAULT 'new',
    error      TEXT,
    added_at   TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS keywords (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    word     TEXT NOT NULL UNIQUE,
    added_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS hits (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_tg_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    keyword    TEXT NOT NULL,
    chat_title TEXT,
    sender     TEXT,
    text       TEXT,
    date       TEXT,
    link       TEXT,
    found_at   TEXT DEFAULT (datetime('now')),
    UNIQUE (chat_tg_id, message_id, keyword)
);

CREATE INDEX IF NOT EXISTS idx_hits_found ON hits (found_at DESC);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def init(db_path: str) -> None:
    global _conn
    _conn = sqlite3.connect(db_path, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    with _lock:
        _conn.executescript(SCHEMA)
        _conn.commit()


def _db() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("storage.init() не вызван")
    return _conn


def _exec(sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
    with _lock:
        cur = _db().execute(sql, tuple(params))
        _db().commit()
        return cur


def _all(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    with _lock:
        return list(_db().execute(sql, tuple(params)).fetchall())


# ---------------------------------------------------------------- чаты
def add_chat(link: str) -> bool:
    """True, если ссылка добавлена; False, если она уже была в списке."""
    cur = _exec("INSERT OR IGNORE INTO chats (link) VALUES (?)", (link,))
    return cur.rowcount > 0


def list_chats() -> list[sqlite3.Row]:
    return _all("SELECT * FROM chats ORDER BY id")


def count_chats() -> int:
    return _all("SELECT COUNT(*) AS n FROM chats")[0]["n"]


def delete_chat(chat_id: int) -> bool:
    return _exec("DELETE FROM chats WHERE id = ?", (chat_id,)).rowcount > 0


def clear_chats() -> int:
    return _exec("DELETE FROM chats", ()).rowcount


def update_chat(chat_id: int, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    _exec(f"UPDATE chats SET {cols} WHERE id = ?", (*fields.values(), chat_id))


def monitored_chat_ids() -> set[int]:
    rows = _all("SELECT tg_id FROM chats WHERE tg_id IS NOT NULL")
    return {r["tg_id"] for r in rows}


# ------------------------------------------------------ ключевые слова
def add_keyword(word: str) -> bool:
    """True, если слово добавлено. Сравнение без учёта регистра (в т.ч. кириллица)."""
    word = word.strip()
    if not word or _find_keyword(word) is not None:
        return False
    return _exec("INSERT INTO keywords (word) VALUES (?)", (word,)).rowcount > 0


def list_keywords() -> list[str]:
    return [r["word"] for r in _all("SELECT word FROM keywords ORDER BY id")]


def _find_keyword(word: str) -> int | None:
    """SQLite lower() не умеет в кириллицу, поэтому сравниваем на стороне Python."""
    target = word.strip().lower()
    for row in _all("SELECT id, word FROM keywords"):
        if row["word"].strip().lower() == target:
            return row["id"]
    return None


def delete_keyword(word: str) -> bool:
    found = _find_keyword(word)
    if found is None:
        return False
    return _exec("DELETE FROM keywords WHERE id = ?", (found,)).rowcount > 0


def clear_keywords() -> int:
    return _exec("DELETE FROM keywords", ()).rowcount


# ------------------------------------------------------------ находки
def save_hit(
    *,
    chat_tg_id: int,
    message_id: int,
    keyword: str,
    chat_title: str | None,
    sender: str | None,
    text: str | None,
    date: str | None,
    link: str | None,
) -> bool:
    """True, если сообщение новое (дубли отсекаются)."""
    cur = _exec(
        """INSERT OR IGNORE INTO hits
           (chat_tg_id, message_id, keyword, chat_title, sender, text, date, link)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (chat_tg_id, message_id, keyword, chat_title, sender, text, date, link),
    )
    return cur.rowcount > 0


def list_hits(limit: int | None = None, newest_first: bool = True) -> list[sqlite3.Row]:
    order = "DESC" if newest_first else "ASC"
    sql = f"SELECT * FROM hits ORDER BY date {order}, id {order}"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return _all(sql)


def count_hits() -> int:
    return _all("SELECT COUNT(*) AS n FROM hits")[0]["n"]


def clear_hits() -> int:
    return _exec("DELETE FROM hits", ()).rowcount


# ----------------------------------------------------------- настройки
def get_setting(key: str, default: str | None = None) -> str | None:
    rows = _all("SELECT value FROM settings WHERE key = ?", (key,))
    return rows[0]["value"] if rows else default


def set_setting(key: str, value: Any) -> None:
    _exec(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
