from unittest.mock import AsyncMock, MagicMock
from django.test import TestCase
from users.models import TelegramUser
from bot.handlers import cmd_start  # вкажіть правильний шлях імпорту


class StartHandlerTest(TestCase):
    async def test_user_registration_and_return(self):
        # Імітуємо об'єкт повідомлення від Telegram
        message = MagicMock()
        message.from_user.id = 123456789
        message.from_user.username = "test_user"
        message.from_user.first_name = "Іван"
        message.from_user.last_name = "Франко"
        message.answer = AsyncMock()

        # Перший виклик /start (реєстрація)
        await cmd_start(message)

        self.assertEqual(await TelegramUser.objects.acount(), 1)
        user = await TelegramUser.objects.aget(telegram_id=123456789)
        self.assertEqual(user.full_name, "Іван Франко")
        self.assertFalse(user.has_usable_password())

        # Другий виклик /start (повернення)
        await cmd_start(message)

        # Перевіряємо, що дублікат не створився
        self.assertEqual(await TelegramUser.objects.acount(), 1)
        self.assertIn("З поверненням", message.answer.call_args[0][0])
