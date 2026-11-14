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
