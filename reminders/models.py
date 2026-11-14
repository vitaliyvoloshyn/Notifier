from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q


class Reminder(models.Model):
    """Правило нагадування. Усі моменти часу зберігаються Django в UTC."""

    class RecurrenceType(models.TextChoices):
        NONE = "none", "Одноразове"
        DAILY = "daily", "Щодня"
        WEEKLY = "weekly", "Щотижня"
        MONTHLY = "monthly", "Щомісяця"
        YEARLY = "yearly", "Щороку"

    class NotificationPolicy(models.TextChoices):
        ALL_ACTIONS = "all_actions", "Повідомляти про кожну дію"
        EXCEPTIONS_ONLY = "exceptions_only", "Тільки про проблеми"

    title = models.CharField("Назва", max_length=255)
    created_at = models.DateTimeField("Дата створення", auto_now_add=True)
    updated_at = models.DateTimeField("Дата оновлення", auto_now=True)

    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="authored_reminders",
        verbose_name="Автор",
    )

    # Це конкретний момент першого спрацювання. Часовий пояс нижче зберігає
    # локальний контекст, у якому користувач задав цей момент.
    first_reminder_at = models.DateTimeField("Перше нагадування")
    next_reminder_at = models.DateTimeField(
        "Наступне спрацювання",
        null=True,
        blank=True,
        db_index=True,
        help_text="Наступний момент спрацювання в UTC; для одноразового після відправлення — порожній.",
    )
    timezone = models.CharField(
        "Часовий пояс розкладу",
        max_length=64,
        default="Europe/Kyiv",
        help_text="IANA timezone автора на момент створення, наприклад Europe/Kyiv.",
    )

    recurrence_type = models.CharField(
        "Циклічність",
        max_length=10,
        choices=RecurrenceType.choices,
        default=RecurrenceType.NONE,
    )
    recurrence_interval = models.PositiveIntegerField(
        "Інтервал повторення",
        default=1,
        validators=[MinValueValidator(1)],
        help_text="1 — кожен період, 2 — через один період тощо.",
    )

    notification_policy = models.CharField(
        "Інформування контролерів",
        max_length=20,
        choices=NotificationPolicy.choices,
        default=NotificationPolicy.EXCEPTIONS_ONLY,
    )
    response_timeout_minutes = models.PositiveIntegerField(
        "Час очікування відповіді (хвилини)",
        default=60,
        validators=[MinValueValidator(1)],
    )

    is_active = models.BooleanField("Активне", default=True, db_index=True)

    class Meta:
        verbose_name = "Нагадування"
        verbose_name_plural = "Нагадування"
        ordering = ("next_reminder_at", "-created_at")
        constraints = [
            models.CheckConstraint(
                condition=Q(recurrence_interval__gte=1),
                name="reminder_recurrence_interval_gte_1",
            ),
            models.CheckConstraint(
                condition=Q(response_timeout_minutes__gte=1),
                name="reminder_response_timeout_gte_1",
            ),
        ]

    def __str__(self):
        return self.title


class ReminderParticipant(models.Model):
    """Контролер або підлеглий у межах одного конкретного нагадування.

    Автор Reminder автоматично має права контролера і окремим рядком тут
    не дублюється. Один користувач за потреби може мати обидві ролі.
    """

    class Role(models.TextChoices):
        CONTROLLER = "controller", "Контролер"
        ASSIGNEE = "assignee", "Підлеглий"

    reminder = models.ForeignKey(
        Reminder,
        on_delete=models.CASCADE,
        related_name="participants",
        verbose_name="Нагадування",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="reminder_participations",
        verbose_name="Користувач",
    )
    role = models.CharField("Роль", max_length=20, choices=Role.choices)
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="added_reminder_participants",
        verbose_name="Додав",
    )
    added_at = models.DateTimeField("Дата додавання", auto_now_add=True)

    class Meta:
        verbose_name = "Учасник нагадування"
        verbose_name_plural = "Учасники нагадування"
        ordering = ("role", "added_at")
        constraints = [
            models.UniqueConstraint(
                fields=("reminder", "user", "role"),
                name="unique_reminder_user_role",
            ),
        ]
        indexes = [models.Index(fields=("user", "role"), name="rem_part_user_role_idx")]

    def __str__(self):
        return f"{self.user} — {self.get_role_display()}: {self.reminder}"


class ReminderOccurrence(models.Model):
    """Одне конкретне заплановане спрацювання правила Reminder."""

    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Заплановано"
        PROCESSING = "processing", "Обробляється"
        COMPLETED = "completed", "Завершено"
        FAILED = "failed", "Помилка"
        CANCELLED = "cancelled", "Скасовано"

    reminder = models.ForeignKey(
        Reminder,
        on_delete=models.CASCADE,
        related_name="occurrences",
        verbose_name="Нагадування",
    )
    scheduled_at = models.DateTimeField("Запланований час", db_index=True)
    status = models.CharField(
        "Статус", max_length=12, choices=Status.choices, default=Status.SCHEDULED
    )
    created_at = models.DateTimeField("Створено", auto_now_add=True)
    processing_started_at = models.DateTimeField("Початок обробки", null=True, blank=True)
    completed_at = models.DateTimeField("Завершено", null=True, blank=True)
    error_message = models.TextField("Помилка", blank=True)

    class Meta:
        verbose_name = "Спрацювання нагадування"
        verbose_name_plural = "Спрацювання нагадувань"
        ordering = ("-scheduled_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("reminder", "scheduled_at"),
                name="unique_reminder_occurrence_time",
            ),
        ]
        indexes = [models.Index(fields=("status", "scheduled_at"), name="rem_occ_status_sched_idx")]

    def __str__(self):
        return f"{self.reminder} — {self.scheduled_at:%Y-%m-%d %H:%M}"


class ReminderResponse(models.Model):
    """Персональний стан одного підлеглого для одного спрацювання.

    Якщо в нагадуванні немає підлеглих, сервіс створення спрацювання може
    створити відповідь для автора, щоб особисті нагадування також працювали.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Очікує відповіді"
        COMPLETED = "completed", "Виконано"
        REJECTED = "rejected", "Відхилено"
        EXPIRED = "expired", "Час відповіді минув"

    class DeliveryStatus(models.TextChoices):
        PENDING = "pending", "Очікує відправлення"
        SENT = "sent", "Відправлено"
        FAILED = "failed", "Помилка відправлення"

    occurrence = models.ForeignKey(
        ReminderOccurrence,
        on_delete=models.CASCADE,
        related_name="responses",
        verbose_name="Спрацювання",
    )
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="reminder_responses",
        verbose_name="Підлеглий",
    )
    status = models.CharField(
        "Результат", max_length=12, choices=Status.choices, default=Status.PENDING
    )
    delivery_status = models.CharField(
        "Статус доставки",
        max_length=10,
        choices=DeliveryStatus.choices,
        default=DeliveryStatus.PENDING,
    )
    created_at = models.DateTimeField("Створено", auto_now_add=True)
    last_sent_at = models.DateTimeField("Остання успішна відправка", null=True, blank=True)
    deadline_at = models.DateTimeField("Крайній час відповіді", null=True, blank=True, db_index=True)
    deferred_until = models.DateTimeField("Відтерміновано до", null=True, blank=True, db_index=True)
    responded_at = models.DateTimeField("Час остаточної відповіді", null=True, blank=True)
    telegram_message_id = models.BigIntegerField("ID повідомлення Telegram", null=True, blank=True)
    delivery_error = models.TextField("Помилка доставки", blank=True)

    class Meta:
        verbose_name = "Відповідь на нагадування"
        verbose_name_plural = "Відповіді на нагадування"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("occurrence", "assignee"),
                name="unique_occurrence_assignee_response",
            ),
        ]
        indexes = [
            models.Index(fields=("status", "deadline_at"), name="rem_resp_status_deadline_idx"),
            models.Index(fields=("delivery_status", "deferred_until"), name="rem_resp_delivery_defer_idx"),
        ]

    def __str__(self):
        return f"{self.assignee} — {self.occurrence} — {self.get_status_display()}"


class ReminderResponseEvent(models.Model):
    """Журнал дій/подій для персональної відповіді."""

    class EventType(models.TextChoices):
        NOTIFIED = "notified", "Нагадування надіслано"
        COMPLETED = "completed", "Виконано"
        REJECTED = "rejected", "Відхилено"
        DEFERRED = "deferred", "Відтерміновано"
        EXPIRED = "expired", "Час відповіді минув"
        DELIVERY_FAILED = "delivery_failed", "Помилка доставки"

    response = models.ForeignKey(
        ReminderResponse,
        on_delete=models.CASCADE,
        related_name="events",
        verbose_name="Відповідь",
    )
    event_type = models.CharField("Подія", max_length=20, choices=EventType.choices)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reminder_response_events",
        verbose_name="Хто виконав дію",
        help_text="Порожньо для автоматичних системних подій.",
    )
    occurred_at = models.DateTimeField("Час події", auto_now_add=True, db_index=True)
    deferred_until = models.DateTimeField("Відтерміновано до", null=True, blank=True)
    details = models.JSONField("Додаткові дані", default=dict, blank=True)

    class Meta:
        verbose_name = "Подія відповіді"
        verbose_name_plural = "Історія відповідей"
        ordering = ("occurred_at", "pk")
        indexes = [models.Index(fields=("event_type", "occurred_at"), name="rem_evt_type_time_idx")]

    def __str__(self):
        return f"{self.get_event_type_display()} — {self.response}"
