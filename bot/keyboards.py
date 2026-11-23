"""Клавіатури для дій із Telegram-нагадуваннями."""

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder


def response_keyboard(response_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Виконано", callback_data=f"rem:{response_id}:completed"
                ),
                InlineKeyboardButton(
                    text="❌ Відхилити", callback_data=f"rem:{response_id}:rejected"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⏰ +30 хв", callback_data=f"rem:{response_id}:defer30"
                ),
                InlineKeyboardButton(
                    text="🕒 Інший час", callback_data=f"rem:{response_id}:custom"
                ),
            ],
        ]
    )


def defer_keyboard(response_id: int) -> InlineKeyboardMarkup:
    choices = (
        (15, "15 хв"),
        (30, "30 хв"),
        (60, "1 год"),
        (120, "2 год"),
        (180, "3 год"),
        (1440, "24 год"),
    )
    rows = [
        [
            InlineKeyboardButton(
                text=label, callback_data=f"rem:{response_id}:d{minutes}"
            )
            for minutes, label in choices[:3]
        ],
        [
            InlineKeyboardButton(
                text=label, callback_data=f"rem:{response_id}:d{minutes}"
            )
            for minutes, label in choices[3:]
        ],
    ]
    rows.append(
        [InlineKeyboardButton(text="↩️ Назад", callback_data=f"rem:{response_id}:back")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reminders_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛠 Я контролюю", callback_data="mgr:list:controlled:0"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📥 Призначені мені", callback_data="mgr:list:assigned:0"
                )
            ],
            [
                InlineKeyboardButton(
                    text="➕ Створити нагадування", callback_data="mgr:new"
                )
            ],
        ]
    )


def reminders_list_keyboard(
    items: list[dict], mode: str, page: int, page_size: int = 5
) -> InlineKeyboardMarkup:
    start = page * page_size
    page_items = items[start : start + page_size]
    rows = []
    for item in page_items:
        status = "🟢" if item["is_active"] else "⏸"
        title = item["title"][:38]
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{status} {title}",
                    callback_data=f"mgr:view:{item['id']}:{mode}",
                )
            ]
        )
    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(text="⬅️", callback_data=f"mgr:list:{mode}:{page - 1}")
        )
    if start + page_size < len(items):
        nav.append(
            InlineKeyboardButton(text="➡️", callback_data=f"mgr:list:{mode}:{page + 1}")
        )
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="↩️ Меню", callback_data="mgr:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reminder_detail_keyboard(
    reminder_id: int, *, can_manage: bool, is_active: bool
) -> InlineKeyboardMarkup:
    rows = []
    if can_manage:
        rows.append(
            [
                InlineKeyboardButton(
                    text="⏸ Призупинити" if is_active else "▶️ Відновити",
                    callback_data=f"mgr:{'pause' if is_active else 'resume'}:{reminder_id}",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="✏️ Редагувати", callback_data=f"mgr:edit_menu:{reminder_id}"
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="🗑 Видалити", callback_data=f"mgr:delete_confirm:{reminder_id}"
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="↩️ До списку", callback_data="mgr:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def delete_confirmation_keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Так, видалити", callback_data=f"mgr:delete:{reminder_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Ні, залишити",
                    callback_data=f"mgr:cancel_delete:{reminder_id}",
                )
            ],
        ]
    )


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Відмінити", callback_data="cancel_fsm")]
        ]
    )


def skip_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Пропустити", callback_data="skip")
    builder.attach(InlineKeyboardBuilder.from_markup(cancel_keyboard()))
    builder.adjust(2)

    return builder.as_markup()


def repeat_time_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    # Додаємо 5 кнопок вибору
    builder.button(text="Одноразово", callback_data="rec:одноразове")
    builder.button(text="Щодня", callback_data="rec:щодня")
    builder.button(text="Щотижня", callback_data="rec:щотижня")
    builder.button(text="Щомісяця", callback_data="rec:щомісяця")
    builder.button(text="Щороку", callback_data="rec:щороку")

    # Підклеюємо вже існуючу cancel_keyboard()
    builder.attach(InlineKeyboardBuilder.from_markup(cancel_keyboard()))

    # Задаємо сітку по рядках: 1, потім 2, потім 2, потім 1
    builder.adjust(1, 2, 2, 1)

    return builder.as_markup()


def nagging_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Кожні 5 хв", callback_data="nag:5")
    builder.button(text="Кожні 10 хв", callback_data="nag:10")
    builder.button(text="Свій інтервал", callback_data="nag:custom")
    builder.button(text="Пропустити", callback_data="nag:skip")
    builder.attach(InlineKeyboardBuilder.from_markup(cancel_keyboard()))
    builder.adjust(2, 2, 1)
    return builder.as_markup()

def ttl_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="1 година", callback_data="ttl:60")
    builder.button(text="3 години", callback_data="ttl:180")
    builder.button(text="Свій інтервал", callback_data="ttl:custom")
    builder.button(text="Пропустити", callback_data="ttl:skip")
    builder.attach(InlineKeyboardBuilder.from_markup(cancel_keyboard()))
    builder.adjust(2, 2, 1)
    return builder.as_markup()


def reminder_edit_menu_keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📝 Назву", callback_data=f"mgr:rename:{reminder_id}"
                ),
                InlineKeyboardButton(
                    text="🕒 Час", callback_data=f"mgr:edit_time:{reminder_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔁 Повторення", callback_data=f"mgr:edit_rec:{reminder_id}"
                ),
                InlineKeyboardButton(
                    text="🔔 Настирливість", callback_data=f"mgr:edit_nag:{reminder_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⏳ Тайм-аут", callback_data=f"mgr:edit_timeout:{reminder_id}"
                ),
                InlineKeyboardButton(
                    text="⏱ Час життя", callback_data=f"mgr:edit_ttl:{reminder_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="👥 Виконавців",
                    callback_data=f"mgr:edit_assignees:{reminder_id}",
                ),
                InlineKeyboardButton(
                    text="👁 Контролерів",
                    callback_data=f"mgr:edit_controllers:{reminder_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="↩️ Назад до нагадування",
                    callback_data=f"mgr:view_id:{reminder_id}",
                )
            ],
        ]
    )
