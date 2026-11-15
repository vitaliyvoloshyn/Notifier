import logging

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message
from asgiref.sync import sync_to_async

from bot.keyboards import defer_keyboard, response_keyboard
from reminders.models import Reminder
from reminders.responses import (
    ResponseActionError,
    apply_response_action,
    get_controller_telegram_ids,
)
from users.models import TelegramUser

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message):
    tg_user = message.from_user
    full_name = f"{tg_user.first_name or ''} {tg_user.last_name or ''}".strip()[:150]
    greeting_name = full_name or "Друже"

    user, created = await TelegramUser.objects.aupdate_or_create(
        telegram_id=tg_user.id,
        defaults={
            "username": tg_user.username,
            "full_name": full_name,
            "is_active": True,
        },
    )

    if created:
        user.set_unusable_password()
        await user.asave(update_fields=["password"])
        await message.answer(
            f"Привіт, {greeting_name}! Ви успішно зареєстровані. "
            "Надішліть текст та час, щоб створити перше нагадування."
        )
    else:
        await message.answer(f"З поверненням, {greeting_name}!")


@router.callback_query(F.data.startswith("rem:"))
async def reminder_response_callback(callback: CallbackQuery):
    """Обробляє кнопки лише для власника відповіді та актуального повідомлення."""
    try:
        _, raw_id, command = callback.data.split(":", 2)
        response_id = int(raw_id)
    except (AttributeError, TypeError, ValueError):
        await callback.answer("Некоректна команда.", show_alert=True)
        return

    if callback.message is None:
        await callback.answer("Повідомлення недоступне.", show_alert=True)
        return

    if command == "choose":
        await callback.message.edit_reply_markup(reply_markup=defer_keyboard(response_id))
        await callback.answer("Оберіть час відтермінування.")
        return
    if command == "back":
        await callback.message.edit_reply_markup(reply_markup=response_keyboard(response_id))
        await callback.answer()
        return

    if command == "completed":
        action, minutes, label = "completed", None, "виконано"
    elif command == "rejected":
        action, minutes, label = "rejected", None, "відхилено"
    elif command == "defer30":
        action, minutes, label = "defer", 30, "відтерміновано на 30 хвилин"
    elif command.startswith("d") and command[1:].isdigit():
        action, minutes = "defer", int(command[1:])
        label = f"відтерміновано на {minutes} хвилин"
    else:
        await callback.answer("Невідома дія.", show_alert=True)
        return

    try:
        response, event = await sync_to_async(apply_response_action, thread_sensitive=True)(
            response_id=response_id,
            actor_telegram_id=callback.from_user.id,
            action=action,
            defer_minutes=minutes,
            telegram_message_id=callback.message.message_id,
        )
    except ResponseActionError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    except Exception:
        await callback.answer("Не вдалося зберегти відповідь. Спробуйте ще раз.", show_alert=True)
        logging.getLogger(__name__).exception("Помилка обробки callback нагадування %s", response_id)
        return

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Відповідь збережено.")

    reminder = response.occurrence.reminder
    should_notify = (
        reminder.notification_policy == Reminder.NotificationPolicy.ALL_ACTIONS
        or action == "rejected"
    )
    if should_notify:
        controller_ids = await sync_to_async(get_controller_telegram_ids, thread_sensitive=True)(reminder.pk)
        actor_name = response.assignee.full_name or response.assignee.username or str(response.assignee.telegram_id)
        if action == "defer":
            status_text = f"відтермінував(ла) «{reminder.title}» до {event.deferred_until:%d.%m %H:%M} UTC"
        else:
            status_text = f"{label} нагадування «{reminder.title}»"
        for telegram_id in controller_ids:
            try:
                await callback.bot.send_message(telegram_id, f"ℹ️ {actor_name} {status_text}.")
            except Exception:
                # Невдале сповіщення контролера не скасовує вже збережену відповідь.
                logging.getLogger(__name__).exception("Не вдалося сповістити контролера %s", telegram_id)
