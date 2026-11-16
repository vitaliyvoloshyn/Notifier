"""Клавіатури для дій із Telegram-нагадуваннями."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton


def response_keyboard(response_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Виконано", callback_data=f"rem:{response_id}:completed"),
            InlineKeyboardButton(text="❌ Відхилити", callback_data=f"rem:{response_id}:rejected"),
        ],
        [
            InlineKeyboardButton(text="⏰ +30 хв", callback_data=f"rem:{response_id}:defer30"),
            InlineKeyboardButton(text="🕒 Інший час", callback_data=f"rem:{response_id}:custom"),
        ],
    ])


def defer_keyboard(response_id: int) -> InlineKeyboardMarkup:
    choices = ((15, "15 хв"), (30, "30 хв"), (60, "1 год"), (120, "2 год"), (180, "3 год"), (1440, "24 год"))
    rows = [[InlineKeyboardButton(text=label, callback_data=f"rem:{response_id}:d{minutes}") for minutes, label in choices[:3]],
            [InlineKeyboardButton(text=label, callback_data=f"rem:{response_id}:d{minutes}") for minutes, label in choices[3:]]]
    rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data=f"rem:{response_id}:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reminders_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛠 Я контролюю", callback_data="mgr:list:controlled:0")],
        [InlineKeyboardButton(text="📥 Призначені мені", callback_data="mgr:list:assigned:0")],
        [InlineKeyboardButton(text="➕ Створити нагадування", callback_data="mgr:new")],
    ])


def reminders_list_keyboard(items: list[dict], mode: str, page: int, page_size: int = 5) -> InlineKeyboardMarkup:
    start = page * page_size
    page_items = items[start:start + page_size]
    rows = []
    for item in page_items:
        status = "🟢" if item["is_active"] else "⏸"
        title = item["title"][:38]
        rows.append([InlineKeyboardButton(
            text=f"{status} {title}",
            callback_data=f"mgr:view:{item['id']}:{mode}",
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"mgr:list:{mode}:{page - 1}"))
    if start + page_size < len(items):
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"mgr:list:{mode}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="↩️ Меню", callback_data="mgr:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reminder_detail_keyboard(reminder_id: int, *, can_manage: bool, is_active: bool) -> InlineKeyboardMarkup:
    rows = []
    if can_manage:
        rows.append([InlineKeyboardButton(
            text="⏸ Призупинити" if is_active else "▶️ Відновити",
            callback_data=f"mgr:{'pause' if is_active else 'resume'}:{reminder_id}",
        )])
        rows.append([InlineKeyboardButton(text="✏️ Змінити назву", callback_data=f"mgr:rename:{reminder_id}")])
        rows.append([InlineKeyboardButton(text="🗑 Видалити", callback_data=f"mgr:delete_confirm:{reminder_id}")])
    rows.append([InlineKeyboardButton(text="↩️ До списку", callback_data="mgr:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def delete_confirmation_keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Так, видалити", callback_data=f"mgr:delete:{reminder_id}")],
        [InlineKeyboardButton(text="Ні, залишити", callback_data=f"mgr:cancel_delete:{reminder_id}")],
    ])

def main_reply_keyboard() -> ReplyKeyboardMarkup:
  return ReplyKeyboardMarkup(
      keyboard=[
          [
              KeyboardButton(text="🛠 Я контролюю"),
              KeyboardButton(text="📥 Призначені мені"),
          ],
          [KeyboardButton(text="➕ Створити нагадування")],
      ],
      resize_keyboard=True,
      input_field_placeholder="Оберіть дію...",
  )
