from datetime import timedelta

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
