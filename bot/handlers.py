"""Telegram handlers: reminder creation wizard and response actions."""

import asyncio
import logging
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
    cancel_keyboard,
    defer_keyboard,
    delete_confirmation_keyboard,
    nagging_keyboard,
    reminder_detail_keyboard,
    reminder_edit_menu_keyboard,
    reminders_list_keyboard,
    reminders_menu_keyboard,
    repeat_time_keyboard,
    response_keyboard,
    skip_keyboard,
)
from reminders.controls import (
    delete_reminder,
    get_reminder_detail,
    list_reminders,
    rename_reminder,
    set_reminder_active,
    update_reminder_nagging,
    update_reminder_participants,
    update_reminder_recurrence,
    update_reminder_schedule,
    update_reminder_timeout,
)
from reminders.models import Reminder, ReminderResponse
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
    nagging = State()  # <--- НОВИЙ СТАН
    nagging_custom = State()  # <--- НОВИЙ СТАН ДЛЯ ВВОДУ ХВИЛИН


class CustomDefer(StatesGroup):
    waiting_datetime = State()


class ReminderManagement(StatesGroup):
    rename_title = State()
    edit_datetime = State()
    edit_recurrence = State()
    edit_interval = State()
    edit_nagging = State()
    edit_nagging_custom = State()
    edit_timeout = State()
    edit_assignees = State()
    edit_controllers = State()


def _parse_local_datetime(raw: str) -> datetime:
    """Accept Ukrainian-friendly date/time formats; return a naive wall time."""
    value = raw.strip()
    for fmt in ("%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M", "%d.%m.%Y о %H:%M"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValueError(
        "Введіть дату й час у форматі ДД.ММ.РРРР ГГ:ХХ, наприклад 18.11.2026 09:30."
    )


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
    tokens = [
        part.strip().lstrip("@")
        for part in raw.replace(";", ",").split(",")
        if part.strip()
    ]
    users, missing = [], []
    for token in tokens:
        query = (
            TelegramUser.objects.filter(telegram_id=int(token))
            if token.isdigit()
            else TelegramUser.objects.filter(username__iexact=token)
        )
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
            deferred_until__isnull=True,  # <--- НЕ ДАЄМО ПОВТОРНО ВІДТЕРМІНУВАТИ
        )
        .first()
    )


def _build_reminders_list_view(items: list[dict], mode: str, page: int):
    """Формує текст та інлайн-клавіатуру для списку нагадувань."""
    if not items:
        return (
            "У цьому розділі поки немає нагадувань.",
            reminders_list_keyboard([], mode, 0),
            0,
        )

    pages = max(1, (len(items) + 4) // 5)
    page = min(page, pages - 1)
    section_name = "я контролюю" if mode == "controlled" else "призначені мені"
    lines = [f"Список ({section_name}), сторінка {page + 1}/{pages}:"]
    for item in items[page * 5 : (page + 1) * 5]:
        when = item["next_reminder_at"]
        when_text = (
            when.astimezone(ZoneInfo(item["timezone"])).strftime("%d.%m.%Y %H:%M")
            if when
            else "не заплановано"
        )
        lines.append(
            f"{'🟢' if item['is_active'] else '⏸'} {item['title']} — {when_text}"
        )
    return "\n".join(lines), reminders_list_keyboard(items, mode, page), page


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
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
        await message.answer(f"Привіт, {greeting_name}! Ви зареєстровані.")
    else:
        await message.answer(f"З поверненням, {greeting_name}!")


@router.message(Command("cancel"))
async def cancel_dialog(message: Message, state: FSMContext):
    await _delete_wizard_message(message.bot, message.chat.id, state)
    await state.clear()
    await message.answer("Дію скасовано.")


@router.callback_query(F.data == "cancel_fsm")
async def cancel_fsm_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.delete()
    except Exception:  # noqa: BLE001
        await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Дію скасовано.")


@router.message(Command("timezone"))
async def set_timezone(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.answer(
            "Вкажіть часовий пояс у форматі IANA, наприклад: /timezone Europe/Kyiv"
        )
        return
    zone_name = parts[1].strip()
    try:
        get_zone(zone_name)
    except ValidationError:
        await message.answer(
            "Не знаю такого часового поясу. Приклад правильного значення: Europe/Kyiv."
        )
        return
    user = await TelegramUser.objects.filter(telegram_id=message.from_user.id).afirst()
    if user is None:
        await message.answer("Спочатку виконайте /start.")
        return
    user.timezone = zone_name
    await user.asave(update_fields=["timezone"])
    await message.answer(
        f"Часовий пояс збережено: {zone_name}. Він застосовуватиметься до нових нагадувань."
    )


@router.message(Command("reminders"))
@router.message(Command("menu"))
async def show_reminders_menu(message: Message, state: FSMContext):
    await state.clear()
    user = await TelegramUser.objects.filter(telegram_id=message.from_user.id).afirst()
    if user is None:
        await message.answer("Спочатку виконайте /start.")
        return
    await message.answer(
        "Меню керування нагадуваннями:", reply_markup=reminders_menu_keyboard()
    )


async def _open_reminders_section(message: Message, state: FSMContext, mode: str):
    await state.clear()
    telegram_id = message.from_user.id
    user = await TelegramUser.objects.filter(telegram_id=telegram_id).afirst()
    if user is None:
        await message.answer("Спочатку виконайте /start.")
        return
    items = await sync_to_async(list_reminders, thread_sensitive=True)(
        telegram_id, mode
    )
    text, markup, page = _build_reminders_list_view(items, mode, 0)
    await state.update_data(reminder_list_mode=mode, reminder_list_page=page)
    await message.answer(text, reply_markup=markup)


@router.message(F.text == "🛠 Я контролюю")
async def reply_controlled_reminders(message: Message, state: FSMContext):
    await _open_reminders_section(message, state, "controlled")


@router.message(F.text == "📥 Призначені мені")
async def reply_assigned_reminders(message: Message, state: FSMContext):
    await _open_reminders_section(message, state, "assigned")


@router.callback_query(F.data.startswith("mgr:"))
async def reminder_management_callback(callback: CallbackQuery, state: FSMContext):
    try:
        parts = callback.data.split(":")
        action = parts[1]
    except AttributeError, IndexError:
        await callback.answer("Некоректна команда.", show_alert=True)
        return

    telegram_id = callback.from_user.id
    if action == "menu":
        await state.clear()
        await callback.message.edit_text(
            "Меню керування нагадуваннями:", reply_markup=reminders_menu_keyboard()
        )
        await callback.answer()
        return

    if action == "new":
        user = await TelegramUser.objects.filter(telegram_id=telegram_id).afirst()
        if user is None:
            await callback.answer("Спочатку виконайте /start.", show_alert=True)
            return
        await state.clear()
        await state.update_data(
            author_id=user.pk, timezone=user.timezone or "Europe/Kyiv"
        )
        await state.set_state(ReminderCreation.title)
        await _ask_wizard_step(
            callback.message,
            state,
            "Як назвати нове нагадування?",
            reply_markup=cancel_keyboard(),
        )
        await callback.answer()
        return

    if action == "list":
        try:
            mode, page = parts[2], max(0, int(parts[3]))
        except IndexError, ValueError:
            await callback.answer("Некоректний список.", show_alert=True)
            return
        if mode not in {"controlled", "assigned"}:
            await callback.answer("Невідомий розділ.", show_alert=True)
            return
        items = await sync_to_async(list_reminders, thread_sensitive=True)(
            telegram_id, mode
        )
        text, markup, page = _build_reminders_list_view(items, mode, page)
        await state.update_data(reminder_list_mode=mode, reminder_list_page=page)
        await callback.message.edit_text(text, reply_markup=markup)
        await callback.answer()
        return

    if action == "view":
        try:
            reminder_id, mode = int(parts[2]), parts[3]
        except IndexError, ValueError:
            await callback.answer("Некоректне нагадування.", show_alert=True)
            return
        await _render_reminder_detail_view(callback, state, reminder_id, mode)
        return

    if action in {"pause", "resume"}:
        try:
            reminder_id = int(parts[2])
        except IndexError, ValueError:
            await callback.answer("Некоректне нагадування.", show_alert=True)
            return
        active = action == "resume"
        ok = await sync_to_async(set_reminder_active, thread_sensitive=True)(
            reminder_id, telegram_id, active
        )
        if not ok:
            await callback.answer(
                "Не вдалося змінити стан: немає доступу або нагадування вже завершене.",
                show_alert=True,
            )
            return
        detail = await sync_to_async(get_reminder_detail, thread_sensitive=True)(
            reminder_id, telegram_id
        )
        await callback.message.edit_reply_markup(
            reply_markup=reminder_detail_keyboard(
                reminder_id, can_manage=True, is_active=detail["is_active"]
            )
        )
        await callback.answer(
            "Нагадування відновлено." if active else "Нагадування призупинено."
        )
        return

    if action == "rename":
        try:
            reminder_id = int(parts[2])
        except IndexError, ValueError:
            await callback.answer("Некоректне нагадування.", show_alert=True)
            return

        # Отримуємо деталі нагадування або перевіряємо автора
        detail = await sync_to_async(get_reminder_detail, thread_sensitive=True)(
            reminder_id, telegram_id
        )
        if detail is None:
            await callback.answer("Нагадування не знайдено.", show_alert=True)
            return

        # Перевірка: чи є користувач автором нагадування
        reminder_obj = await sync_to_async(
            lambda: (
                Reminder.objects.select_related("author").filter(pk=reminder_id).first()
            ),
            thread_sensitive=True,
        )()

        if reminder_obj is None or reminder_obj.author.telegram_id != telegram_id:
            await callback.answer(
                "Змінювати нагадування можуть лише їх автори.", show_alert=True
            )
            return

        await state.clear()
        await state.update_data(rename_reminder_id=reminder_id)
        await state.set_state(ReminderManagement.rename_title)
        await callback.message.answer(
            "Надішліть нову назву нагадування (до 255 символів).",
            reply_markup=cancel_keyboard(),
        )
        await callback.answer()
        return

    if action == "edit_menu":
        reminder_id = int(parts[2])
        detail = await sync_to_async(get_reminder_detail, thread_sensitive=True)(
            reminder_id, telegram_id
        )
        if detail is None or not detail["is_controller"]:
            await callback.answer("Немає прав на редагування.", show_alert=True)
            return
        await state.set_state(None)
        await callback.message.edit_text(
            f"✏️ Редагування «{detail['title']}»\nОберіть, що саме змінити:",
            reply_markup=reminder_edit_menu_keyboard(reminder_id),
        )
        await callback.answer()
        return

    if action == "view_id":
        try:
            reminder_id = int(parts[2])
        except IndexError, ValueError:
            await callback.answer("Некоректне нагадування.", show_alert=True)
            return
        data = await state.get_data()
        mode = data.get("reminder_list_mode", "controlled")
        await _render_reminder_detail_view(callback, state, reminder_id, mode)
        return

    if action == "edit_time":
        reminder_id = int(parts[2])
        detail = await sync_to_async(get_reminder_detail, thread_sensitive=True)(
            reminder_id, telegram_id
        )
        if detail is None or not detail["is_controller"]:
            await callback.answer("Немає прав.", show_alert=True)
            return
        await state.update_data(
            edit_reminder_id=reminder_id, edit_timezone=detail["timezone"]
        )
        await state.set_state(ReminderManagement.edit_datetime)
        await _ask_wizard_step(
            callback.message,
            state,
            f"Введіть нову дату й час ({detail['timezone']}) у форматі ДД.ММ.РРРР ГГ:ХХ:",
            reply_markup=cancel_keyboard(),
        )
        await callback.answer()
        return

    if action == "edit_rec":
        reminder_id = int(parts[2])
        await state.update_data(edit_reminder_id=reminder_id)
        await state.set_state(ReminderManagement.edit_recurrence)
        await _ask_wizard_step(
            callback.message,
            state,
            "Оберіть нову частоту повторення:",
            reply_markup=repeat_time_keyboard(),
        )
        await callback.answer()
        return

    if action == "edit_nag":
        reminder_id = int(parts[2])
        await state.update_data(edit_reminder_id=reminder_id)
        await state.set_state(ReminderManagement.edit_nagging)
        await _ask_wizard_step(
            callback.message,
            state,
            "Оберіть режим настирливих нагадувань («Пропустити» — вимкнути):",
            reply_markup=nagging_keyboard(),
        )
        await callback.answer()
        return

    if action == "edit_timeout":
        try:
            reminder_id = int(parts[2])
        except IndexError, ValueError:
            await callback.answer("Некоректне нагадування.", show_alert=True)
            return
        detail = await sync_to_async(get_reminder_detail, thread_sensitive=True)(
            reminder_id, telegram_id
        )
        if detail is None or not detail["is_controller"]:
            await callback.answer("Немає прав на редагування.", show_alert=True)
            return
        await state.update_data(edit_reminder_id=reminder_id)
        await state.set_state(ReminderManagement.edit_timeout)
        await _ask_wizard_step(
            callback.message,
            state,
            f"Поточний тайм-аут: {detail['response_timeout_minutes']} хв.\n"
            "Введіть новий час очікування відповіді у хвилинах (ціле число від 1 до 10080):",
            reply_markup=cancel_keyboard(),
        )
        await callback.answer()
        return

    if action in {"edit_assignees", "edit_controllers"}:
        reminder_id = int(parts[2])
        role = "assignee" if action == "edit_assignees" else "controller"
        target_state = (
            ReminderManagement.edit_assignees
            if role == "assignee"
            else ReminderManagement.edit_controllers
        )
        await state.update_data(edit_reminder_id=reminder_id, edit_role=role)
        await state.set_state(target_state)
        await _ask_wizard_step(
            callback.message,
            state,
            "Вкажіть новий список (@username або Telegram ID через кому) або натисніть «Пропустити», щоб очистити список:",
            reply_markup=skip_keyboard(),
        )
        await callback.answer()
        return

    if action == "delete_confirm":
        try:
            reminder_id = int(parts[2])
        except IndexError, ValueError:
            await callback.answer("Некоректне нагадування.", show_alert=True)
            return
        detail = await sync_to_async(get_reminder_detail, thread_sensitive=True)(
            reminder_id, telegram_id
        )
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
        except IndexError, ValueError:
            await callback.answer("Некоректне нагадування.", show_alert=True)
            return
        ok = await sync_to_async(delete_reminder, thread_sensitive=True)(
            reminder_id, telegram_id
        )
        if not ok:
            await callback.answer(
                "Немає прав або нагадування вже видалене.", show_alert=True
            )
            return
        await callback.message.edit_text(
            "🗑 Нагадування та його історію видалено.",
            reply_markup=reminders_menu_keyboard(),
        )
        await callback.answer("Видалено.")
        return

    if action == "cancel_delete":
        try:
            reminder_id = int(parts[2])
        except IndexError, ValueError:
            await callback.answer("Некоректне нагадування.", show_alert=True)
            return
        detail = await sync_to_async(get_reminder_detail, thread_sensitive=True)(
            reminder_id, telegram_id
        )
        if detail is None:
            await callback.answer("Нагадування недоступне.", show_alert=True)
            return
        await callback.message.edit_text(
            f"📌 {detail['title']}\nСтан: {'активне' if detail['is_active'] else 'призупинене'}",
            reply_markup=reminder_detail_keyboard(
                reminder_id,
                can_manage=detail["is_controller"],
                is_active=detail["is_active"],
            ),
        )
        await callback.answer("Видалення скасовано.")
        return

    if action == "back":
        data = await state.get_data()
        mode = data.get("reminder_list_mode", "controlled")
        page = data.get("reminder_list_page", 0)
        items = await sync_to_async(list_reminders, thread_sensitive=True)(
            telegram_id, mode
        )
        text, markup, _ = _build_reminders_list_view(items, mode, page)
        await callback.message.edit_text(text, reply_markup=markup)
        await callback.answer()
        return

    await callback.answer("Невідома дія.", show_alert=True)


@router.message(ReminderManagement.rename_title)
async def rename_reminder_title(message: Message, state: FSMContext):
    data = await state.get_data()
    reminder_id = data.get("rename_reminder_id")
    telegram_id = message.from_user.id

    # Подвійна перевірка автора на етапі збереження повідомлення
    reminder_obj = await sync_to_async(
        lambda: (
            Reminder.objects.select_related("author").filter(pk=reminder_id).first()
        ),
        thread_sensitive=True,
    )()

    if reminder_obj is None or reminder_obj.author.telegram_id != telegram_id:
        await state.clear()
        await message.answer("Змінювати нагадування можуть лише їх автори.")
        return

    try:
        ok = await sync_to_async(rename_reminder, thread_sensitive=True)(
            reminder_id, telegram_id, message.text or ""
        )
    except ValueError as exc:
        await message.answer(str(exc), reply_markup=cancel_keyboard())
        return

    await state.clear()
    if ok:
        await message.answer("✅ Назву нагадування змінено.")
    else:
        await message.answer(
            "Не вдалося змінити назву: нагадування недоступне або у вас немає прав."
        )


@router.message(Command("new_reminder"))
@router.message(F.text == "➕ Створити нагадування")
async def start_reminder_creation(message: Message, state: FSMContext):
    await state.clear()
    user = await TelegramUser.objects.filter(telegram_id=message.from_user.id).afirst()
    if user is None:
        await message.answer("Спочатку виконайте /start.")
        return
    await state.update_data(author_id=user.pk, timezone=user.timezone or "Europe/Kyiv")
    await state.set_state(ReminderCreation.title)
    await _ask_wizard_step(
        message,
        state,
        "Як назвати нагадування? Надішліть короткий текст.",
        reply_markup=cancel_keyboard(),
    )


@router.message(ReminderCreation.title)
async def creation_title(message: Message, state: FSMContext):
    title = (message.text or "").strip()
    if not title or len(title) > 255:
        await _ask_wizard_step(
            message,
            state,
            "Назва має містити від 1 до 255 символів. Спробуйте ще раз.",
            reply_markup=cancel_keyboard(),
        )
        return
    await state.update_data(title=title)
    data = await state.get_data()
    await state.set_state(ReminderCreation.first_datetime)
    await _ask_wizard_step(
        message,
        state,
        f"Коли нагадати? Часовий пояс: {data['timezone']}.\n"
        "Введіть дату й час: ДД.ММ.РРРР ГГ:ХХ\nНаприклад: 18.11.2026 09:30",
        reply_markup=cancel_keyboard(),
    )


@router.message(ReminderCreation.first_datetime)
async def creation_datetime(message: Message, state: FSMContext):
    try:
        local_dt = _parse_local_datetime(message.text or "")
        data = await state.get_data()
        utc_dt = normalize_first_datetime(local_dt, data["timezone"])
        if utc_dt <= django_timezone.now():
            await _ask_wizard_step(
                message,
                state,
                "Дата й час мають бути в майбутньому. Введіть інший час.",
                reply_markup=cancel_keyboard(),
            )
            return
    except (ValueError, ValidationError) as exc:
        await _ask_wizard_step(
            message,
            state,
            str(exc) or "Не вдалося розпізнати дату. Спробуйте ще раз.",
            reply_markup=cancel_keyboard(),
        )
        return
    await state.update_data(first_reminder_at=local_dt.isoformat())
    await state.set_state(ReminderCreation.recurrence)
    await _ask_wizard_step(
        message,
        state,
        "Оберіть частоту повторення:",
        reply_markup=repeat_time_keyboard(),
    )


async def _process_recurrence_choice(
    target: Message, state: FSMContext, raw_value: str
):
    recurrence = _parse_recurrence(raw_value)
    if recurrence is None:
        await _ask_wizard_step(
            target,
            state,
            "Не розпізнав варіант. Оберіть кнопку або введіть: одноразове, щодня,"
            " щотижня, щомісяця або щороку.",
            reply_markup=repeat_time_keyboard(),
        )
        return

    await state.update_data(recurrence_type=recurrence)
    if recurrence == Reminder.RecurrenceType.NONE:
        await state.set_state(ReminderCreation.assignees)
        await _ask_wizard_step(
            target,
            state,
            "Кому призначити нагадування? Вкажіть @username або Telegram ID через кому.\n"
            "Якщо Ви створюєте нагадування для себе — натисніть Пропустити.",
            reply_markup=skip_keyboard(),
        )
    else:
        await state.set_state(ReminderCreation.interval)
        await _ask_wizard_step(
            target,
            state,
            "Кожен який інтервал? Введіть ціле число: 1 — кожен період, 2 — через один, тощо.",
            reply_markup=cancel_keyboard(),
        )


@router.callback_query(ReminderCreation.recurrence, F.data.startswith("rec:"))
async def creation_recurrence_callback(callback: CallbackQuery, state: FSMContext):
    raw_value = callback.data.split(":", 1)[1]
    await callback.answer()
    await _delete_wizard_message(callback.bot, callback.message.chat.id, state)
    await _process_recurrence_choice(callback.message, state, raw_value)


@router.message(ReminderCreation.recurrence)
async def creation_recurrence_message(message: Message, state: FSMContext):
    await _process_recurrence_choice(message, state, message.text or "")


@router.message(ReminderCreation.interval)
async def creation_interval(message: Message, state: FSMContext):
    try:
        interval = int((message.text or "").strip())
        if not 1 <= interval <= 365:
            raise ValueError
    except ValueError:
        await _ask_wizard_step(
            message,
            state,
            "Введіть ціле число від 1 до 365.",
            reply_markup=skip_keyboard(),
        )
        return
    await state.update_data(recurrence_interval=interval)
    await state.set_state(ReminderCreation.assignees)
    await _ask_wizard_step(
        message,
        state,
        "Кому призначити нагадування? Вкажіть @username або Telegram ID через кому.\n"
        "Якщо Ви створюєте нагадування для себе — натисніть Пропустити.",
        reply_markup=skip_keyboard(),
    )


async def _proceed_to_controllers(
    target: Message, state: FSMContext, assignee_ids: list[int]
):
    await state.update_data(assignee_ids=assignee_ids)
    data = await state.get_data()
    author_id = data.get("author_id")

    # Перевіряємо, чи є серед виконавців хтось, окрім автора
    has_other_assignees = any(uid != author_id for uid in assignee_ids)

    if has_other_assignees:
        text = (
            "Ви автоматично стаєте контролером у цьому нагадуванні.\n"
            "Додати інших контролерів? Вкажіть @username або Telegram ID через кому.\n"
            "Якщо ні — натисніть Пропустити."
        )
    else:
        text = (
            "Оскільки це персональне нагадування, ви не додані до контролерів автоматично (щоб не отримувати зайвих звітів).\n"
            "Якщо все ж бажаєте додати контролерів (зокрема себе) — вкажіть @username або Telegram ID через кому.\n"
            "Якщо ні — натисніть Пропустити."
        )

    await state.set_state(ReminderCreation.controllers)
    await _ask_wizard_step(target, state, text, reply_markup=skip_keyboard())


@router.callback_query(ReminderCreation.assignees, F.data == "skip")
async def skip_assignees_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await _delete_wizard_message(callback.bot, callback.message.chat.id, state)
    await _proceed_to_controllers(callback.message, state, [])


@router.message(ReminderCreation.assignees)
async def creation_assignees(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    assignees = []
    if raw.lower() not in {"пропустити", "ні", "немає", "-", "skip"}:
        assignees, missing = await sync_to_async(_resolve_users, thread_sensitive=True)(
            raw
        )
        if missing:
            await _ask_wizard_step(
                message,
                state,
                "Не знайшов зареєстрованих користувачів: "
                + ", ".join(missing)
                + ". Вони мають хоча б один раз запустити бота через /start. Спробуйте ще раз або натисніть Пропустити.",
                reply_markup=skip_keyboard(),
            )
            return
    await _proceed_to_controllers(message, state, [user.pk for user in assignees])


async def _finalize_reminder_creation(
    target: Message, state: FSMContext, nag_interval_minutes: int | None = None
):
    data = await state.get_data()
    author = await TelegramUser.objects.aget(pk=data["author_id"])
    assignees = await sync_to_async(
        lambda: list(TelegramUser.objects.filter(pk__in=data.get("assignee_ids", []))),
        thread_sensitive=True,
    )()
    controllers = await sync_to_async(
        lambda: list(
            TelegramUser.objects.filter(pk__in=data.get("controller_ids", []))
        ),
        thread_sensitive=True,
    )()
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
            nag_interval_minutes=nag_interval_minutes,
        )
    except (ValidationError, ValueError) as exc:
        await _delete_wizard_message(target.bot, target.chat.id, state)
        await target.delete()
        await state.clear()
        await target.answer(
            f"Не вдалося створити нагадування: {exc}\nСпробуйте ще раз."
        )
        return

    local_at = reminder.first_reminder_at.astimezone(ZoneInfo(reminder.timezone))
    recurrence_label = reminder.get_recurrence_type_display()
    nag_text = (
        f"\n🔔 Настирливе повторення: кожні {reminder.nag_interval_minutes} хв"
        if reminder.nag_interval_minutes
        else ""
    )
    await _delete_wizard_message(target.bot, target.chat.id, state)
    if target.from_user and not target.from_user.is_bot:
        try:
            await target.delete()
        except Exception:  # noqa: BLE001, S110
            pass
    await state.clear()
    final_msg = await target.answer(
        "✅ Нагадування створено!\n"
        f"«{reminder.title}»\n"
        f"Перше спрацювання: {local_at:%d.%m.%Y %H:%M} ({reminder.timezone})\n"
        f"Повторення: {recurrence_label}"
        + (
            f", інтервал {reminder.recurrence_interval}"
            if reminder.recurrence_type != Reminder.RecurrenceType.NONE
            else ""
        )
        + nag_text
        + f"\nВиконавців: {len(assignees) if assignees else 1}."
    )
    asyncio.create_task(_delete_message_after_delay(final_msg, 5))


async def _proceed_to_nagging(
    target: Message, state: FSMContext, controller_ids: list[int]
):
    await state.update_data(controller_ids=controller_ids)
    await state.set_state(ReminderCreation.nagging)
    await _ask_wizard_step(
        target,
        state,
        "Увімкнути настирливі нагадування? Нагадування будуть дублюватися через заданий термін, поки Ви їх не закриєте.",
        reply_markup=nagging_keyboard(),
    )


@router.callback_query(ReminderCreation.controllers, F.data == "skip")
async def skip_controllers_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await _delete_wizard_message(callback.bot, callback.message.chat.id, state)
    await _proceed_to_nagging(callback.message, state, [])


@router.message(ReminderCreation.controllers)
async def creation_controllers(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    controllers = []
    if raw.lower() not in {"пропустити", "ні", "немає", "-", "skip"}:
        controllers, missing = await sync_to_async(
            _resolve_users, thread_sensitive=True
        )(raw)
        if missing:
            await _ask_wizard_step(
                message,
                state,
                "Не знайшов зареєстрованих користувачів: "
                + ", ".join(missing)
                + ". Вони мають спочатку виконати /start. Спробуйте ще раз або натисніть Пропустити.",
                reply_markup=skip_keyboard(),
            )
            return
    await _proceed_to_nagging(message, state, [user.pk for user in controllers])


@router.callback_query(F.data.startswith("rem:"))
async def reminder_response_callback(callback: CallbackQuery, state: FSMContext):
    """Обробляє кнопки лише для власника відповіді та актуального повідомлення."""
    try:
        _, raw_id, command = callback.data.split(":", 2)
        response_id = int(raw_id)
    except AttributeError, TypeError, ValueError:
        await callback.answer("Некоректна команда.", show_alert=True)
        return
    if callback.message is None:
        await callback.answer("Повідомлення недоступне.", show_alert=True)
        return

    if command == "custom":
        response = await sync_to_async(
            _get_response_for_custom_defer, thread_sensitive=True
        )(response_id, callback.from_user.id)
        if response is None:
            await callback.answer(
                "Це нагадування не належить вам або вже неактуальне.", show_alert=True
            )
            return
        await state.clear()
        await state.update_data(
            custom_response_id=response_id,
            source_message_id=callback.message.message_id,
            reminder_timezone=response.occurrence.reminder.timezone,
        )
        await state.set_state(CustomDefer.waiting_datetime)
        await callback.message.answer(
            "Введіть дату й час повторного нагадування у часовому поясі "
            f"{response.occurrence.reminder.timezone}.\n"
            "Формат: ДД.ММ.РРРР ГГ:ХХ. Максимум — 7 днів від поточного моменту.",
            reply_markup=cancel_keyboard(),
        )
        await callback.answer()
        return

    if command == "choose":
        await callback.message.edit_reply_markup(
            reply_markup=defer_keyboard(response_id)
        )
        await callback.answer("Оберіть час відтермінування.")
        return
    if command == "back":
        await callback.message.edit_reply_markup(
            reply_markup=response_keyboard(response_id)
        )
        await callback.answer()
        return

    if command == "completed":
        action, minutes = "completed", None
    elif command == "rejected":
        action, minutes = "rejected", None
    elif command == "defer30":
        action, minutes = "defer", 30
    elif command.startswith("d") and command[1:].isdigit():
        action, minutes = "defer", int(command[1:])
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
        round_trip = (
            candidate.astimezone(ZoneInfo("UTC")).astimezone(zone).replace(tzinfo=None)
        )
        if round_trip != local_dt:
            await message.answer(
                "Цей локальний час не існує через перехід на літній час. Введіть інший час.",
                reply_markup=cancel_keyboard(),
            )
            return
        defer_until = candidate.astimezone(ZoneInfo("UTC"))
        now = django_timezone.now()
        seconds = (defer_until - now).total_seconds()
        if seconds <= 0:
            await message.answer(
                "Час має бути в майбутньому. Спробуйте ще раз.",
                reply_markup=cancel_keyboard(),
            )
            return
        if seconds > 7 * 24 * 60 * 60:
            await message.answer(
                "Можна відтермінувати не більш ніж на 7 днів.",
                reply_markup=cancel_keyboard(),
            )
            return
        response_id = data["custom_response_id"]
        source_message_id = data["source_message_id"]
    except ValueError, KeyError, ZoneInfoNotFoundError:
        await message.answer(
            "Не вдалося розпізнати дату. Формат: ДД.ММ.РРРР ГГ:ХХ.",
            reply_markup=cancel_keyboard(),
        )
        return

    await _apply_and_notify(
        message,
        response_id,
        "defer",
        None,
        defer_until,
        telegram_message_id=source_message_id,
    )
    await state.clear()


async def _apply_and_notify(
    event,
    response_id: int,
    action: str,
    minutes: int | None,
    defer_until,
    telegram_message_id=None,
):
    actor_id = event.from_user.id
    source_message_id = telegram_message_id
    if source_message_id is None and isinstance(event, CallbackQuery) and event.message:
        source_message_id = event.message.message_id
    try:
        response, audit_event = await sync_to_async(
            apply_response_action, thread_sensitive=True
        )(
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
            await event.answer(
                "Не вдалося зберегти відповідь. Спробуйте ще раз.", show_alert=True
            )
        else:
            await event.answer("Не вдалося зберегти відповідь. Спробуйте ще раз.")
        return

    if isinstance(event, CallbackQuery):
        if event.message:
            await event.message.edit_reply_markup(reply_markup=None)
        await event.answer("Відповідь збережено.")
        bot = event.bot
    else:
        await event.answer("✅ Час відтермінування збережено.")
        bot = event.bot

    reminder = response.occurrence.reminder
    should_notify = (
        reminder.notification_policy == Reminder.NotificationPolicy.ALL_ACTIONS
        or action == "rejected"
        or bool(audit_event.details.get("late"))
    )
    if should_notify:
        controller_ids = await sync_to_async(
            get_controller_telegram_ids, thread_sensitive=True
        )(reminder.pk)
        actor_name = (
            response.assignee.full_name
            or response.assignee.username
            or str(response.assignee.telegram_id)
        )
        if action == "defer":
            local_defer = audit_event.deferred_until.astimezone(
                ZoneInfo(reminder.timezone)
            )
            status_text = f"відтермінував(ла) «{reminder.title}» до {local_defer:%d.%m.%Y %H:%M} ({reminder.timezone})"
        else:
            verb = "виконав(ла)" if action == "completed" else "відхилив(ла)"
            late_suffix = " із запізненням" if audit_event.details.get("late") else ""
            status_text = f"{verb}{late_suffix} нагадування «{reminder.title}»"
        for telegram_id in controller_ids:
            try:
                await bot.send_message(telegram_id, f"ℹ️ {actor_name} {status_text}.")
            except Exception:
                logger.exception("Не вдалося сповістити контролера %s", telegram_id)


@router.callback_query(ReminderCreation.nagging, F.data.startswith("nag:"))
async def creation_nagging_callback(callback: CallbackQuery, state: FSMContext):
    choice = callback.data.split(":", 1)[1]
    await callback.answer()
    await _delete_wizard_message(callback.bot, callback.message.chat.id, state)

    if choice == "skip":
        await _finalize_reminder_creation(
            callback.message, state, nag_interval_minutes=None
        )
    elif choice in {"5", "10"}:
        await _finalize_reminder_creation(
            callback.message, state, nag_interval_minutes=int(choice)
        )
    elif choice == "custom":
        await state.set_state(ReminderCreation.nagging_custom)
        await _ask_wizard_step(
            callback.message,
            state,
            "Введіть інтервал дублювання у хвилинах (ціле число від 1 до 1440, наприклад: 15):",
            reply_markup=cancel_keyboard(),
        )


@router.message(ReminderCreation.nagging_custom)
async def creation_nagging_custom(message: Message, state: FSMContext):
    try:
        minutes = int((message.text or "").strip())
        if not 1 <= minutes <= 1440:
            raise ValueError
    except ValueError:
        await _ask_wizard_step(
            message,
            state,
            "Введіть ціле число від 1 до 1440 (хвилин).",
            reply_markup=cancel_keyboard(),
        )
        return
    await _finalize_reminder_creation(message, state, nag_interval_minutes=minutes)


async def _delete_wizard_message(bot, chat_id: int, state: FSMContext):
    """Повністю видаляє попереднє повідомлення бота у майстрі створення."""
    data = await state.get_data()
    last_msg_id = data.get("wizard_message_id")
    if not last_msg_id:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=last_msg_id)
    except Exception:  # noqa: BLE001
        # Якщо повідомлення вже видалене або старше 48 годин — просто прибираємо кнопки
        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=last_msg_id, reply_markup=None
            )
        except Exception:  # noqa S110
            pass
    await state.update_data(wizard_message_id=None)


async def _ask_wizard_step(
    target: Message, state: FSMContext, text: str, reply_markup=None
):
    """Видаляє попереднє питання бота, повідомлення користувача, надсилає нове та зберігає його ID."""
    await _delete_wizard_message(target.bot, target.chat.id, state)

    # Видаляємо текстове повідомлення користувача (окрім стартової команди/кнопки, якщо бажаєте)
    if target.from_user and not target.from_user.is_bot:
        try:
            await target.delete()
        except Exception:  # noqa: BLE001, S110
            pass

    sent = await target.answer(text, reply_markup=reply_markup)
    await state.update_data(wizard_message_id=sent.message_id)
    return sent


async def _delete_message_after_delay(message: Message, delay: int = 5):
    """Видаляє повідомлення через задану кількість секунд у фоні."""
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception:  # noqa: BLE001, S110
        pass


async def _finish_edit_step(target: Message, state: FSMContext, text: str):
    data = await state.get_data()
    reminder_id = data.get("edit_reminder_id")
    await _delete_wizard_message(target.bot, target.chat.id, state)
    if target.from_user and not target.from_user.is_bot:
        try:
            await target.delete()
        except Exception:  # noqa: BLE001, S110
            pass
    await state.set_state(None)
    msg = await target.answer(
        text,
        reply_markup=reminder_edit_menu_keyboard(reminder_id) if reminder_id else None,
    )


@router.message(ReminderManagement.edit_datetime)
async def process_edit_datetime(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        local_dt = _parse_local_datetime(message.text or "")
        ok = await sync_to_async(update_reminder_schedule, thread_sensitive=True)(
            data["edit_reminder_id"], message.from_user.id, local_dt
        )
    except (ValueError, ValidationError) as exc:
        await _ask_wizard_step(message, state, str(exc), reply_markup=cancel_keyboard())
        return
    if not ok:
        await _finish_edit_step(message, state, "❌ Не вдалося оновити час.")
        return
    await _finish_edit_step(message, state, "✅ Час спрацювання оновлено.")


@router.callback_query(ReminderManagement.edit_recurrence, F.data.startswith("rec:"))
async def process_edit_recurrence(callback: CallbackQuery, state: FSMContext):
    raw_value = callback.data.split(":", 1)[1]
    recurrence = _parse_recurrence(raw_value)
    await callback.answer()
    if recurrence == Reminder.RecurrenceType.NONE:
        data = await state.get_data()
        await sync_to_async(update_reminder_recurrence, thread_sensitive=True)(
            data["edit_reminder_id"], callback.from_user.id, recurrence, 1
        )
        await _finish_edit_step(
            callback.message, state, "✅ Нагадування змінено на одноразове."
        )
    else:
        await state.update_data(edit_rec_type=recurrence)
        await state.set_state(ReminderManagement.edit_interval)
        await _ask_wizard_step(
            callback.message,
            state,
            "Введіть інтервал повторення (ціле число від 1 до 365):",
            reply_markup=cancel_keyboard(),
        )


@router.message(ReminderManagement.edit_interval)
async def process_edit_interval(message: Message, state: FSMContext):
    try:
        interval = int((message.text or "").strip())
        if not 1 <= interval <= 365:
            raise ValueError
    except ValueError:
        await _ask_wizard_step(
            message,
            state,
            "Введіть ціле число від 1 до 365.",
            reply_markup=cancel_keyboard(),
        )
        return
    data = await state.get_data()
    await sync_to_async(update_reminder_recurrence, thread_sensitive=True)(
        data["edit_reminder_id"], message.from_user.id, data["edit_rec_type"], interval
    )
    await _finish_edit_step(message, state, "✅ Повторення оновлено.")


@router.callback_query(ReminderManagement.edit_nagging, F.data.startswith("nag:"))
async def process_edit_nagging(callback: CallbackQuery, state: FSMContext):
    choice = callback.data.split(":", 1)[1]
    await callback.answer()
    data = await state.get_data()
    if choice == "custom":
        await state.set_state(ReminderManagement.edit_nagging_custom)
        await _ask_wizard_step(
            callback.message,
            state,
            "Введіть інтервал дублювання у хвилинах (від 1 до 1440):",
            reply_markup=cancel_keyboard(),
        )
        return

    minutes = None if choice == "skip" else int(choice)
    await sync_to_async(update_reminder_nagging, thread_sensitive=True)(
        data["edit_reminder_id"], callback.from_user.id, minutes
    )
    status = f"кожні {minutes} хв" if minutes else "вимкнено"
    await _finish_edit_step(
        callback.message, state, f"✅ Настирливі нагадування: {status}."
    )


@router.message(ReminderManagement.edit_nagging_custom)
async def process_edit_nagging_custom(message: Message, state: FSMContext):
    try:
        minutes = int((message.text or "").strip())
        if not 1 <= minutes <= 1440:
            raise ValueError
    except ValueError:
        await _ask_wizard_step(
            message,
            state,
            "Введіть ціле число від 1 до 1440.",
            reply_markup=cancel_keyboard(),
        )
        return
    data = await state.get_data()
    await sync_to_async(update_reminder_nagging, thread_sensitive=True)(
        data["edit_reminder_id"], message.from_user.id, minutes
    )
    await _finish_edit_step(
        message, state, f"✅ Настирливі нагадування: кожні {minutes} хв."
    )


@router.callback_query(ReminderManagement.edit_assignees, F.data == "skip")
@router.callback_query(ReminderManagement.edit_controllers, F.data == "skip")
async def process_edit_participants_skip(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    await sync_to_async(update_reminder_participants, thread_sensitive=True)(
        data["edit_reminder_id"],
        callback.from_user.id,
        role=data["edit_role"],
        users=[],
    )
    await _finish_edit_step(callback.message, state, "✅ Список учасників оновлено.")


@router.message(ReminderManagement.edit_assignees)
@router.message(ReminderManagement.edit_controllers)
async def process_edit_participants_text(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    users, missing = await sync_to_async(_resolve_users, thread_sensitive=True)(raw)
    if missing:
        await _ask_wizard_step(
            message,
            state,
            "Не знайдено користувачів: " + ", ".join(missing) + ". Спробуйте ще раз:",
            reply_markup=skip_keyboard(),
        )
        return
    data = await state.get_data()
    await sync_to_async(update_reminder_participants, thread_sensitive=True)(
        data["edit_reminder_id"],
        message.from_user.id,
        role=data["edit_role"],
        users=users,
    )
    await _finish_edit_step(message, state, "✅ Список учасників оновлено.")


async def _render_reminder_detail_view(
    callback: CallbackQuery, state: FSMContext, reminder_id: int, mode: str
):
    telegram_id = callback.from_user.id
    detail = await sync_to_async(get_reminder_detail, thread_sensitive=True)(
        reminder_id, telegram_id
    )
    if detail is None:
        await callback.answer(
            "Нагадування не знайдено або у вас немає доступу.", show_alert=True
        )
        return

    await state.update_data(reminder_list_mode=mode)
    zone = ZoneInfo(detail["timezone"])
    first = detail["first_reminder_at"].astimezone(zone).strftime("%d.%m.%Y %H:%M")
    next_at = detail["next_reminder_at"]
    next_text = (
        next_at.astimezone(zone).strftime("%d.%m.%Y %H:%M") if next_at else "немає"
    )
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
                name = full_name or (
                    f"@{username}" if username else str(participant_id)
                )
                lines.append(
                    f"• {'Контролер' if role == 'controller' else 'Виконавець'}: {name}"
                )
    if detail.get("nag_interval_minutes"):
        lines.append(
            f"🔔 Настирливе нагадування: кожні {detail['nag_interval_minutes']} хв"
        )
    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=reminder_detail_keyboard(
            reminder_id,
            can_manage=detail["is_controller"],
            is_active=detail["is_active"],
        ),
    )
    await callback.answer()


@router.message(ReminderManagement.edit_timeout)
async def process_edit_timeout(message: Message, state: FSMContext):
    try:
        minutes = int((message.text or "").strip())
        if not 1 <= minutes <= 10080:
            raise ValueError
    except ValueError:
        await _ask_wizard_step(
            message,
            state,
            "Введіть ціле число від 1 до 10080 (хвилин).",
            reply_markup=cancel_keyboard(),
        )
        return

    data = await state.get_data()
    try:
        ok = await sync_to_async(update_reminder_timeout, thread_sensitive=True)(
            data["edit_reminder_id"], message.from_user.id, minutes
        )
    except ValueError as exc:
        await _ask_wizard_step(message, state, str(exc), reply_markup=cancel_keyboard())
        return

    if not ok:
        await _finish_edit_step(message, state, "❌ Не вдалося оновити тайм-аут.")
        return
    await _finish_edit_step(
        message, state, f"✅ Тайм-аут відповіді змінено на {minutes} хв."
    )
