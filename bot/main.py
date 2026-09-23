"""Точка входа: python main.py"""
import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from config import load_config, load_content
from core import Services
from db import DB
from handlers import admin, router
from scheduler import scheduler_loop

COMMANDS = [
    BotCommand(command="start", description="Начать"),
    BotCommand(command="subscribe", description="Оформить подписку"),
    BotCommand(command="status", description="Моя подписка"),
    BotCommand(command="remind", description="Ежедневное напоминание"),
    BotCommand(command="terms", description="Условия"),
    BotCommand(command="paysupport", description="Вопросы по оплате"),
]


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    content = load_content(cfg.content_path)

    db = DB(cfg.db_path)
    await db.connect()
    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    svc = Services(bot, db, cfg, content)

    dp = Dispatcher(svc=svc)
    admin.message.filter(F.from_user.id.in_(cfg.admin_ids))
    dp.include_routers(admin, router)

    await bot.set_my_commands(COMMANDS)
    task = asyncio.create_task(scheduler_loop(svc))
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
