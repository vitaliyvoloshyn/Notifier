"""Сервіси створення нагадувань і календарного розрахунку повторень."""

from calendar import monthrange
from collections.abc import Iterable
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone as django_timezone

from .models import (
    Reminder,
    ReminderOccurrence,
    ReminderParticipant,
    ReminderResponse,
)


def get_zone(timezone_name: str) -> ZoneInfo:
    """Повертає IANA timezone або зрозумілу помилку валідації."""
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, TypeError, ValueError) as exc:
        raise ValidationError(
            {"timezone": "Вкажіть коректний IANA часовий пояс."}
        ) from exc


def _localize_wall_time(
    value: datetime, zone: ZoneInfo, *, shift_nonexistent: bool
) -> datetime:
    """Перетворює локальний naive datetime на aware datetime.

    Для неоднозначного часу під час осіннього переходу обирається перше
    входження (fold=0). Для неіснуючого часу навесні за замовчуванням
    повертається помилка; для автоматично обчисленого повторення можна
    пересунути час уперед до першої існуючої хвилини.
    """
    candidate = value.replace(tzinfo=zone, fold=0)
    round_trip = (
        candidate.astimezone(ZoneInfo("UTC")).astimezone(zone).replace(tzinfo=None)
    )
    if round_trip == value:
        return candidate

    if not shift_nonexistent:
        raise ValidationError(
            "Цей локальний час не існує через перехід на літній час. Оберіть інший час."
        )

    shifted = value
    for _ in range(181):
        shifted += timedelta(minutes=1)
        candidate = shifted.replace(tzinfo=zone, fold=0)
        round_trip = (
            candidate.astimezone(ZoneInfo("UTC")).astimezone(zone).replace(tzinfo=None)
        )
        if round_trip == shifted:
            return candidate
    raise ValidationError(
        "Не вдалося визначити коректний локальний час для повторення."
    )


def normalize_first_datetime(value: datetime, timezone_name: str) -> datetime:
    """Нормалізує першу дату: naive трактує як локальну, aware — як момент часу."""
    if not isinstance(value, datetime):
        raise ValidationError({"first_reminder_at": "Потрібно передати дату й час."})
    zone = get_zone(timezone_name)
    if django_timezone.is_aware(value):
        return value.astimezone(ZoneInfo("UTC"))
    return _localize_wall_time(value, zone, shift_nonexistent=False).astimezone(
        ZoneInfo("UTC")
    )


def _add_months(
    year: int, month: int, months: int, day: int, wall_time: time
) -> datetime:
    month_index = year * 12 + (month - 1) + months
    target_year, zero_based_month = divmod(month_index, 12)
    target_month = zero_based_month + 1
    target_day = min(day, monthrange(target_year, target_month)[1])
    return datetime.combine(
        datetime(target_year, target_month, target_day).date(),
        wall_time,
    )


def calculate_next_occurrence(
    reminder: Reminder, after: datetime | None = None
) -> datetime | None:
    """Обчислює наступне спрацювання з локальним календарним кроком.

    Якщо `after` не задано, перше спрацювання використовується як база,
    від якої обчислюється наступне. Для щомісячного/щорічного повторення
    зберігається початковий день
    (наприклад, 31 січня -> 28 лютого -> 31 березня).
    """
    if reminder.recurrence_type == Reminder.RecurrenceType.NONE:
        return None

    zone = get_zone(reminder.timezone)
    anchor_utc = reminder.first_reminder_at
    if django_timezone.is_naive(anchor_utc):
        raise ValidationError("first_reminder_at має містити часовий пояс.")
    anchor_local = anchor_utc.astimezone(zone)

    if after is None:
        after_local = anchor_local
    else:
        if django_timezone.is_naive(after):
            raise ValidationError("Параметр after має містити часовий пояс.")
        after_local = after.astimezone(zone)

    interval = reminder.recurrence_interval
    anchor_wall_time = anchor_local.timetz().replace(tzinfo=None)
    anchor_date = anchor_local.date()
    after_date = after_local.date()

    if reminder.recurrence_type == Reminder.RecurrenceType.DAILY:
        elapsed = (after_date - anchor_date).days
        periods = max(1, elapsed // interval + 1)
        target = datetime.combine(
            anchor_date + timedelta(days=periods * interval), anchor_wall_time
        )
    elif reminder.recurrence_type == Reminder.RecurrenceType.WEEKLY:
        elapsed = (after_date - anchor_date).days
        periods = max(1, elapsed // (7 * interval) + 1)
        target = datetime.combine(
            anchor_date + timedelta(days=periods * 7 * interval), anchor_wall_time
        )
    elif reminder.recurrence_type == Reminder.RecurrenceType.MONTHLY:
        month_delta = (
            (after_local.year - anchor_local.year) * 12
            + after_local.month
            - anchor_local.month
        )
        periods = max(1, month_delta // interval + 1)
        target = _add_months(
            anchor_local.year,
            anchor_local.month,
            periods * interval,
            anchor_local.day,
            anchor_wall_time,
        )
    elif reminder.recurrence_type == Reminder.RecurrenceType.YEARLY:
        year_delta = after_local.year - anchor_local.year
        periods = max(1, year_delta // interval + 1)
        target_year = anchor_local.year + periods * interval
        target_day = min(
            anchor_local.day, monthrange(target_year, anchor_local.month)[1]
        )
        target = datetime.combine(
            datetime(target_year, anchor_local.month, target_day).date(),
            anchor_wall_time,
        )
    else:
        raise ValidationError({"recurrence_type": "Невідомий тип повторення."})

    return _localize_wall_time(target, zone, shift_nonexistent=True).astimezone(
        ZoneInfo("UTC")
    )


@transaction.atomic
def create_reminder(
    *,
    author,
    title: str,
    first_reminder_at: datetime,
    recurrence_type: str = Reminder.RecurrenceType.NONE,
    recurrence_interval: int = 1,
    assignees: Iterable = (),
    controllers: Iterable = (),
    timezone_name: str | None = None,
    notification_policy: str = Reminder.NotificationPolicy.EXCEPTIONS_ONLY,
    response_timeout_minutes: int = 60,
    nag_interval_minutes: int | None = None,
    occurrence_ttl_minutes: int | None = None
) -> Reminder:
    """Створює правило, перше спрацювання, учасників і персональні відповіді.

    `assignees` та `controllers` — ітеровані набори об'єктів TelegramUser.
    Якщо виконавців немає, відповідь створюється для автора.
    """
    clean_title = (title or "").strip()
    if not clean_title:
        raise ValidationError({"title": "Назва нагадування не може бути порожньою."})
    if len(clean_title) > 255:
        raise ValidationError({"title": "Назва має містити не більше 255 символів."})
    if recurrence_type not in Reminder.RecurrenceType.values:
        raise ValidationError({"recurrence_type": "Невідомий тип повторення."})
    if (
        not isinstance(recurrence_interval, int)
        or isinstance(recurrence_interval, bool)
        or recurrence_interval < 1
    ):
        raise ValidationError(
            {"recurrence_interval": "Інтервал має бути цілим числом від 1."}
        )
    if (
        not isinstance(response_timeout_minutes, int)
        or isinstance(response_timeout_minutes, bool)
        or response_timeout_minutes < 1
    ):
        raise ValidationError(
            {
                "response_timeout_minutes": "Час очікування має бути цілим числом від 1 хвилини."
            }
        )
    if notification_policy not in Reminder.NotificationPolicy.values:
        raise ValidationError(
            {"notification_policy": "Невідома політика інформування."}
        )
    if nag_interval_minutes is not None:  # noqa: SIM102
        if (
            not isinstance(nag_interval_minutes, int)
            or isinstance(nag_interval_minutes, bool)
            or not 1 <= nag_interval_minutes <= 1440
        ):
            raise ValidationError(
                {
                    "nag_interval_minutes": "Інтервал настирливих нагадувань має бути від 1 до 1440 хвилин."
                }
            )
    if occurrence_ttl_minutes is not None:  # noqa: SIM102
        if (
            not isinstance(occurrence_ttl_minutes, int)
            or isinstance(occurrence_ttl_minutes, bool)
            or not 1 <= occurrence_ttl_minutes <= 43200
        ):
            raise ValidationError(
                {
                    "occurrence_ttl_minutes": "Інтервал «життя» нагадувань має бути від 1 до 43200 хвилин."
                }
            )
    

    selected_timezone = (
        timezone_name or getattr(author, "timezone", "Europe/Kyiv") or "Europe/Kyiv"
    )
    get_zone(selected_timezone)
    first_at = normalize_first_datetime(first_reminder_at, selected_timezone)

    reminder = Reminder.objects.create(
        title=clean_title,
        author=author,
        first_reminder_at=first_at,
        next_reminder_at=first_at,
        timezone=selected_timezone,
        recurrence_type=recurrence_type,
        recurrence_interval=recurrence_interval,
        notification_policy=notification_policy,
        response_timeout_minutes=response_timeout_minutes,
        nag_interval_minutes=nag_interval_minutes,
        occurrence_ttl_minutes=occurrence_ttl_minutes
    )

    assignee_map = {user.pk: user for user in assignees}

    # Перевіряємо, чи є серед виконавців хтось, крім самого автора
    has_other_assignees = any(pk != author.pk for pk in assignee_map)

    controller_map = {user.pk: user for user in controllers}

    # Якщо нагадування створено для інших людей — автор стає контролером автоматично.
    # Якщо нагадування персональне — автор буде контролером, тільки якщо сам себе додав.
    if has_other_assignees:
        controller_map[author.pk] = author

    for user in controller_map.values():
        ReminderParticipant.objects.create(
            reminder=reminder,
            user=user,
            role=ReminderParticipant.Role.CONTROLLER,
            added_by=author,
        )
    for user in assignee_map.values():
        ReminderParticipant.objects.create(
            reminder=reminder,
            user=user,
            role=ReminderParticipant.Role.ASSIGNEE,
            added_by=author,
        )

    expires_at = (
        first_at + timedelta(minutes=occurrence_ttl_minutes)
        if occurrence_ttl_minutes
        else None
    )
    occurrence = ReminderOccurrence.objects.create(
        reminder=reminder,
        scheduled_at=first_at,
        expires_at=expires_at,
    )
    response_users = assignee_map.values() if assignee_map else (author,)
    ReminderResponse.objects.bulk_create(
        [
            ReminderResponse(occurrence=occurrence, assignee=user)
            for user in response_users
        ]
    )
    return reminder
