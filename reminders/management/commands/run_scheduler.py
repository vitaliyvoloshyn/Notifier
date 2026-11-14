import asyncio
import logging

from aiogram import Bot
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from reminders.dispatch import dispatch_due_occurrences


class Command(BaseCommand):
    help = "Запускає планувальник доставки Telegram-нагадувань"

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=15, help="Інтервал опитування в секундах")
        parser.add_argument("--batch-size", type=int, default=50, help="Максимум спрацювань за цикл")
        parser.add_argument("--once", action="store_true", help="Виконати один цикл і завершитися")

    def handle(self, *args, **options):
        token = getattr(settings, "TELEGRAM_BOT_TOKEN", None)
        if not token:
            raise CommandError("Не задано TELEGRAM_BOT_TOKEN у змінних оточення.")
        if options["interval"] < 1 or options["batch_size"] < 1:
            raise CommandError("interval і batch-size мають бути додатними числами.")
        try:
            asyncio.run(self.run_scheduler(token, options["interval"], options["batch_size"], options["once"]))
        except KeyboardInterrupt:
            self.stdout.write("Планувальник зупинено.")

    async def run_scheduler(self, token, interval, batch_size, once):
        logging.basicConfig(level=logging.INFO)
        async with Bot(token=token) as bot:
            self.stdout.write(self.style.SUCCESS("Планувальник запущено."))
            while True:
                stats = await dispatch_due_occurrences(bot, limit=batch_size)
                if stats["occurrences"]:
                    self.stdout.write(
                        f"Оброблено: {stats['occurrences']}; доставлено: {stats['sent']}; помилок: {stats['failed']}"
                    )
                if once:
                    break
                await asyncio.sleep(interval)
