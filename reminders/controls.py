"""Операції керування нагадуваннями з перевіркою ролей."""

from django.db import transaction
from django.db.models import Q

from .models import Reminder, ReminderParticipant, ReminderResponse


def _can_control(reminder: Reminder, telegram_id: int) -> bool:
    if reminder.author.telegram_id == telegram_id:
        return True
    return reminder.participants.filter(
        user__telegram_id=telegram_id,
        role=ReminderParticipant.Role.CONTROLLER,
    ).exists()


def list_reminders(telegram_id: int, mode: str = "controlled") -> list[dict]:
    """Повертає нагадування, доступні в обраному розділі меню."""
    if mode == "controlled":
        queryset = Reminder.objects.filter(
            participants__user__telegram_id=telegram_id,
            participants__role=ReminderParticipant.Role.CONTROLLER,
        )
    elif mode == "assigned":
        queryset = Reminder.objects.filter(
            Q(
                participants__user__telegram_id=telegram_id,
                participants__role=ReminderParticipant.Role.ASSIGNEE,
            )
            | Q(occurrences__responses__assignee__telegram_id=telegram_id)
        )
    else:
        return []

    reminders = (
        queryset.select_related("author")
        .distinct()
        .order_by("-is_active", "next_reminder_at", "-created_at")
    )
    return [
        {
            "id": item.pk,
            "title": item.title,
            "is_active": item.is_active,
            "next_reminder_at": item.next_reminder_at,
            "timezone": item.timezone,
            "can_manage": _can_control(item, telegram_id),
        }
        for item in reminders
    ]


def get_reminder_detail(reminder_id: int, telegram_id: int) -> dict | None:
    reminder = Reminder.objects.select_related("author").filter(pk=reminder_id).first()
    if reminder is None:
        return None

    is_controller = _can_control(reminder, telegram_id)
    is_assignee = (
        reminder.participants.filter(
            user__telegram_id=telegram_id,
            role=ReminderParticipant.Role.ASSIGNEE,
        ).exists()
        or reminder.occurrences.filter(
            responses__assignee__telegram_id=telegram_id
        ).exists()
    )
    if not is_controller and not is_assignee:
        return None

    participants = []
    if is_controller:
        participants = list(
            reminder.participants.select_related("user")
            .order_by("role", "user__full_name")
            .values_list(
                "role", "user__full_name", "user__username", "user__telegram_id"
            )
        )

    response_counts = {
        "pending": 0,
        "completed": 0,
        "rejected": 0,
        "expired": 0,
    }
    responses = ReminderResponse.objects.filter(occurrence__reminder=reminder)
    if not is_controller:
        responses = responses.filter(assignee__telegram_id=telegram_id)
    for status in response_counts:
        response_counts[status] = responses.filter(status=status).count()

    return {
        "id": reminder.pk,
        "title": reminder.title,
        "is_active": reminder.is_active,
        "first_reminder_at": reminder.first_reminder_at,
        "next_reminder_at": reminder.next_reminder_at,
        "timezone": reminder.timezone,
        "recurrence_type": reminder.get_recurrence_type_display(),
        "recurrence_interval": reminder.recurrence_interval,
        "notification_policy": reminder.get_notification_policy_display(),
        "response_timeout_minutes": reminder.response_timeout_minutes,
        "author": reminder.author.full_name
        or reminder.author.username
        or str(reminder.author.telegram_id),
        "is_controller": is_controller,
        "participants": participants,
        "response_counts": response_counts,
        "nag_interval_minutes": reminder.nag_interval_minutes,
    }


@transaction.atomic
def set_reminder_active(reminder_id: int, telegram_id: int, active: bool) -> bool:
    reminder = (
        Reminder.objects.select_for_update()
        .select_related("author")
        .filter(pk=reminder_id)
        .first()
    )
    if reminder is None or not _can_control(reminder, telegram_id):
        return False
    if active and reminder.next_reminder_at is None:
        return False
    reminder.is_active = active
    reminder.save(update_fields=["is_active", "updated_at"])
    return True


@transaction.atomic
def rename_reminder(reminder_id: int, telegram_id: int, title: str) -> bool:
    clean_title = (title or "").strip()
    if not clean_title or len(clean_title) > 255:
        raise ValueError("Назва має містити від 1 до 255 символів.")
    reminder = (
        Reminder.objects.select_for_update()
        .select_related("author")
        .filter(pk=reminder_id)
        .first()
    )
    if reminder is None or not _can_control(reminder, telegram_id):
        return False
    reminder.title = clean_title
    reminder.save(update_fields=["title", "updated_at"])
    return True


@transaction.atomic
def delete_reminder(reminder_id: int, telegram_id: int) -> bool:
    reminder = (
        Reminder.objects.select_for_update()
        .select_related("author")
        .filter(pk=reminder_id)
        .first()
    )
    if reminder is None or not _can_control(reminder, telegram_id):
        return False
    reminder.delete()
    return True


# Додайте необхідні імпорти на початку reminders/controls.py:
from datetime import datetime

from django.utils import timezone as django_timezone

from .models import ReminderOccurrence
from .services import calculate_next_occurrence, normalize_first_datetime


@transaction.atomic
def update_reminder_schedule(
    reminder_id: int, telegram_id: int, local_dt: datetime
) -> bool:
    """Оновлює час наступного спрацювання та переплановує активне Occurrence."""
    reminder = (
        Reminder.objects.select_for_update()
        .select_related("author")
        .filter(pk=reminder_id)
        .first()
    )
    if reminder is None or not _can_control(reminder, telegram_id):
        return False

    utc_dt = normalize_first_datetime(local_dt, reminder.timezone)
    if utc_dt <= django_timezone.now():
        raise ValueError("Новий час має бути в майбутньому.")

    # Скасовуємо незавершені заплановані спрацювання на старий час
    reminder.occurrences.filter(
        status__in=[
            ReminderOccurrence.Status.SCHEDULED,
            ReminderOccurrence.Status.FAILED,
        ]
    ).update(status=ReminderOccurrence.Status.CANCELLED)

    reminder.next_reminder_at = utc_dt
    if reminder.first_reminder_at > django_timezone.now():
        reminder.first_reminder_at = utc_dt
    reminder.is_active = True
    reminder.save(
        update_fields=[
            "first_reminder_at",
            "next_reminder_at",
            "is_active",
            "updated_at",
        ]
    )

    occurrence, _ = ReminderOccurrence.objects.get_or_create(
        reminder=reminder,
        scheduled_at=utc_dt,
        defaults={"status": ReminderOccurrence.Status.SCHEDULED},
    )
    if occurrence.status == ReminderOccurrence.Status.CANCELLED:
        occurrence.status = ReminderOccurrence.Status.SCHEDULED
        occurrence.save(update_fields=["status"])

    # Створюємо відповіді для виконавців, якщо їх ще немає для цього спрацювання
    if not occurrence.responses.exists():
        assignee_ids = list(
            reminder.participants.filter(
                role=ReminderParticipant.Role.ASSIGNEE
            ).values_list("user_id", flat=True)
        )
        if not assignee_ids:
            assignee_ids = [reminder.author_id]
        ReminderResponse.objects.bulk_create(
            [
                ReminderResponse(occurrence=occurrence, assignee_id=uid)
                for uid in assignee_ids
            ]
        )
    return True


@transaction.atomic
def update_reminder_recurrence(
    reminder_id: int,
    telegram_id: int,
    recurrence_type: str,
    recurrence_interval: int = 1,
) -> bool:
    """Оновлює періодичність нагадування."""
    reminder = (
        Reminder.objects.select_for_update()
        .select_related("author")
        .filter(pk=reminder_id)
        .first()
    )
    if reminder is None or not _can_control(reminder, telegram_id):
        return False

    reminder.recurrence_type = recurrence_type
    reminder.recurrence_interval = (
        1 if recurrence_type == Reminder.RecurrenceType.NONE else recurrence_interval
    )

    # Якщо наступне спрацювання не заплановане, але нагадування стало повторюваним — рахуємо нову дату
    if (
        reminder.next_reminder_at is None
        and recurrence_type != Reminder.RecurrenceType.NONE
    ):
        next_at = calculate_next_occurrence(reminder, django_timezone.now())
        reminder.next_reminder_at = next_at
        if next_at:
            occ = ReminderOccurrence.objects.create(
                reminder=reminder, scheduled_at=next_at
            )
            assignee_ids = list(
                reminder.participants.filter(
                    role=ReminderParticipant.Role.ASSIGNEE
                ).values_list("user_id", flat=True)
            ) or [reminder.author_id]
            ReminderResponse.objects.bulk_create(
                [
                    ReminderResponse(occurrence=occ, assignee_id=uid)
                    for uid in assignee_ids
                ]
            )

    reminder.save(
        update_fields=[
            "recurrence_type",
            "recurrence_interval",
            "next_reminder_at",
            "updated_at",
        ]
    )
    return True


@transaction.atomic
def update_reminder_nagging(
    reminder_id: int, telegram_id: int, nag_interval_minutes: int | None
) -> bool:
    """Вмикає, змінює інтервал або вимикає настирливі нагадування."""
    reminder = (
        Reminder.objects.select_for_update()
        .select_related("author")
        .filter(pk=reminder_id)
        .first()
    )
    if reminder is None or not _can_control(reminder, telegram_id):
        return False

    reminder.nag_interval_minutes = nag_interval_minutes
    reminder.save(update_fields=["nag_interval_minutes", "updated_at"])

    # Якщо вимкнули настирливість — знімаємо заплановані дублі з поточних відкритих відповідей
    if nag_interval_minutes is None:
        ReminderResponse.objects.filter(
            occurrence__reminder=reminder,
            status__in=[
                ReminderResponse.Status.PENDING,
                ReminderResponse.Status.EXPIRED,
            ],
        ).update(next_nag_at=None)
    return True


@transaction.atomic
def update_reminder_participants(
    reminder_id: int, telegram_id: int, *, role: str, users: list
) -> bool:
    """Оновлює список виконавців або контролерів нагадування."""
    reminder = (
        Reminder.objects.select_for_update()
        .select_related("author")
        .filter(pk=reminder_id)
        .first()
    )
    if reminder is None or not _can_control(reminder, telegram_id):
        return False

    reminder.participants.filter(role=role).delete()
    user_map = {u.pk: u for u in users}

    if role == ReminderParticipant.Role.CONTROLLER:
        has_other_assignees = (
            reminder.participants.filter(role=ReminderParticipant.Role.ASSIGNEE)
            .exclude(user=reminder.author)
            .exists()
        )
        if has_other_assignees:
            user_map[reminder.author_id] = reminder.author

    for user in user_map.values():
        ReminderParticipant.objects.create(
            reminder=reminder,
            user=user,
            role=role,
            added_by=reminder.author,
        )

    # Якщо змінили виконавців — оновлюємо отримувачів для майбутнього запланованого спрацювання
    if role == ReminderParticipant.Role.ASSIGNEE:
        scheduled_occ = reminder.occurrences.filter(
            status=ReminderOccurrence.Status.SCHEDULED
        ).first()
        if scheduled_occ:
            scheduled_occ.responses.all().delete()
            target_users = list(user_map.values()) or [reminder.author]
            ReminderResponse.objects.bulk_create(
                [
                    ReminderResponse(occurrence=scheduled_occ, assignee=u)
                    for u in target_users
                ]
            )
    return True


@transaction.atomic
def update_reminder_timeout(
    reminder_id: int, telegram_id: int, timeout_minutes: int
) -> bool:
    """Оновлює час очікування відповіді (у хвилинах)."""
    if (
        not isinstance(timeout_minutes, int)
        or isinstance(timeout_minutes, bool)
        or not 1 <= timeout_minutes <= 10080
    ):
        raise ValueError(
            "Тайм-аут має бути цілим числом від 1 до 10080 хвилин (7 днів)."
        )

    reminder = (
        Reminder.objects.select_for_update()
        .select_related("author")
        .filter(pk=reminder_id)
        .first()
    )
    if reminder is None or not _can_control(reminder, telegram_id):
        return False

    reminder.response_timeout_minutes = timeout_minutes
    reminder.save(update_fields=["response_timeout_minutes", "updated_at"])
    return True
