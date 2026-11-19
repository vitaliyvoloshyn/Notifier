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
