import asyncio
import logging
from django.core.management.base import BaseCommand
from django.conf import settings
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeDefault
from bot.handlers import router


async def set_bot_commands(bot: Bot):
  commands = [
      # BotCommand(command="start", description="🚀 Показати меню / Старт"),
      BotCommand(command="new_reminder", description="➕ Створити нагадування"),
      BotCommand(command="reminders", description="📋 Список нагадувань"),
      BotCommand(command="timezone", description="🕒 Часовий пояс"),
  ]
  await bot.set_my_commands(commands, scope=BotCommandScopeDefault())

class Command(BaseCommand):
    help = "Запускає Telegram-бота в режимі Polling"

    def handle(self, *args, **options):
        logging.basicConfig(level=logging.INFO)
        # Запускаємо асинхронний цикл подій
        try:
            asyncio.run(self.start_bot())
        except KeyboardInterrupt:
            self.stdout.write(self.style.SUCCESS("Бот зупинений!"))



    async def start_bot(self):
        # Токен варто зберігати у settings.py (через змінні оточення .env)
        bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
        dp = Dispatcher(storage=MemoryStorage())

        # Підключаємо роутер з обробниками
        dp.include_router(router)

        self.stdout.write(self.style.SUCCESS("Бот успішно запущений!"))
        try:
            # Видаляємо старі вебхуки (якщо були) і запускаємо Polling
            await bot.delete_webhook(drop_pending_updates=True)
            await set_bot_commands(bot)
            await dp.start_polling(bot)
        finally:
            await bot.session.close()
