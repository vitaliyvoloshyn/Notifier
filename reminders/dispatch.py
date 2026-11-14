"""Доставка запланованих спрацювань у Telegram."""

import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

from asgiref.sync import sync_to_async
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    Reminder,
    ReminderOccurrence,
    ReminderResponse,
    ReminderResponseEvent,
)
from .services import calculate_next_occurrence

logger = logging.getLogger(__name__)


def _claim_due_occurrences(limit: int = 50):
    """
Захоплення подій в роботу
Ця функція безпечно обирає порцію нагадувань, час яких уже настав, і «бронює» їх за поточним воркером.
• Атомарне блокування (transaction.atomic + select_for_update): База даних блокує вибрані рядки ReminderOccurrence до
завершення транзакції. Це захищає від стану гонитви (race condition), якщо ви запустите кілька процесів планувальника одночасно.
• Критерії відбору: Беруться лише активні нагадування (reminder__is_active=True), час яких вже настав (scheduled_at__lte=now),
у трьох можливих станах:
	1. SCHEDULED — нові заплановані події.
	2. FAILED — події, які не вдалося доставити в попередньому циклі.
	3. PROCESSING зі старим часом старту (processing_started_at < stale_before, тобто понад 10 хвилин тому) — захист
	від «зависання», якщо попередній процес воркера впав чи був примусово перезапущений посеред роботи.
• Зміна статусу: Для всіх знайдених записів встановлюється статус PROCESSING і фіксується час початку обробки. Функція
повертає список ID захоплених подій.
    """
    now = timezone.now()
    stale_before = now - timedelta(minutes=10)
    claimed = []
    with transaction.atomic():
        occurrences = (
            ReminderOccurrence.objects.select_for_update()
            .select_related("reminder")
            .filter(
                reminder__is_active=True,
                scheduled_at__lte=now,
            )
            .filter(
                Q(status=ReminderOccurrence.Status.SCHEDULED)
                | Q(status=ReminderOccurrence.Status.FAILED)
                | Q(status=ReminderOccurrence.Status.PROCESSING, processing_started_at__lt=stale_before)
            )
            .order_by("scheduled_at")[:limit]
        )
        for occurrence in occurrences:
            occurrence.status = ReminderOccurrence.Status.PROCESSING
            occurrence.processing_started_at = now
            occurrence.error_message = ""
            occurrence.save(update_fields=["status", "processing_started_at", "error_message"])
            claimed.append(occurrence.pk)
    return claimed


def _get_pending_responses(occurrence_id: int):
    """
    Отримання списку отримувачів
    Один запис ReminderOccurrence (сама подія в часі) може мати багато отримувачів (ReminderResponse), якщо нагадування групове.
    • Фільтрація: Функція дістає лише тих отримувачів (status=PENDING), яким повідомлення ще не відправлялося
    (delivery_status=PENDING) або попередня спроба надсилання закінчилася помилкою (delivery_status=FAILED). Тим,
    хто вже отримав повідомлення, дублікати не надсилаються.
    • Оптимізація запитів (select_related): Одразу підтягує дані користувача (assignee) та самого нагадування
    (occurrence__reminder) одним SQL-запитом, щоб уникнути помилок доступу до БД в асинхронному циклі пізніше.
    """
    return list(
        ReminderResponse.objects.select_related("assignee", "occurrence__reminder")
        .filter(occurrence_id=occurrence_id, status=ReminderResponse.Status.PENDING)
        .filter(
            Q(delivery_status=ReminderResponse.DeliveryStatus.PENDING)
            | Q(delivery_status=ReminderResponse.DeliveryStatus.FAILED)
        )
        .order_by("pk")
    )


def _record_delivery(response_id: int, *, message_id: int | None = None, error: str = ""):
    """
    Фіксація результату відправки та аудит
    Оновлює стан доставки для конкретного користувача після спроби відправити повідомлення в Telegram.
    • Сценарій помилки (if error:):
        • Переводить delivery_status у FAILED і зберігає текст помилки (обрізаний до 4000 символів).
        • Записує подію DELIVERY_FAILED в історію (ReminderResponseEvent) для аудиту.
    • Сценарій успіху:
        • Переводить delivery_status у SENT і очищає поле помилок.
        • Зберігає telegram_message_id (це знадобиться в майбутньому, якщо бот захоче відредагувати або видалити це повідомлення).
        • Розраховує deadline_at — крайній термін, до якого користувач має відреагувати на нагадування (натиснути «Виконано» тощо).
        • Створює запис NOTIFIED у журналі подій ReminderResponseEvent.
    """
    now = timezone.now()
    with transaction.atomic():
        response = ReminderResponse.objects.select_for_update().get(pk=response_id)
        if error:
            response.delivery_status = ReminderResponse.DeliveryStatus.FAILED
            response.delivery_error = error[:4000]
            response.save(update_fields=["delivery_status", "delivery_error"])
            ReminderResponseEvent.objects.create(
                response=response,
                event_type=ReminderResponseEvent.EventType.DELIVERY_FAILED,
                details={"error": error[:1000]},
            )
            return

        response.delivery_status = ReminderResponse.DeliveryStatus.SENT
        response.last_sent_at = now
        response.deadline_at = now + timedelta(minutes=response.occurrence.reminder.response_timeout_minutes)
        response.deferred_until = None
        response.telegram_message_id = message_id
        response.delivery_error = ""
        response.save(update_fields=[
            "delivery_status", "last_sent_at", "deadline_at", "deferred_until",
            "telegram_message_id", "delivery_error",
        ])
        ReminderResponseEvent.objects.create(
            response=response,
            event_type=ReminderResponseEvent.EventType.NOTIFIED,
            details={"telegram_message_id": message_id} if message_id else {},
        )


def _finish_occurrence(occurrence_id: int):
    """
    Завершення циклу та планування наступного разу
    Викликається після того, як воркер пройшовся по всіх отримувачах конкретної події.
    • Перевірка повноти доставки: Функція перевіряє, чи не залишилося отримувачів зі статусом FAILED або PENDING. Якщо
    хоча б одному користувачу повідомлення не дійшло, вся подія occurrence переводиться у статус FAILED (щоб через
    15 секунд воркер знову її підхопив і спробував доставити тим, хто не отримав).
    • Закриття поточної події: Якщо всі успішно отримали нагадування, подія отримує статус COMPLETED і час завершення completed_at.
    • Генерація наступного спрацювання (для повторюваних нагадувань):
        • Якщо нагадування все ще активне (reminder.is_active), викликається calculate_next_occurrence(...).
        • Якщо нагадування одноразове, next_at поверне None.
        • Якщо воно періодичне (наприклад, щодня о 9:00), створюється новий запис ReminderOccurrence на майбутню дату,
        а через bulk_create одразу генеруються записи ReminderResponse для всіх призначених виконавців (або для автора
        нагадування, якщо виконавців не вказано).
    """
    with transaction.atomic():
        occurrence = (
            ReminderOccurrence.objects.select_for_update()
            .select_related("reminder")
            .get(pk=occurrence_id)
        )
        reminder = Reminder.objects.select_for_update().get(pk=occurrence.reminder_id)
        if occurrence.status != ReminderOccurrence.Status.PROCESSING:
            return

        failed_exists = occurrence.responses.filter(
            status=ReminderResponse.Status.PENDING,
            delivery_status=ReminderResponse.DeliveryStatus.FAILED,
        ).exists()
        pending_delivery_exists = occurrence.responses.filter(
            status=ReminderResponse.Status.PENDING,
            delivery_status=ReminderResponse.DeliveryStatus.PENDING,
        ).exists()
        if failed_exists or pending_delivery_exists:
            occurrence.status = ReminderOccurrence.Status.FAILED
            occurrence.error_message = "Не всім адресатам вдалося доставити повідомлення."
            occurrence.save(update_fields=["status", "error_message"])
            return

        occurrence.status = ReminderOccurrence.Status.COMPLETED
        occurrence.completed_at = timezone.now()
        occurrence.save(update_fields=["status", "completed_at"])

        if reminder.is_active:
            next_at = calculate_next_occurrence(reminder, occurrence.scheduled_at)
            reminder.next_reminder_at = next_at
            reminder.save(update_fields=["next_reminder_at", "updated_at"])
            if next_at is not None:
                next_occurrence = ReminderOccurrence.objects.create(
                    reminder=reminder,
                    scheduled_at=next_at,
                )
                assignee_ids = list(
                    reminder.participants.filter(role="assignee").values_list("user_id", flat=True)
                )
                if not assignee_ids:
                    assignee_ids = [reminder.author_id]
                ReminderResponse.objects.bulk_create([
                    ReminderResponse(occurrence=next_occurrence, assignee_id=user_id)
                    for user_id in assignee_ids
                ])


async def dispatch_due_occurrences(bot, *, limit: int = 50) -> dict[str, int]:
    """
    Головний асинхронний оркестратор
    Зв'язує всі попередні синхронні функції з асинхронним клієнтом aiogram:
    1. Захоплює пачку подій через _claim_due_occurrences.
    2. Для кожної події отримує список отримувачів через _get_pending_responses.
    3. Формує текст повідомлення з урахуванням індивідуального часового поясу нагадування (ZoneInfo(reminder.timezone)).
    4. Намагається відправити повідомлення через await bot.send_message(...) та фіксує результат (_record_delivery),
    збираючи статистику (sent / failed).
    5. Наприкінці кожної події викликає _finish_occurrence, щоб закрити її та запланувати наступну.
    """
    occurrence_ids = await sync_to_async(_claim_due_occurrences)(limit)
    stats = {"occurrences": len(occurrence_ids), "sent": 0, "failed": 0}

    for occurrence_id in occurrence_ids:
        responses = await sync_to_async(_get_pending_responses)(occurrence_id)
        for response in responses:
            reminder = response.occurrence.reminder
            text = (
                f"⏰ Нагадування: {reminder.title}\n"
                f"Заплановано: {timezone.localtime(response.occurrence.scheduled_at, ZoneInfo(reminder.timezone)).strftime('%d.%m.%Y %H:%M')}"
            )
            try:
                message = await bot.send_message(
                    chat_id=response.assignee.telegram_id,
                    text=text,
                )
                await sync_to_async(_record_delivery)(response.pk, message_id=message.message_id)
                stats["sent"] += 1
            except Exception as exc:  # Telegram/network errors are recorded and retried next cycle.
                logger.exception("Не вдалося надіслати ReminderResponse id=%s", response.pk)
                await sync_to_async(_record_delivery)(response.pk, error=str(exc))
                stats["failed"] += 1

        await sync_to_async(_finish_occurrence)(occurrence_id)

    return stats
