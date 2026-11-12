import asyncio
import logging
from django.core.management.base import BaseCommand
from django.conf import settings
from aiogram import Bot, Dispatcher
from bot.handlers import router


class Command(BaseCommand):
    help = "Запускає Telegram-бота в режимі Polling"

    def handle(self, *args, **options):
        logging.basicConfig(level=logging.INFO)
        # Запускаємо асинхронний цикл подій
        asyncio.run(self.start_bot())

    async def start_bot(self):
        # Токен варто зберігати у settings.py (через змінні оточення .env)
        bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
        dp = Dispatcher()

        # Підключаємо роутер з обробниками
        dp.include_router(router)

        self.stdout.write(self.style.SUCCESS("Бот успішно запущений!"))

        # Видаляємо старі вебхуки (якщо були) і запускаємо Polling
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
