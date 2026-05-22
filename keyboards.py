from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import BOT_USERNAME


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏆 Ближайшие турниры", callback_data="list")],
            [InlineKeyboardButton(text="📋 Мои записи", callback_data="my_regs")],
            [InlineKeyboardButton(text="❓ Связь с админом", callback_data="contact")],
        ]
    )


def back_to_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="« В меню", callback_data="menu")],
        ]
    )


def tournaments_list(tournaments: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for t in tournaments:
        label = t["title"]
        when = " ".join(filter(None, [t.get("date"), t.get("time")])).strip()
        if when:
            label = f"{label} • {when}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"t:{t['id']}")])
    rows.append([InlineKeyboardButton(text="« В меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def tournament_card(tid: str, user_registered: bool) -> InlineKeyboardMarkup:
    if user_registered:
        action = InlineKeyboardButton(text="❌ Отменить запись", callback_data=f"cancel:{tid}")
    else:
        action = InlineKeyboardButton(text="✅ Записаться", callback_data=f"join:{tid}")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [action],
            [InlineKeyboardButton(text="« К списку", callback_data="list")],
            [InlineKeyboardButton(text="« В меню", callback_data="menu")],
        ]
    )


def announce_button(tid: str, label: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=label,
                    url=f"https://t.me/{BOT_USERNAME}?start={tid}",
                )
            ]
        ]
    )
