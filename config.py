"""Конфигурация парсера: читается из переменных окружения / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on", "да"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


@dataclass
class Config:
    api_id: int
    api_hash: str
    bot_token: str
    owner_ids: set[int] = field(default_factory=set)
    session_name: str = "parser"
    db_path: str = "parser.db"
    search_days: int = 30
    per_chat_limit: int = 500
    match_mode: str = "smart"
    scan_mode: bool = False
    auto_join: bool = False
    request_delay: float = 0.7
    max_flood_wait: int = 120


def load_config(require_bot: bool = True) -> Config:
    api_id = _int("API_ID", 0)
    api_hash = (os.getenv("API_HASH") or "").strip()
    bot_token = (os.getenv("BOT_TOKEN") or "").strip()

    missing = []
    if not api_id:
        missing.append("API_ID")
    if not api_hash:
        missing.append("API_HASH")
    if require_bot and not bot_token:
        missing.append("BOT_TOKEN")
    if missing:
        raise SystemExit(
            "Не заданы переменные окружения: "
            + ", ".join(missing)
            + ".\nСкопируйте .env.example в .env и заполните значения."
        )

    owners: set[int] = set()
    for chunk in (os.getenv("OWNER_IDS") or "").replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk.lstrip("-").isdigit():
            owners.add(int(chunk))

    mode = (os.getenv("MATCH_MODE") or "smart").strip().lower()
    if mode not in {"smart", "exact", "substring"}:
        mode = "smart"

    return Config(
        api_id=api_id,
        api_hash=api_hash,
        bot_token=bot_token,
        owner_ids=owners,
        session_name=(os.getenv("SESSION_NAME") or "parser").strip(),
        db_path=(os.getenv("DB_PATH") or "parser.db").strip(),
        search_days=_int("SEARCH_DAYS", 30),
        per_chat_limit=_int("PER_CHAT_LIMIT", 500),
        match_mode=mode,
        scan_mode=_bool("SCAN_MODE", False),
        auto_join=_bool("AUTO_JOIN", False),
        request_delay=_float("REQUEST_DELAY", 0.7),
        max_flood_wait=_int("MAX_FLOOD_WAIT", 120),
    )
