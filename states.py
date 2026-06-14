from aiogram.fsm.state import State, StatesGroup


class Register(StatesGroup):
    """FSM записи игрока на турнир (Этап 1.2)."""
    name = State()              # ждём кастомное имя (если не из Telegram)
    partner_username = State()  # ждём @ник партнёра


class AdminTournament(StatesGroup):
    """Визард создания турнира (Этап 4). Заглушка под будущие шаги."""
    pass
