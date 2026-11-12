from django.contrib import admin
from .models import TelegramUser

@admin.register(TelegramUser)
class TelegramUserAdmin(admin.ModelAdmin):
    list_display = ('telegram_id', 'username', 'first_name', 'is_active', 'date_joined')
    search_fields = ('telegram_id', 'username', 'first_name')
    list_filter = ('is_active', 'is_staff')
