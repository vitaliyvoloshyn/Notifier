"""Бізнес-логіка відповідей виконавців і контролю строків."""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import ReminderParticipant, ReminderResponse, ReminderResponseEvent


class ResponseActionError(Exception):
    """Дію не можна виконати для цього стану відповіді або користувача."""


def apply_response_action(
    *,
    response_id: int,
    actor_telegram_id: int,
    action: str,
    defer_minutes: int | None = None,
    defer_until=None,
    telegram_message_id: int | None = None,
):
    """Застосовує дію лише від призначеного виконавця; зміна атомарна."""
    with transaction.atomic():
        response = (
            ReminderResponse.objects.select_for_update()
            .select_related("assignee", "occurrence__reminder")
            .get(pk=response_id)
        )
        if response.assignee.telegram_id != actor_telegram_id:
            raise ResponseActionError("Це нагадування призначене іншому користувачу.")
        if telegram_message_id and response.telegram_message_id != telegram_message_id:
            raise ResponseActionError(
                "Це повідомлення вже неактуальне. Скористайтеся останнім нагадуванням."
            )
        if response.delivery_status != ReminderResponse.DeliveryStatus.SENT:
            raise ResponseActionError(
                "Нагадування ще не доставлено або воно вже обробляється."
            )
        if response.deferred_until and response.deferred_until > timezone.now():
            raise ResponseActionError("Нагадування вже відтерміновано.")

        is_expired = response.status == ReminderResponse.Status.EXPIRED
        allowed_statuses = {
            ReminderResponse.Status.PENDING,
            ReminderResponse.Status.EXPIRED,
        }
        if response.status not in allowed_statuses:
            raise ResponseActionError(
                "На це нагадування вже надано остаточну відповідь."
            )

        now = timezone.now()
        if action in {"completed", "rejected"}:
            response.status = (
                ReminderResponse.Status.COMPLETED
                if action == "completed"
                else ReminderResponse.Status.REJECTED
            )
            response.responded_at = now
            response.deadline_at = None
            response.deferred_until = None
            response.save(
                update_fields=[
                    "status",
                    "responded_at",
                    "deadline_at",
                    "deferred_until",
                ]
            )
            event_type = (
                ReminderResponseEvent.EventType.COMPLETED
                if action == "completed"
                else ReminderResponseEvent.EventType.REJECTED
            )
            event = ReminderResponseEvent.objects.create(
                response=response,
                event_type=event_type,
                actor=response.assignee,
                details={"late": True} if is_expired else {},
            )
            return response, event

        if action == "defer":
            if is_expired:
                raise ResponseActionError(
                    "Час відповіді вже минув, відтермінування недоступне. Підтвердіть виконання або відхиліть."
                )
            if defer_until is not None:
                if timezone.is_naive(defer_until):
                    raise ResponseActionError(
                        "Час відтермінування має містити часовий пояс."
                    )
                deferred_until = defer_until
                seconds = (deferred_until - now).total_seconds()
                if seconds <= 0 or seconds > 7 * 24 * 60 * 60:
                    raise ResponseActionError(
                        "Час відтермінування має бути в межах наступних 7 днів."
                    )
                details = {"mode": "custom"}
            else:
                if (
                    not isinstance(defer_minutes, int)
                    or isinstance(defer_minutes, bool)
                    or not 1 <= defer_minutes <= 10080
                ):
                    raise ResponseActionError("Некоректний інтервал відтермінування.")
                deferred_until = now + timedelta(minutes=defer_minutes)
                details = {"minutes": defer_minutes}
            response.deferred_until = deferred_until
            response.deadline_at = None
            response.save(update_fields=["deferred_until", "deadline_at"])
            event = ReminderResponseEvent.objects.create(
                response=response,
                event_type=ReminderResponseEvent.EventType.DEFERRED,
                actor=response.assignee,
                deferred_until=deferred_until,
                details=details,
            )
            return response, event

        raise ResponseActionError("Невідома дія.")


def claim_due_deferred_responses(limit: int = 100) -> list[int]:
    """Повертає відтерміновані відповіді в чергу доставки після настання часу."""
    now = timezone.now()
    claimed = []
    with transaction.atomic():
        qs = (
            ReminderResponse.objects.select_for_update()
            .filter(
                status=ReminderResponse.Status.PENDING,
                delivery_status=ReminderResponse.DeliveryStatus.SENT,
                deferred_until__isnull=False,
                deferred_until__lte=now,
            )
            .order_by("deferred_until")[:limit]
        )
        for response in qs:
            response.delivery_status = ReminderResponse.DeliveryStatus.PENDING
            response.deferred_until = None
            response.deadline_at = None
            response.save(
                update_fields=["delivery_status", "deferred_until", "deadline_at"]
            )
            claimed.append(response.pk)
    return claimed


def expire_due_responses(limit: int = 100) -> list[dict]:
    """Позначає відповіді без реакції як прострочені та повертає дані для сповіщень."""
    now = timezone.now()
    expired = []
    with transaction.atomic():
        qs = (
            ReminderResponse.objects.select_for_update()
            .select_related("assignee", "occurrence__reminder")
            .filter(
                status=ReminderResponse.Status.PENDING,
                delivery_status=ReminderResponse.DeliveryStatus.SENT,
                deadline_at__isnull=False,
                deadline_at__lte=now,
                deferred_until__isnull=True,
            )
            .order_by("deadline_at")[:limit]
        )
        for response in qs:
            response.status = ReminderResponse.Status.EXPIRED
            response.responded_at = now
            response.deadline_at = None
            response.save(update_fields=["status", "responded_at", "deadline_at"])
            ReminderResponseEvent.objects.create(
                response=response,
                event_type=ReminderResponseEvent.EventType.EXPIRED,
                details={"reason": "response_timeout"},
            )
            expired.append(
                {
                    "response_id": response.pk,
                    "reminder_id": response.occurrence.reminder_id,
                    "reminder_title": response.occurrence.reminder.title,
                    "assignee_name": response.assignee.full_name
                    or response.assignee.username
                    or str(response.assignee.telegram_id),
                    "assignee_id": response.assignee.telegram_id,
                    "notification_policy": response.occurrence.reminder.notification_policy,
                }
            )
    return expired


def get_controller_telegram_ids(reminder_id: int) -> list[int]:
    """Автор і додані контролери конкретного нагадування."""
    from .models import Reminder

    # Перевіряємо, що нагадування існує (для збереження сумісності з Reminder.DoesNotExist)
    if not Reminder.objects.filter(pk=reminder_id).exists():
        raise Reminder.DoesNotExist

    controller_ids = ReminderParticipant.objects.filter(
        reminder_id=reminder_id,
        role=ReminderParticipant.Role.CONTROLLER,
    ).values_list("user__telegram_id", flat=True)

    return list(dict.fromkeys(controller_ids))
