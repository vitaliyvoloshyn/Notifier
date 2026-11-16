"""Telegram handlers: reminder creation wizard and response actions."""

import logging
import math
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from asgiref.sync import sync_to_async
from django.core.exceptions import ValidationError
from django.utils import timezone as django_timezone

from bot.keyboards import (
    defer_keyboard,
    response_keyboard,
    reminders_menu_keyboard,
    reminders_list_keyboard,
    reminder_detail_keyboard,
    delete_confirmation_keyboard, main_reply_keyboard,
)
from reminders.models import Reminder, ReminderResponse
from reminders.controls import (
    list_reminders, get_reminder_detail, set_reminder_active,
    rename_reminder, delete_reminder,
)
from reminders.responses import (
    ResponseActionError,
    apply_response_action,
    get_controller_telegram_ids,
)
from reminders.services import create_reminder, get_zone, normalize_first_datetime
from users.models import TelegramUser

logger = logging.getLogger(__name__)
router = Router()


class ReminderCreation(StatesGroup):
    title = State()
    first_datetime = State()
    recurrence = State()
    interval = State()
    assignees = State()
    controllers = State()


class CustomDefer(StatesGroup):
    waiting_datetime = State()


class ReminderManagement(StatesGroup):
    rename_title = State()


def _parse_local_datetime(raw: str) -> datetime:
    """Accept Ukrainian-friendly date/time formats; return a naive wall time."""
    value = raw.strip()
    for fmt in ("%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M", "%d.%m.%Y о %H:%M"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValueError("Введіть дату й час у форматі ДД.ММ.РРРР ГГ:ХХ, наприклад 18.11.2026 09:30.")


def _parse_recurrence(raw: str) -> str | None:
    value = raw.strip().lower()
    aliases = {
        "ні": Reminder.RecurrenceType.NONE,
        "ні, одноразове": Reminder.RecurrenceType.NONE,
        "одноразове": Reminder.RecurrenceType.NONE,
        "0": Reminder.RecurrenceType.NONE,
        "щодня": Reminder.RecurrenceType.DAILY,
        "день": Reminder.RecurrenceType.DAILY,
        "щотижня": Reminder.RecurrenceType.WEEKLY,
        "тиждень": Reminder.RecurrenceType.WEEKLY,
        "щомісяця": Reminder.RecurrenceType.MONTHLY,
        "місяць": Reminder.RecurrenceType.MONTHLY,
        "щороку": Reminder.RecurrenceType.YEARLY,
        "рік": Reminder.RecurrenceType.YEARLY,
    }
    return aliases.get(value)


def _resolve_users(raw: str) -> tuple[list[TelegramUser], list[str]]:
    """Resolve comma/space-separated Telegram IDs or usernames of bot users."""
    tokens = [part.strip().lstrip("@") for part in raw.replace(";", ",").split(",") if part.strip()]
    users, missing = [], []
    for token in tokens:
        query = TelegramUser.objects.filter(telegram_id=int(token)) if token.isdigit() else TelegramUser.objects.filter(
            username__iexact=token)
        user = query.first()
        if user is None or not user.is_active:
            missing.append(token)
        elif user.pk not in {item.pk for item in users}:
            users.append(user)
    return users, missing


def _get_response_for_custom_defer(response_id: int, telegram_id: int):
    return (
        ReminderResponse.objects.select_related("assignee", "occurrence__reminder")
        .filter(
            pk=response_id,
            assignee__telegram_id=telegram_id,
            status=ReminderResponse.Status.PENDING,
            delivery_status=ReminderResponse.DeliveryStatus.SENT,
        )
        .first()
    )




@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    tg_user = message.from_user
    full_name = f"{tg_user.first_name or ''} {tg_user.last_name or ''}".strip()[:150]
    greeting_name = full_name or "Друже"
    user, created = await TelegramUser.objects.aupdate_or_create(
        telegram_id=tg_user.id,
        defaults={"username": tg_user.username, "full_name": full_name, "is_active": True},
    )
    if created:
        user.set_unusable_password()
        await user.asave(update_fields=["password"])
        await message.answer(
            f"Привіт, {greeting_name}! Ви зареєстровані.\n",
            # "Створити нагадування: /new_reminder\n"
            # "Керування нагадуваннями: /reminders\n"
            # "Налаштувати часовий пояс: /timezone Europe/Kyiv"
            reply_markup=main_reply_keyboard(),
        )
    else:
        await message.answer(
            f"З поверненням, {greeting_name}!\n",
            # "Створити нагадування: /new_reminder\n"
            # "Керування нагадуваннями: /reminders"
            reply_markup=main_reply_keyboard(),
        )


@router.message(Command("cancel"))
async def cancel_dialog(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Дію скасовано.")


@router.message(Command("timezone"))
async def set_timezone(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Вкажіть часовий пояс у форматі IANA, наприклад: /timezone Europe/Kyiv")
        return
    zone_name = parts[1].strip()
    try:
        get_zone(zone_name)
    except ValidationError:
        await message.answer("Не знаю такого часового поясу. Приклад правильного значення: Europe/Kyiv.")
        return
    user = await TelegramUser.objects.filter(telegram_id=message.from_user.id).afirst()
    if user is None:
        await message.answer("Спочатку виконайте /start.")
        return
    user.timezone = zone_name
    await user.asave(update_fields=["timezone"])
    await message.answer(f"Часовий пояс збережено: {zone_name}. Він застосовуватиметься до нових нагадувань.")


@router.message(Command("reminders"))
@router.message(Command("menu"))
async def show_reminders_menu(message: Message, state: FSMContext):
    await state.clear()
    user = await TelegramUser.objects.filter(telegram_id=message.from_user.id).afirst()
    if user is None:
        await message.answer("Спочатку виконайте /start.")
        return
    await message.answer("Меню керування нагадуваннями:", reply_markup=reminders_menu_keyboard())




@router.callback_query(F.data.startswith("mgr:"))
async def reminder_management_callback(callback: CallbackQuery, state: FSMContext):
    try:
        parts = callback.data.split(":")
        action = parts[1]
    except (AttributeError, IndexError):
        await callback.answer("Некоректна команда.", show_alert=True)
        return

    telegram_id = callback.from_user.id
    if action == "menu":
        await state.clear()
        await callback.message.edit_text("Меню керування нагадуваннями:", reply_markup=reminders_menu_keyboard())
        await callback.answer()
        return

    if action == "new":
        user = await TelegramUser.objects.filter(telegram_id=telegram_id).afirst()
        if user is None:
            await callback.answer("Спочатку виконайте /start.", show_alert=True)
            return
        await state.clear()
        await state.update_data(author_id=user.pk, timezone=user.timezone or "Europe/Kyiv")
        await state.set_state(ReminderCreation.title)
        await callback.message.answer("Як назвати нове нагадування? Для скасування — /cancel.")
        await callback.answer()
        return

    if action == "list":
        try:
            mode, page = parts[2], max(0, int(parts[3]))
        except (IndexError, ValueError):
            await callback.answer("Некоректний список.", show_alert=True)
            return
        if mode not in {"controlled", "assigned"}:
            await callback.answer("Невідомий розділ.", show_alert=True)
            return
        items = await sync_to_async(list_reminders, thread_sensitive=True)(telegram_id, mode)
        await state.update_data(reminder_list_mode=mode, reminder_list_page=page)
        if not items:
            text = "У цьому розділі поки немає нагадувань."
            markup = reminders_list_keyboard([], mode, 0)
        else:
            pages = max(1, (len(items) + 4) // 5)
            page = min(page, pages - 1)
            await state.update_data(reminder_list_page=page)
            lines = [
                f"Список ({'я контролюю' if mode == 'controlled' else 'призначені мені'}), сторінка {page + 1}/{pages}:"]
            for item in items[page * 5:(page + 1) * 5]:
                when = item["next_reminder_at"]
                when_text = when.astimezone(ZoneInfo(item["timezone"])).strftime(
                    "%d.%m.%Y %H:%M") if when else "не заплановано"
                lines.append(f"{'🟢' if item['is_active'] else '⏸'} {item['title']} — {when_text}")
            text = "\n".join(lines)
            markup = reminders_list_keyboard(items, mode, page)
        await callback.message.edit_text(text, reply_markup=markup)
        await callback.answer()
        return

    if action == "view":
        try:
            reminder_id, mode = int(parts[2]), parts[3]
        except (IndexError, ValueError):
            await callback.answer("Некоректне нагадування.", show_alert=True)
            return
        detail = await sync_to_async(get_reminder_detail, thread_sensitive=True)(reminder_id, telegram_id)
        if detail is None:
            await callback.answer("Нагадування не знайдено або у вас немає доступу.", show_alert=True)
            return
        await state.update_data(reminder_list_mode=mode)
        zone = ZoneInfo(detail["timezone"])
        first = detail["first_reminder_at"].astimezone(zone).strftime("%d.%m.%Y %H:%M")
        next_at = detail["next_reminder_at"]
        next_text = next_at.astimezone(zone).strftime("%d.%m.%Y %H:%M") if next_at else "немає"
        counts = detail["response_counts"]
        lines = [
            f"📌 {detail['title']}",
            f"Стан: {'активне' if detail['is_active'] else 'призупинене'}",
            f"Автор: {detail['author']}",
            f"Перше спрацювання: {first} ({detail['timezone']})",
            f"Наступне: {next_text}",
            f"Повторення: {detail['recurrence_type']} (інтервал {detail['recurrence_interval']})",
            f"Очікують: {counts['pending']} · Виконано: {counts['completed']} · Відхилено: {counts['rejected']} · Прострочено: {counts['expired']}",
        ]
        if detail["is_controller"]:
            lines.append(f"Сповіщення контролерів: {detail['notification_policy']}")
            lines.append(f"Тайм-аут відповіді: {detail['response_timeout_minutes']} хв")
            if detail["participants"]:
                lines.append("Учасники:")
                for role, full_name, username, participant_id in detail["participants"]:
                    name = full_name or (f"@{username}" if username else str(participant_id))
                    lines.append(f"• {'Контролер' if role == 'controller' else 'Виконавець'}: {name}")
        await callback.message.edit_text(
            "\n".join(lines),
            reply_markup=reminder_detail_keyboard(reminder_id, can_manage=detail["is_controller"],
                                                  is_active=detail["is_active"]),
        )
        await callback.answer()
        return

    if action in {"pause", "resume"}:
        try:
            reminder_id = int(parts[2])
        except (IndexError, ValueError):
            await callback.answer("Некоректне нагадування.", show_alert=True)
            return
        active = action == "resume"
        ok = await sync_to_async(set_reminder_active, thread_sensitive=True)(reminder_id, telegram_id, active)
        if not ok:
            await callback.answer("Не вдалося змінити стан: немає доступу або нагадування вже завершене.",
                                  show_alert=True)
            return
        detail = await sync_to_async(get_reminder_detail, thread_sensitive=True)(reminder_id, telegram_id)
        await callback.message.edit_reply_markup(
            reply_markup=reminder_detail_keyboard(reminder_id, can_manage=True, is_active=detail["is_active"])
        )
        await callback.answer("Нагадування відновлено." if active else "Нагадування призупинено.")
        return

    if action == "rename":
        try:
            reminder_id = int(parts[2])
        except (IndexError, ValueError):
            await callback.answer("Некоректне нагадування.", show_alert=True)
            return
        detail = await sync_to_async(get_reminder_detail, thread_sensitive=True)(reminder_id, telegram_id)
        if detail is None or not detail["is_controller"]:
            await callback.answer("Немає прав на редагування.", show_alert=True)
            return
        await state.clear()
        await state.update_data(rename_reminder_id=reminder_id)
        await state.set_state(ReminderManagement.rename_title)
        await callback.message.answer("Надішліть нову назву нагадування (до 255 символів). /cancel — скасувати.")
        await callback.answer()
        return

    if action == "delete_confirm":
        try:
            reminder_id = int(parts[2])
        except (IndexError, ValueError):
            await callback.answer("Некоректне нагадування.", show_alert=True)
            return
        detail = await sync_to_async(get_reminder_detail, thread_sensitive=True)(reminder_id, telegram_id)
        if detail is None or not detail["is_controller"]:
            await callback.answer("Немає прав на видалення.", show_alert=True)
            return
        await callback.message.edit_text(
            f"Видалити нагадування «{detail['title']}»? Разом із ним буде видалено історію спрацювань і відповідей.",
            reply_markup=delete_confirmation_keyboard(reminder_id),
        )
        await callback.answer()
        return

    if action == "delete":
        try:
            reminder_id = int(parts[2])
        except (IndexError, ValueError):
            await callback.answer("Некоректне нагадування.", show_alert=True)
            return
        ok = await sync_to_async(delete_reminder, thread_sensitive=True)(reminder_id, telegram_id)
        if not ok:
            await callback.answer("Немає прав або нагадування вже видалене.", show_alert=True)
            return
        await callback.message.edit_text("🗑 Нагадування та його історію видалено.",
                                         reply_markup=reminders_menu_keyboard())
        await callback.answer("Видалено.")
        return

    if action == "cancel_delete":
        try:
            reminder_id = int(parts[2])
        except (IndexError, ValueError):
            await callback.answer("Некоректне нагадування.", show_alert=True)
            return
        detail = await sync_to_async(get_reminder_detail, thread_sensitive=True)(reminder_id, telegram_id)
        if detail is None:
            await callback.answer("Нагадування недоступне.", show_alert=True)
            return
        await callback.message.edit_text(
            f"📌 {detail['title']}\nСтан: {'активне' if detail['is_active'] else 'призупинене'}",
            reply_markup=reminder_detail_keyboard(reminder_id, can_manage=detail['is_controller'],
                                                  is_active=detail['is_active']),
        )
        await callback.answer("Видалення скасовано.")
        return

    if action == "back":
        data = await state.get_data()
        mode = data.get("reminder_list_mode", "controlled")
        page = data.get("reminder_list_page", 0)
        items = await sync_to_async(list_reminders, thread_sensitive=True)(telegram_id, mode)
        pages = max(1, (len(items) + 4) // 5)
        page = min(page, pages - 1)
        lines = [f"Список ({'я контролюю' if mode == 'controlled' else 'призначені мені'}):"]
        for item in items[page * 5:(page + 1) * 5]:
            when = item["next_reminder_at"]
            when_text = when.astimezone(ZoneInfo(item["timezone"])).strftime(
                "%d.%m.%Y %H:%M") if when else "не заплановано"
            lines.append(f"{'🟢' if item['is_active'] else '⏸'} {item['title']} — {when_text}")
        await callback.message.edit_text("\n".join(lines) if items else "У цьому розділі поки немає нагадувань.",
                                         reply_markup=reminders_list_keyboard(items, mode, page))
        await callback.answer()
        return

    await callback.answer("Невідома дія.", show_alert=True)


@router.message(ReminderManagement.rename_title)
async def rename_reminder_title(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        ok = await sync_to_async(rename_reminder, thread_sensitive=True)(
            data["rename_reminder_id"], message.from_user.id, message.text or ""
        )
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.clear()
    if ok:
        await message.answer("✅ Назву нагадування змінено. Переглянути список: /reminders")
    else:
        await message.answer("Не вдалося змінити назву: нагадування недоступне або у вас немає прав.")


@router.message(Command("new_reminder"))
async def start_reminder_creation(message: Message, state: FSMContext):
    await state.clear()
    user = await TelegramUser.objects.filter(telegram_id=message.from_user.id).afirst()
    if user is None:
        await message.answer("Спочатку виконайте /start.")
        return
    await state.update_data(author_id=user.pk, timezone=user.timezone or "Europe/Kyiv")
    await state.set_state(ReminderCreation.title)
    await message.answer("Як назвати нагадування? Надішліть короткий текст. Для скасування — /cancel.")


@router.message(ReminderCreation.title)
async def creation_title(message: Message, state: FSMContext):
    title = (message.text or "").strip()
    if not title or len(title) > 255:
        await message.answer("Назва має містити від 1 до 255 символів. Спробуйте ще раз.")
        return
    await state.update_data(title=title)
    data = await state.get_data()
    await state.set_state(ReminderCreation.first_datetime)
    await message.answer(
        f"Коли нагадати? Часовий пояс: {data['timezone']}.\n"
        "Введіть дату й час: ДД.ММ.РРРР ГГ:ХХ\nНаприклад: 18.11.2026 09:30"
    )


@router.message(ReminderCreation.first_datetime)
async def creation_datetime(message: Message, state: FSMContext):
    try:
        local_dt = _parse_local_datetime(message.text or "")
        data = await state.get_data()
        utc_dt = normalize_first_datetime(local_dt, data["timezone"])
        if utc_dt <= django_timezone.now():
            await message.answer("Дата й час мають бути в майбутньому. Введіть інший час.")
            return
    except (ValueError, ValidationError) as exc:
        await message.answer(str(exc) or "Не вдалося розпізнати дату. Спробуйте ще раз.")
        return
    await state.update_data(first_reminder_at=local_dt.isoformat())
    await state.set_state(ReminderCreation.recurrence)
    await message.answer(
        "Як часто повторювати?\n"
        "Введіть один варіант: одноразове, щодня, щотижня, щомісяця або щороку."
    )


@router.message(ReminderCreation.recurrence)
async def creation_recurrence(message: Message, state: FSMContext):
    recurrence = _parse_recurrence(message.text or "")
    if recurrence is None:
        await message.answer("Не розпізнав варіант. Оберіть: одноразове, щодня, щотижня, щомісяця або щороку.")
        return
    await state.update_data(recurrence_type=recurrence)
    if recurrence == Reminder.RecurrenceType.NONE:
        await state.set_state(ReminderCreation.assignees)
        await message.answer(
            "Кому призначити нагадування? Вкажіть @username або Telegram ID через кому. Для особистого — «пропустити».")
    else:
        await state.set_state(ReminderCreation.interval)
        await message.answer("Кожен який інтервал? Введіть ціле число: 1 — кожен період, 2 — через один, тощо.")


@router.message(ReminderCreation.interval)
async def creation_interval(message: Message, state: FSMContext):
    try:
        interval = int((message.text or "").strip())
        if not 1 <= interval <= 365:
            raise ValueError
    except ValueError:
        await message.answer("Введіть ціле число від 1 до 365.")
        return
    await state.update_data(recurrence_interval=interval)
    await state.set_state(ReminderCreation.assignees)
    await message.answer(
        "Кому призначити нагадування? Вкажіть @username або Telegram ID через кому. Для особистого — «пропустити».")


@router.message(ReminderCreation.assignees)
async def creation_assignees(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    assignees = []
    if raw.lower() not in {"пропустити", "ні", "немає", "-", "skip"}:
        assignees, missing = await sync_to_async(_resolve_users, thread_sensitive=True)(raw)
        if missing:
            await message.answer(
                "Не знайшов зареєстрованих користувачів: " + ", ".join(missing) +
                ". Вони мають хоча б один раз запустити бота через /start. Спробуйте ще раз або «пропустити»."
            )
            return
    await state.update_data(assignee_ids=[user.pk for user in assignees])
    await state.set_state(ReminderCreation.controllers)
    await message.answer(
        "Додати інших контролерів? Вкажіть @username або Telegram ID через кому. Якщо ні — «пропустити».")


@router.message(ReminderCreation.controllers)
async def creation_controllers(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    controllers = []
    if raw.lower() not in {"пропустити", "ні", "немає", "-", "skip"}:
        controllers, missing = await sync_to_async(_resolve_users, thread_sensitive=True)(raw)
        if missing:
            await message.answer(
                "Не знайшов зареєстрованих користувачів: " + ", ".join(missing) +
                ". Вони мають спочатку виконати /start. Спробуйте ще раз або «пропустити»."
            )
            return
    data = await state.get_data()
    author = await TelegramUser.objects.aget(pk=data["author_id"])
    assignees = await sync_to_async(lambda: list(TelegramUser.objects.filter(pk__in=data["assignee_ids"])),
                                    thread_sensitive=True)()
    try:
        reminder = await sync_to_async(create_reminder, thread_sensitive=True)(
            author=author,
            title=data["title"],
            first_reminder_at=datetime.fromisoformat(data["first_reminder_at"]),
            recurrence_type=data.get("recurrence_type", Reminder.RecurrenceType.NONE),
            recurrence_interval=data.get("recurrence_interval", 1),
            assignees=assignees,
            controllers=controllers,
            timezone_name=data["timezone"],
        )
    except (ValidationError, ValueError) as exc:
        await state.clear()
        await message.answer(f"Не вдалося створити нагадування: {exc}\nСпробуйте ще раз командою /new_reminder.")
        return

    local_at = reminder.first_reminder_at.astimezone(ZoneInfo(reminder.timezone))
    recurrence_label = reminder.get_recurrence_type_display()
    await state.clear()
    await message.answer(
        "✅ Нагадування створено!\n"
        f"«{reminder.title}»\n"
        f"Перше спрацювання: {local_at:%d.%m.%Y %H:%M} ({reminder.timezone})\n"
        f"Повторення: {recurrence_label}"
        + (
            f", інтервал {reminder.recurrence_interval}" if reminder.recurrence_type != Reminder.RecurrenceType.NONE else "")
        + f"\nВиконавців: {len(assignees) if assignees else 1}."
    )


@router.callback_query(F.data.startswith("rem:"))
async def reminder_response_callback(callback: CallbackQuery, state: FSMContext):
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

    if command == "custom":
        response = await sync_to_async(_get_response_for_custom_defer, thread_sensitive=True)(response_id,
                                                                                              callback.from_user.id)
        if response is None:
            await callback.answer("Це нагадування не належить вам або вже неактуальне.", show_alert=True)
            return
        await state.clear()
        await state.update_data(
            custom_response_id=response_id,
            source_message_id=callback.message.message_id,
            reminder_timezone=response.occurrence.reminder.timezone,
        )
        await state.set_state(CustomDefer.waiting_datetime)
        await callback.message.answer(
            f"Введіть дату й час повторного нагадування у часовому поясі "
            f"{response.occurrence.reminder.timezone}.\n"
            "Формат: ДД.ММ.РРРР ГГ:ХХ. Максимум — 7 днів від поточного моменту.\n"
            "Для скасування — /cancel."
        )
        await callback.answer()
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

    await _apply_and_notify(callback, response_id, action, minutes, None)


@router.message(CustomDefer.waiting_datetime)
async def custom_defer_datetime(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        local_dt = _parse_local_datetime(message.text or "")
        zone = ZoneInfo(data["reminder_timezone"])
        candidate = local_dt.replace(tzinfo=zone, fold=0)
        round_trip = candidate.astimezone(ZoneInfo("UTC")).astimezone(zone).replace(tzinfo=None)
        if round_trip != local_dt:
            await message.answer("Цей локальний час не існує через перехід на літній час. Введіть інший час.")
            return
        defer_until = candidate.astimezone(ZoneInfo("UTC"))
        now = django_timezone.now()
        seconds = (defer_until - now).total_seconds()
        if seconds <= 0:
            await message.answer("Час має бути в майбутньому. Спробуйте ще раз.")
            return
        if seconds > 7 * 24 * 60 * 60:
            await message.answer("Можна відтермінувати не більш ніж на 7 днів.")
            return
        response_id = data["custom_response_id"]
        source_message_id = data["source_message_id"]
    except (ValueError, KeyError, ZoneInfoNotFoundError):
        await message.answer("Не вдалося розпізнати дату. Формат: ДД.ММ.РРРР ГГ:ХХ.")
        return

    await _apply_and_notify(
        message, response_id, "defer", None, defer_until,
        telegram_message_id=source_message_id,
    )
    await state.clear()


async def _apply_and_notify(event, response_id: int, action: str, minutes: int | None, defer_until,
                            telegram_message_id=None):
    actor_id = event.from_user.id
    source_message_id = telegram_message_id
    if source_message_id is None and isinstance(event, CallbackQuery) and event.message:
        source_message_id = event.message.message_id
    try:
        response, audit_event = await sync_to_async(apply_response_action, thread_sensitive=True)(
            response_id=response_id,
            actor_telegram_id=actor_id,
            action=action,
            defer_minutes=minutes,
            defer_until=defer_until,
            telegram_message_id=source_message_id,
        )
    except ResponseActionError as exc:
        if isinstance(event, CallbackQuery):
            await event.answer(str(exc), show_alert=True)
        else:
            await event.answer(str(exc))
        return
    except Exception:
        logger.exception("Помилка обробки callback нагадування %s", response_id)
        if isinstance(event, CallbackQuery):
            await event.answer("Не вдалося зберегти відповідь. Спробуйте ще раз.", show_alert=True)
        else:
            await event.answer("Не вдалося зберегти відповідь. Спробуйте ще раз.")
        return

    if isinstance(event, CallbackQuery):
        if event.message:
            await event.message.edit_reply_markup(reply_markup=None)
        await event.answer("Відповідь збережено.")
        bot = event.bot
        reply_target = event.message
    else:
        await event.answer("✅ Час відтермінування збережено.")
        bot = event.bot
        reply_target = event

    reminder = response.occurrence.reminder
    should_notify = (
            reminder.notification_policy == Reminder.NotificationPolicy.ALL_ACTIONS
            or action == "rejected"
    )
    if should_notify:
        controller_ids = await sync_to_async(get_controller_telegram_ids, thread_sensitive=True)(reminder.pk)
        actor_name = response.assignee.full_name or response.assignee.username or str(response.assignee.telegram_id)
        if action == "defer":
            local_defer = audit_event.deferred_until.astimezone(ZoneInfo(reminder.timezone))
            status_text = f"відтермінував(ла) «{reminder.title}» до {local_defer:%d.%m.%Y %H:%M} ({reminder.timezone})"
        else:
            status_text = f"{'виконав(ла)' if action == 'completed' else 'відхилив(ла)'} нагадування «{reminder.title}»"
        for telegram_id in controller_ids:
            try:
                await bot.send_message(telegram_id, f"ℹ️ {actor_name} {status_text}.")
            except Exception:
                logger.exception("Не вдалося сповістити контролера %s", telegram_id)
