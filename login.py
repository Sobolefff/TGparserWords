"""Одноразовая авторизация Telegram-аккаунта: создаёт файл сессии для парсера.

Запуск: python login.py
Понадобится номер телефона, код из Telegram и пароль 2FA (если включён).
"""
from __future__ import annotations

import asyncio

from telethon import TelegramClient

from config import load_config


async def main() -> None:
    cfg = load_config(require_bot=False)
    client = TelegramClient(cfg.session_name, cfg.api_id, cfg.api_hash)
    await client.start()
    me = await client.get_me()
    name = f"@{me.username}" if me.username else (me.first_name or str(me.id))
    print(f"\nГотово. Авторизован как {name}.")
    print(f"Файл сессии: {cfg.session_name}.session (никому не передавайте его).")
    print("Теперь можно запускать бота: python bot.py")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
