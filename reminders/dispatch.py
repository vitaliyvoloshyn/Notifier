"""Доставка запланованих спрацювань у Telegram."""

import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

from asgiref.sync import sync_to_async
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from bot.keyboards import response_keyboard

from .models import (
    Reminder,
    ReminderOccurrence,
    ReminderResponse,
    ReminderResponseEvent,
)
from .responses import (
    claim_due_deferred_responses,
    claim_due_nag_responses,
    close_expired_occurrences,
    expire_due_responses,
    get_controller_telegram_ids,
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
                | Q(
                    status=ReminderOccurrence.Status.PROCESSING,
                    processing_started_at__lt=stale_before,
                )
            )
            .order_by("scheduled_at")[:limit]
        )
        for occurrence in occurrences:
            occurrence.status = ReminderOccurrence.Status.PROCESSING
            occurrence.processing_started_at = now
            occurrence.error_message = ""
            occurrence.save(
                update_fields=["status", "processing_started_at", "error_message"]
            )
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


def _record_delivery(
    response_id: int, *, message_id: int | None = None, error: str = ""
):
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
        # Дедлайн ставимо лише якщо відповідь ще в статусі PENDING і дедлайн ще не встановлений
        if (
            response.status == ReminderResponse.Status.PENDING
            and response.deadline_at is None
        ):
            response.deadline_at = now + timedelta(
                minutes=response.occurrence.reminder.response_timeout_minutes
            )
        response.deferred_until = None
        response.telegram_message_id = message_id
        response.delivery_error = ""

        update_fields = [
            "delivery_status",
            "last_sent_at",
            "deadline_at",
            "deferred_until",
            "telegram_message_id",
            "delivery_error",
        ]

        # Якщо увімкнено настирливі нагадування і next_nag_at ще не заплановано — стартуємо таймер
        nag_interval = response.occurrence.reminder.nag_interval_minutes
        if nag_interval and response.next_nag_at is None:
            response.next_nag_at = now + timedelta(minutes=nag_interval)
            update_fields.append("next_nag_at")

        response.save(update_fields=update_fields)
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
            occurrence.error_message = (
                "Не всім адресатам вдалося доставити повідомлення."
            )
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
                expires_at = (
                    next_at + timedelta(minutes=reminder.occurrence_ttl_minutes)
                    if reminder.occurrence_ttl_minutes
                    else None
                )
                next_occurrence = ReminderOccurrence.objects.create(
                    reminder=reminder,
                    scheduled_at=next_at,
                    expires_at=expires_at,
                )
                assignee_ids = list(
                    reminder.participants.filter(role="assignee").values_list(
                        "user_id", flat=True
                    )
                )
                if not assignee_ids:
                    assignee_ids = [reminder.author_id]
                ReminderResponse.objects.bulk_create(
                    [
                        ReminderResponse(
                            occurrence=next_occurrence, assignee_id=user_id
                        )
                        for user_id in assignee_ids
                    ]
                )


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
                f"<b>{reminder.title}</b>\n⏰ Нагадування:\n"
                f"Заплановано: {timezone.localtime(response.occurrence.scheduled_at, ZoneInfo(reminder.timezone)).strftime('%d.%m.%Y %H:%M')}"
            )
            try:
                message = await bot.send_message(
                    chat_id=response.assignee.telegram_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=response_keyboard(response.pk),
                )
                await sync_to_async(_record_delivery)(
                    response.pk, message_id=message.message_id
                )
                stats["sent"] += 1
            except (
                Exception
            ) as exc:  # Telegram/network errors are recorded and retried next cycle.
                logger.exception(
                    "Не вдалося надіслати ReminderResponse id=%s", response.pk
                )
                await sync_to_async(_record_delivery)(response.pk, error=str(exc))
                stats["failed"] += 1

        await sync_to_async(_finish_occurrence)(occurrence_id)

    return stats


def _get_responses_by_ids(response_ids: list[int], *, allow_expired: bool = False):
    statuses = [ReminderResponse.Status.PENDING]
    if allow_expired:
        statuses.append(ReminderResponse.Status.EXPIRED)
    return list(
        ReminderResponse.objects.select_related("assignee", "occurrence__reminder")
        .filter(pk__in=response_ids, status__in=statuses)
        .order_by("pk")
    )


async def process_response_timeouts(bot, *, limit: int = 100) -> dict[str, int]:
    """Повторно доставляє відтерміновані відповіді та закриває прострочені."""
    auto_closed = await sync_to_async(close_expired_occurrences)(limit)
    for item in auto_closed:
        await _clear_old_reply_markup(bot, item["chat_id"], item["message_id"])

    deferred_ids = await sync_to_async(claim_due_deferred_responses)(limit)
    deferred_responses = await sync_to_async(_get_responses_by_ids)(deferred_ids)
    deferred_sent = 0
    deferred_failed = 0
    for response in deferred_responses:
        reminder = response.occurrence.reminder
        local_time = timezone.localtime(
            response.occurrence.scheduled_at, ZoneInfo(reminder.timezone)
        )
        text = (
            f"<b>{reminder.title}</b>\n⏰ Нагадування (повторно):\n"
            f"Початковий час: {local_time.strftime('%d.%m.%Y %H:%M')}"
        )
        old_message_id = response.telegram_message_id
        try:
            message = await bot.send_message(
                chat_id=response.assignee.telegram_id,
                text=text,
                parse_mode="HTML",
                reply_markup=response_keyboard(response.pk),
            )
            await _clear_old_reply_markup(
                bot, response.assignee.telegram_id, old_message_id
            )  # <--- ПРИБИРАЄМО СТАРІ КНОПКИ
            await sync_to_async(_record_delivery)(
                response.pk, message_id=message.message_id
            )
            deferred_sent += 1
        except Exception as exc:
            logger.exception(
                "Не вдалося повторно надіслати ReminderResponse id=%s", response.pk
            )
            await sync_to_async(_record_delivery)(response.pk, error=str(exc))
            deferred_failed += 1

    expired = await sync_to_async(expire_due_responses)(limit)
    notified = 0
    for item in expired:
        if (
            item["notification_policy"] != Reminder.NotificationPolicy.EXCEPTIONS_ONLY
            and item["notification_policy"] != Reminder.NotificationPolicy.ALL_ACTIONS
        ):
            continue
        try:
            controller_ids = await sync_to_async(get_controller_telegram_ids)(
                item["reminder_id"]
            )
        except Reminder.DoesNotExist:
            continue
        text = f"⌛ Немає відповіді на нагадування «{item['reminder_title']}» від {item['assignee_name']}."
        for telegram_id in controller_ids:
            try:
                await bot.send_message(telegram_id, text)
                notified += 1
            except Exception:
                logger.exception(
                    "Не вдалося повідомити контролера %s про таймаут", telegram_id
                )
    nag_ids = await sync_to_async(claim_due_nag_responses)(limit)
    nag_responses = await sync_to_async(_get_responses_by_ids)(
        nag_ids, allow_expired=True
    )
    nag_sent = 0
    nag_failed = 0
    for response in nag_responses:
        reminder = response.occurrence.reminder
        local_time = timezone.localtime(
            response.occurrence.scheduled_at, ZoneInfo(reminder.timezone)
        )
        text = (
            f"<b>{reminder.title}</b>\n🔔 Настирливе нагадування:\n"
            f"Початковий час: {local_time.strftime('%d.%m.%Y %H:%M')}"
            f"<i>(Повторюється кожні {reminder.nag_interval_minutes} хв до закриття)</i>"
        )
        old_message_id = response.telegram_message_id  # <--- ЗБЕРІГАЄМО СТАРИЙ ID
        try:
            message = await bot.send_message(
                chat_id=response.assignee.telegram_id,
                text=text,
                parse_mode="HTML",
                reply_markup=response_keyboard(response.pk),
            )
            await _clear_old_reply_markup(
                bot, response.assignee.telegram_id, old_message_id
            )  # <--- ПРИБИРАЄМО СТАРІ КНОПКИ
            # Оновлюємо telegram_message_id, щоб кнопки працювали у новому повідомленні, статус не чіпаємо
            await sync_to_async(_record_nag_delivery)(
                response.pk, message_id=message.message_id
            )
            nag_sent += 1
        except Exception:
            logger.exception(
                "Не вдалося надіслати настирливий дубль ReminderResponse id=%s",
                response.pk,
            )
            nag_failed += 1

    return {
        "deferred": len(deferred_ids),
        "deferred_sent": deferred_sent,
        "deferred_failed": deferred_failed,
        "expired": len(expired),
        "notified": notified,
        "nag_sent": nag_sent,
        "nag_failed": nag_failed,
        "auto_closed": len(auto_closed),
    }


def _record_nag_delivery(response_id: int, *, message_id: int):
    with transaction.atomic():
        response = ReminderResponse.objects.select_for_update().get(pk=response_id)
        response.telegram_message_id = message_id
        response.last_sent_at = timezone.now()
        response.save(update_fields=["telegram_message_id", "last_sent_at"])
        ReminderResponseEvent.objects.create(
            response=response,
            event_type=ReminderResponseEvent.EventType.NAGGED,
            details={"telegram_message_id": message_id},
        )


async def _clear_old_reply_markup(bot, chat_id: int, message_id: int | None):
    """Прибирає інлайн-кнопки з попереднього повідомлення нагадування."""
    if not message_id:
        return
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=None,
        )
    except Exception:  # noqa: BLE001
        # Повідомлення могло бути видалене користувачем або старше 48 годин
        logger.debug(
            "Не вдалося прибрати кнопки з повідомлення %s у чаті %s",
            message_id,
            chat_id,
        )
