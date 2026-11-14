from django.contrib import admin

from .models import (
    Reminder,
    ReminderOccurrence,
    ReminderParticipant,
    ReminderResponse,
    ReminderResponseEvent,
)


class ReminderParticipantInline(admin.TabularInline):
    model = ReminderParticipant
    extra = 0


@admin.register(Reminder)
class ReminderAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "author",
        "first_reminder_at",
        "next_reminder_at",
        "recurrence_type",
        "is_active",
    )
    list_filter = ("is_active", "recurrence_type", "notification_policy")
    search_fields = ("title", "author__telegram_id", "author__username")
    inlines = (ReminderParticipantInline,)
    date_hierarchy = "created_at"


@admin.register(ReminderOccurrence)
class ReminderOccurrenceAdmin(admin.ModelAdmin):
    list_display = ("reminder", "scheduled_at", "status", "completed_at")
    list_filter = ("status",)
    search_fields = ("reminder__title",)
    date_hierarchy = "scheduled_at"


@admin.register(ReminderResponse)
class ReminderResponseAdmin(admin.ModelAdmin):
    list_display = (
        "occurrence",
        "assignee",
        "status",
        "delivery_status",
        "last_sent_at",
        "deadline_at",
        "deferred_until",
    )
    list_filter = ("status", "delivery_status")
    search_fields = ("occurrence__reminder__title", "assignee__telegram_id")


@admin.register(ReminderResponseEvent)
class ReminderResponseEventAdmin(admin.ModelAdmin):
    list_display = ("response", "event_type", "actor", "occurred_at")
    list_filter = ("event_type",)
    search_fields = ("response__occurrence__reminder__title", "actor__telegram_id")
    readonly_fields = ("response", "event_type", "actor", "occurred_at", "deferred_until", "details")
