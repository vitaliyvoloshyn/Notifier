from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message
from users.models import TelegramUser

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message):
    tg_user = message.from_user
    full_name = f"{tg_user.first_name or ''} {tg_user.last_name or ''}".strip()[:150]
    greeting_name = full_name or 'Друже'

    # Шукаємо за telegram_id; оновлюємо або створюємо з даними з defaults
    user, created = await TelegramUser.objects.aupdate_or_create(
        telegram_id=tg_user.id,
        defaults={
            'username': tg_user.username,
            'full_name': full_name,
            'is_active': True,
        }
    )

    if created:
        # Встановлюємо не валідний пароль для щойно створеного юзера
        user.set_unusable_password()
        await user.asave(update_fields=['password'])

        await message.answer(
            f"Привіт, {greeting_name}! Ви успішно зареєстровані. "
            "Надішліть текст та час, щоб створити перше нагадування."
        )
    else:
        await message.answer(f"З поверненням, {greeting_name}!")

