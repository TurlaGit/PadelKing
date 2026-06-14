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


def reg_name(tg_name: str) -> InlineKeyboardMarkup:
    """Шаг «как тебя записать» в FSM записи."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"✅ {tg_name}"[:60], callback_data="rn:tg")],
            [InlineKeyboardButton(text="✏️ Ввести другое имя", callback_data="rn:edit")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="rn:cancel")],
        ]
    )


def reg_partner_choice() -> InlineKeyboardMarkup:
    """Шаг «с кем играешь»."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👥 Знаю партнёра", callback_data="pc:known")],
            [InlineKeyboardButton(text="🔍 Ищу партнёра", callback_data="pc:looking")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="rn:cancel")],
        ]
    )


def reg_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="rn:cancel")],
        ]
    )


def pay_choice(rid: int, pays_for: str = "self") -> InlineKeyboardMarkup:
    """Выбор «за себя / за обоих». Текущий выбор помечается галочкой."""
    self_mark = "✅ " if pays_for == "self" else ""
    both_mark = "✅ " if pays_for == "both" else ""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"{self_mark}🙋 Оплачу за себя", callback_data=f"pay:self:{rid}")],
            [InlineKeyboardButton(text=f"{both_mark}👥 Оплачу за обоих", callback_data=f"pay:both:{rid}")],
        ]
    )


def partner_confirm(rid: int) -> InlineKeyboardMarkup:
    """Кнопки в DM партнёру: подтвердить / отказаться от участия в паре."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"pconf:{rid}:yes"),
                InlineKeyboardButton(text="❌ Отказаться", callback_data=f"pconf:{rid}:no"),
            ],
        ]
    )


def pair_request_decision(req_id: int) -> InlineKeyboardMarkup:
    """Кнопки одиночке А: принять/отклонить заявку Б."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Принять", callback_data=f"preq:{req_id}:yes"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"preq:{req_id}:no"),
            ],
        ]
    )


def pair_declined_options(tid: str) -> InlineKeyboardMarkup:
    """Что предложить Б после отказа А."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Записаться на турнир", callback_data=f"reg:{tid}")],
            [InlineKeyboardButton(text="🤝 Выбрать из тех, кто ищет пару", callback_data=f"singles:{tid}")],
            [InlineKeyboardButton(text="🏠 В меню", callback_data="menu")],
        ]
    )


def singles_list(tid: str, singles: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for s in singles:
        name = s.get("player_name") or "Игрок"
        uname = f" @{s['player_username']}" if s.get("player_username") else ""
        rows.append([
            InlineKeyboardButton(
                text=f"✋ {name}{uname}"[:60],
                callback_data=f"pair:{s['id']}",
            )
        ])
    rows.append([InlineKeyboardButton(text="🏠 В меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def my_registrations(regs: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for r in regs:
        when = " ".join(filter(None, [r.get("date"), r.get("time")])).strip()
        label = r.get("title") or "Турнир"
        if when:
            label = f"{label} • {when}"
        rows.append([InlineKeyboardButton(text=label[:60], callback_data=f"t:{r['tid']}")])
    rows.append([InlineKeyboardButton(text="🏠 В меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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


def announce_keyboard(
    tid: str,
    label: str,
    maps_url: str | None = None,
    singles: list[dict] | None = None,
) -> InlineKeyboardMarkup:
    """Клавиатура под анонсом в ГРУППЕ — только URL-кнопки (deep-link),
    т.к. диалог должен идти в личке с ботом."""
    base = f"https://t.me/{BOT_USERNAME}?start="
    rows = [[InlineKeyboardButton(text=label, url=f"{base}{tid}")]]
    if maps_url:
        rows.append([InlineKeyboardButton(text="🔗 Открыть в Maps", url=maps_url)])
    for s in (singles or []):
        name = s.get("player_name") or "игроку"
        rows.append([
            InlineKeyboardButton(
                text=f"✋ В пару к {name}"[:60],
                url=f"{base}pair_{s['id']}",
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def dm_tournament_card(tid: str, my_rid: int | None) -> InlineKeyboardMarkup:
    """Карточка турнира в личке: записаться / отменить + назад."""
    if my_rid:
        action = InlineKeyboardButton(
            text="❌ Отменить запись", callback_data=f"rcancel:{my_rid}"
        )
    else:
        action = InlineKeyboardButton(text="✅ Записаться", callback_data=f"reg:{tid}")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [action],
            [InlineKeyboardButton(text="« К списку", callback_data="list")],
            [InlineKeyboardButton(text="🏠 В меню", callback_data="menu")],
        ]
    )
