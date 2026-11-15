from datetime import datetime, timedelta

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from users.models import TelegramUser
from .models import (
    Reminder,
    ReminderOccurrence,
    ReminderParticipant,
    ReminderResponse,
    ReminderResponseEvent,
)


class ReminderModelTests(TestCase):
    def setUp(self):
        self.author = TelegramUser.objects.create_user(telegram_id=1001, username="author")
        self.assignee = TelegramUser.objects.create_user(telegram_id=1002, username="assignee")
        self.reminder = Reminder.objects.create(
            title="Перевірити завдання",
            author=self.author,
            first_reminder_at=timezone.now() + timedelta(hours=1),
            next_reminder_at=timezone.now() + timedelta(hours=1),
            timezone="Europe/Kyiv",
        )

    def test_participant_can_have_both_roles(self):
        ReminderParticipant.objects.create(
            reminder=self.reminder,
            user=self.assignee,
            role=ReminderParticipant.Role.ASSIGNEE,
            added_by=self.author,
        )
        ReminderParticipant.objects.create(
            reminder=self.reminder,
            user=self.assignee,
            role=ReminderParticipant.Role.CONTROLLER,
            added_by=self.author,
        )
        self.assertEqual(self.reminder.participants.filter(user=self.assignee).count(), 2)

    def test_same_role_cannot_be_added_twice(self):
        ReminderParticipant.objects.create(
            reminder=self.reminder,
            user=self.assignee,
            role=ReminderParticipant.Role.ASSIGNEE,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            ReminderParticipant.objects.create(
                reminder=self.reminder,
                user=self.assignee,
                role=ReminderParticipant.Role.ASSIGNEE,
            )

    def test_each_assignee_has_independent_response_and_event_history(self):
        occurrence = ReminderOccurrence.objects.create(
            reminder=self.reminder,
            scheduled_at=self.reminder.first_reminder_at,
        )
        response = ReminderResponse.objects.create(
            occurrence=occurrence,
            assignee=self.assignee,
        )
        event = ReminderResponseEvent.objects.create(
            response=response,
            event_type=ReminderResponseEvent.EventType.DEFERRED,
            actor=self.assignee,
            deferred_until=timezone.now() + timedelta(minutes=30),
            details={"minutes": 30},
        )
        self.assertEqual(response.status, ReminderResponse.Status.PENDING)
        self.assertEqual(response.events.get(), event)


class ReminderServiceTests(TestCase):
    def setUp(self):
        self.author = TelegramUser.objects.create_user(telegram_id=2001, username="service_author")
        self.assignee = TelegramUser.objects.create_user(telegram_id=2002, username="service_assignee")
        self.controller = TelegramUser.objects.create_user(telegram_id=2003, username="service_controller")

    def test_create_reminder_creates_occurrence_and_individual_responses(self):
        from .services import create_reminder

        reminder = create_reminder(
            author=self.author,
            title="  Надіслати звіт  ",
            first_reminder_at=datetime(2027, 1, 31, 9, 0),
            recurrence_type=Reminder.RecurrenceType.MONTHLY,
            assignees=[self.assignee],
            controllers=[self.controller, self.author],
        )

        self.assertEqual(reminder.title, "Надіслати звіт")
        self.assertEqual(reminder.timezone, "Europe/Kyiv")
        self.assertEqual(reminder.participants.filter(role=ReminderParticipant.Role.ASSIGNEE).count(), 1)
        self.assertEqual(reminder.participants.filter(role=ReminderParticipant.Role.CONTROLLER).count(), 1)
        occurrence = reminder.occurrences.get()
        self.assertEqual(occurrence.responses.count(), 1)
        self.assertEqual(occurrence.responses.get().assignee, self.assignee)

    def test_personal_reminder_creates_response_for_author(self):
        from .services import create_reminder

        reminder = create_reminder(
            author=self.author,
            title="Особисте",
            first_reminder_at=datetime(2027, 1, 31, 9, 0),
        )
        self.assertEqual(reminder.occurrences.get().responses.get().assignee, self.author)

    def test_monthly_recurrence_keeps_original_day_after_short_month(self):
        from zoneinfo import ZoneInfo
        from .services import calculate_next_occurrence

        first = datetime(2027, 1, 31, 9, 0, tzinfo=ZoneInfo("Europe/Kyiv"))
        reminder = Reminder(
            first_reminder_at=first,
            timezone="Europe/Kyiv",
            recurrence_type=Reminder.RecurrenceType.MONTHLY,
            recurrence_interval=1,
        )
        february = calculate_next_occurrence(reminder, first)
        march = calculate_next_occurrence(reminder, february)
        self.assertEqual(february.astimezone(ZoneInfo("Europe/Kyiv")).date(), datetime(2027, 2, 28).date())
        self.assertEqual(march.astimezone(ZoneInfo("Europe/Kyiv")).date(), datetime(2027, 3, 31).date())

    def test_yearly_recurrence_restores_february_29_in_leap_year(self):
        from zoneinfo import ZoneInfo
        from .services import calculate_next_occurrence

        first = datetime(2024, 2, 29, 10, 0, tzinfo=ZoneInfo("Europe/Kyiv"))
        reminder = Reminder(
            first_reminder_at=first,
            timezone="Europe/Kyiv",
            recurrence_type=Reminder.RecurrenceType.YEARLY,
            recurrence_interval=1,
        )
        non_leap_2025 = calculate_next_occurrence(reminder, first)
        non_leap_2026 = calculate_next_occurrence(reminder, non_leap_2025)
        non_leap_2027 = calculate_next_occurrence(reminder, non_leap_2026)
        leap_2028 = calculate_next_occurrence(reminder, non_leap_2027)
        self.assertEqual(non_leap_2025.astimezone(ZoneInfo("Europe/Kyiv")).date(), datetime(2025, 2, 28).date())
        self.assertEqual(non_leap_2026.astimezone(ZoneInfo("Europe/Kyiv")).date(), datetime(2026, 2, 28).date())
        self.assertEqual(non_leap_2027.astimezone(ZoneInfo("Europe/Kyiv")).date(), datetime(2027, 2, 28).date())
        self.assertEqual(leap_2028.astimezone(ZoneInfo("Europe/Kyiv")).date(), datetime(2028, 2, 29).date())

    def test_nonexistent_initial_local_time_is_rejected(self):
        from django.core.exceptions import ValidationError
        from .services import normalize_first_datetime

        with self.assertRaises(ValidationError):
            normalize_first_datetime(datetime(2027, 3, 28, 3, 30), "Europe/Kyiv")

    def test_weekly_recurrence_keeps_local_clock_across_dst(self):
        from zoneinfo import ZoneInfo
        from .services import calculate_next_occurrence

        first = datetime(2027, 3, 21, 3, 30, tzinfo=ZoneInfo("Europe/Kyiv"))
        reminder = Reminder(
            first_reminder_at=first,
            timezone="Europe/Kyiv",
            recurrence_type=Reminder.RecurrenceType.WEEKLY,
            recurrence_interval=1,
        )
        next_at = calculate_next_occurrence(reminder, first)
        local_next = next_at.astimezone(ZoneInfo("Europe/Kyiv"))
        # 03:30 не існує в день весняного переходу, тому алгоритм
        # переносить повторення на першу доступну хвилину — 04:00.
        self.assertEqual(local_next.hour, 4)
        self.assertEqual(local_next.minute, 0)


class ReminderDispatchTests(TestCase):
    def setUp(self):
        self.author = TelegramUser.objects.create_user(telegram_id=3001, username="dispatch_author")
        self.assignee = TelegramUser.objects.create_user(telegram_id=3002, username="dispatch_assignee")
        self.reminder = Reminder.objects.create(
            title="Повторне нагадування",
            author=self.author,
            first_reminder_at=timezone.now() - timedelta(minutes=1),
            next_reminder_at=timezone.now() - timedelta(minutes=1),
            timezone="Europe/Kyiv",
            recurrence_type=Reminder.RecurrenceType.DAILY,
        )
        ReminderParticipant.objects.create(
            reminder=self.reminder,
            user=self.assignee,
            role=ReminderParticipant.Role.ASSIGNEE,
            added_by=self.author,
        )
        self.occurrence = ReminderOccurrence.objects.create(
            reminder=self.reminder,
            scheduled_at=self.reminder.first_reminder_at,
            status=ReminderOccurrence.Status.PROCESSING,
        )
        self.response = ReminderResponse.objects.create(
            occurrence=self.occurrence,
            assignee=self.assignee,
        )

    def test_successful_delivery_sets_deadline_and_event(self):
        from .dispatch import _record_delivery

        _record_delivery(self.response.pk, message_id=987)
        self.response.refresh_from_db()
        self.assertEqual(self.response.delivery_status, ReminderResponse.DeliveryStatus.SENT)
        self.assertEqual(self.response.telegram_message_id, 987)
        self.assertIsNotNone(self.response.deadline_at)
        self.assertEqual(
            self.response.events.get().event_type,
            ReminderResponseEvent.EventType.NOTIFIED,
        )

    def test_finished_recurring_occurrence_creates_next_one(self):
        from .dispatch import _record_delivery, _finish_occurrence

        _record_delivery(self.response.pk, message_id=988)
        _finish_occurrence(self.occurrence.pk)
        self.occurrence.refresh_from_db()
        self.reminder.refresh_from_db()
        self.assertEqual(self.occurrence.status, ReminderOccurrence.Status.COMPLETED)
        self.assertEqual(self.reminder.occurrences.count(), 2)
        next_occurrence = self.reminder.occurrences.exclude(pk=self.occurrence.pk).get()
        self.assertEqual(next_occurrence.responses.count(), 1)
        self.assertEqual(next_occurrence.responses.get().assignee, self.assignee)

    def test_failed_delivery_keeps_occurrence_retryable(self):
        from .dispatch import _record_delivery, _finish_occurrence

        _record_delivery(self.response.pk, error="temporary Telegram error")
        _finish_occurrence(self.occurrence.pk)
        self.occurrence.refresh_from_db()
        self.assertEqual(self.occurrence.status, ReminderOccurrence.Status.FAILED)
        self.assertEqual(
            self.response.events.get().event_type,
            ReminderResponseEvent.EventType.DELIVERY_FAILED,
        )


class ReminderResponseActionTests(TestCase):
    def setUp(self):
        self.author = TelegramUser.objects.create_user(telegram_id=4101, username="action_author")
        self.assignee = TelegramUser.objects.create_user(telegram_id=4102, username="action_assignee")
        self.other_user = TelegramUser.objects.create_user(telegram_id=4103, username="other_user")
        self.reminder = Reminder.objects.create(
            title="Перевірити відповідь",
            author=self.author,
            first_reminder_at=timezone.now() - timedelta(minutes=5),
            timezone="Europe/Kyiv",
            notification_policy=Reminder.NotificationPolicy.EXCEPTIONS_ONLY,
        )
        self.occurrence = ReminderOccurrence.objects.create(
            reminder=self.reminder,
            scheduled_at=self.reminder.first_reminder_at,
            status=ReminderOccurrence.Status.COMPLETED,
        )
        self.response = ReminderResponse.objects.create(
            occurrence=self.occurrence,
            assignee=self.assignee,
            delivery_status=ReminderResponse.DeliveryStatus.SENT,
            telegram_message_id=777,
            last_sent_at=timezone.now(),
            deadline_at=timezone.now() + timedelta(hours=1),
        )

    def test_assignee_can_complete_own_response(self):
        from .responses import apply_response_action

        response, event = apply_response_action(
            response_id=self.response.pk,
            actor_telegram_id=self.assignee.telegram_id,
            action="completed",
            telegram_message_id=777,
        )
        self.assertEqual(response.status, ReminderResponse.Status.COMPLETED)
        self.assertEqual(event.event_type, ReminderResponseEvent.EventType.COMPLETED)
        self.assertIsNotNone(response.responded_at)

    def test_another_user_cannot_answer(self):
        from .responses import ResponseActionError, apply_response_action

        with self.assertRaises(ResponseActionError):
            apply_response_action(
                response_id=self.response.pk,
                actor_telegram_id=self.other_user.telegram_id,
                action="completed",
                telegram_message_id=777,
            )

    def test_stale_telegram_message_is_rejected(self):
        from .responses import ResponseActionError, apply_response_action

        with self.assertRaises(ResponseActionError):
            apply_response_action(
                response_id=self.response.pk,
                actor_telegram_id=self.assignee.telegram_id,
                action="completed",
                telegram_message_id=776,
            )

    def test_defer_records_event_and_due_deferral_returns_to_delivery_queue(self):
        from .responses import apply_response_action, claim_due_deferred_responses

        response, event = apply_response_action(
            response_id=self.response.pk,
            actor_telegram_id=self.assignee.telegram_id,
            action="defer",
            defer_minutes=30,
            telegram_message_id=777,
        )
        self.assertEqual(response.status, ReminderResponse.Status.PENDING)
        self.assertEqual(event.event_type, ReminderResponseEvent.EventType.DEFERRED)
        self.assertIsNone(response.deadline_at)

        ReminderResponse.objects.filter(pk=response.pk).update(deferred_until=timezone.now() - timedelta(seconds=1))
        claimed = claim_due_deferred_responses()
        self.assertIn(response.pk, claimed)
        response.refresh_from_db()
        self.assertEqual(response.delivery_status, ReminderResponse.DeliveryStatus.PENDING)
        self.assertIsNone(response.deferred_until)

    def test_custom_defer_uses_exact_aware_datetime(self):
        from .responses import apply_response_action

        target = timezone.now() + timedelta(hours=3, minutes=17)
        response, event = apply_response_action(
            response_id=self.response.pk,
            actor_telegram_id=self.assignee.telegram_id,
            action="defer",
            defer_until=target,
            telegram_message_id=777,
        )
        self.assertEqual(response.deferred_until, target)
        self.assertEqual(event.deferred_until, target)
        self.assertEqual(event.details, {"mode": "custom"})
        self.assertIsNone(response.deadline_at)

    def test_custom_defer_rejects_naive_datetime(self):
        from datetime import datetime
        from reminders.responses import ResponseActionError, apply_response_action

        with self.assertRaises(ResponseActionError):
            apply_response_action(
                response_id=self.response.pk,
                actor_telegram_id=self.assignee.telegram_id,
                action="defer",
                defer_until=datetime(2026, 11, 16, 10, 0),
                telegram_message_id=777,
            )

    def test_expired_response_is_marked_and_logged(self):
        from .responses import expire_due_responses

        ReminderResponse.objects.filter(pk=self.response.pk).update(deadline_at=timezone.now() - timedelta(seconds=1))
        expired = expire_due_responses()
        self.assertEqual(len(expired), 1)
        self.response.refresh_from_db()
        self.assertEqual(self.response.status, ReminderResponse.Status.EXPIRED)
        self.assertEqual(
            self.response.events.get().event_type,
            ReminderResponseEvent.EventType.EXPIRED,
        )


class ReminderManagementTests(TestCase):
    def setUp(self):
        self.author = TelegramUser.objects.create_user(telegram_id=8001, username="manage_author")
        self.controller = TelegramUser.objects.create_user(telegram_id=8002, username="manage_controller")
        self.assignee = TelegramUser.objects.create_user(telegram_id=8003, username="manage_assignee")
        self.outsider = TelegramUser.objects.create_user(telegram_id=8004, username="manage_outsider")
        self.reminder = Reminder.objects.create(
            title="Кероване нагадування",
            author=self.author,
            first_reminder_at=timezone.now() + timedelta(hours=2),
            next_reminder_at=timezone.now() + timedelta(hours=2),
            timezone="Europe/Kyiv",
        )
        ReminderParticipant.objects.create(
            reminder=self.reminder, user=self.controller,
            role=ReminderParticipant.Role.CONTROLLER, added_by=self.author,
        )
        ReminderParticipant.objects.create(
            reminder=self.reminder, user=self.assignee,
            role=ReminderParticipant.Role.ASSIGNEE, added_by=self.author,
        )

    def test_controller_can_list_view_pause_resume_rename_and_delete(self):
        from .controls import (
            list_reminders, get_reminder_detail, set_reminder_active,
            rename_reminder, delete_reminder,
        )

        self.assertEqual([item["id"] for item in list_reminders(self.controller.telegram_id, "controlled")], [self.reminder.pk])
        self.assertTrue(get_reminder_detail(self.reminder.pk, self.controller.telegram_id)["is_controller"])
        self.assertTrue(set_reminder_active(self.reminder.pk, self.controller.telegram_id, False))
        self.reminder.refresh_from_db()
        self.assertFalse(self.reminder.is_active)
        self.assertTrue(set_reminder_active(self.reminder.pk, self.controller.telegram_id, True))
        self.assertTrue(rename_reminder(self.reminder.pk, self.controller.telegram_id, "Нова назва"))
        self.reminder.refresh_from_db()
        self.assertEqual(self.reminder.title, "Нова назва")
        self.assertTrue(delete_reminder(self.reminder.pk, self.controller.telegram_id))
        self.assertFalse(Reminder.objects.filter(pk=self.reminder.pk).exists())

    def test_assignee_can_view_but_cannot_manage(self):
        from .controls import get_reminder_detail, set_reminder_active, rename_reminder, delete_reminder

        detail = get_reminder_detail(self.reminder.pk, self.assignee.telegram_id)
        self.assertIsNotNone(detail)
        self.assertFalse(detail["is_controller"])
        self.assertFalse(set_reminder_active(self.reminder.pk, self.assignee.telegram_id, False))
        self.assertFalse(rename_reminder(self.reminder.pk, self.assignee.telegram_id, "Чужа назва"))
        self.assertFalse(delete_reminder(self.reminder.pk, self.assignee.telegram_id))

    def test_unrelated_user_cannot_view_or_manage(self):
        from .controls import get_reminder_detail, set_reminder_active, delete_reminder

        self.assertIsNone(get_reminder_detail(self.reminder.pk, self.outsider.telegram_id))
        self.assertFalse(set_reminder_active(self.reminder.pk, self.outsider.telegram_id, False))
        self.assertFalse(delete_reminder(self.reminder.pk, self.outsider.telegram_id))

    def test_completed_one_time_reminder_cannot_be_resumed(self):
        from .controls import set_reminder_active

        self.reminder.next_reminder_at = None
        self.reminder.save(update_fields=["next_reminder_at"])
        self.reminder.is_active = False
        self.reminder.save(update_fields=["is_active"])
        self.assertFalse(set_reminder_active(self.reminder.pk, self.author.telegram_id, True))

    def test_assigned_list_does_not_include_unrelated_reminders(self):
        from .controls import list_reminders

        items = list_reminders(self.assignee.telegram_id, "assigned")
        self.assertEqual([item["id"] for item in items], [self.reminder.pk])
        self.assertEqual(list_reminders(self.outsider.telegram_id, "assigned"), [])
