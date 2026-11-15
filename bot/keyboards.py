"""Клавіатури для дій із Telegram-нагадуваннями."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def response_keyboard(response_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Виконано", callback_data=f"rem:{response_id}:completed"),
            InlineKeyboardButton(text="❌ Відхилити", callback_data=f"rem:{response_id}:rejected"),
        ],
        [
            InlineKeyboardButton(text="⏰ +30 хв", callback_data=f"rem:{response_id}:defer30"),
            InlineKeyboardButton(text="🕒 Інший час", callback_data=f"rem:{response_id}:choose"),
        ],
    ])


def defer_keyboard(response_id: int) -> InlineKeyboardMarkup:
    choices = ((15, "15 хв"), (30, "30 хв"), (60, "1 год"), (120, "2 год"), (180, "3 год"), (1440, "24 год"))
    rows = [[InlineKeyboardButton(text=label, callback_data=f"rem:{response_id}:d{minutes}") for minutes, label in choices[:3]],
            [InlineKeyboardButton(text=label, callback_data=f"rem:{response_id}:d{minutes}") for minutes, label in choices[3:]]]
    rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data=f"rem:{response_id}:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
