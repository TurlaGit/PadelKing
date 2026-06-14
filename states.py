from aiogram.fsm.state import State, StatesGroup


class Register(StatesGroup):
    """FSM записи игрока на турнир (Этап 1.2)."""
    name = State()              # ждём кастомное имя (если не из Telegram)
    partner_username = State()  # ждём @ник партнёра


class AddPair(StatesGroup):
    """Админ добавляет пару вручную (Этап 2.4)."""
    player = State()
    partner = State()


class NewTournament(StatesGroup):
    """Визард создания турнира (Этап 4)."""
    title = State()
    fmt_custom = State()
    date = State()
    time = State()
    loc_new_title = State()
    loc_new_url = State()
    game_format = State()
    lv_custom = State()
    price = State()
    max_pairs = State()
    extra = State()
