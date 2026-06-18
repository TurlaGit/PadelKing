"""Этап 4.1 — визард «Создать турнир» (для ADMIN_IDS).

Шаги: название → формат → дата → время → локация → формат игры →
уровни (мультивыбор) → валюта → цена → макс. пар → доп. → превью →
публикация. «Изменить шаг»/«Заново»/отложенная публикация — шаг 4.2.
"""
import json
import logging
import re
import uuid
from datetime import date as _date

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import db
from announcement import refresh_announcement
from config import ADMIN_IDS, GROUP_CHAT_ID
from formatting import render_tournament
from states import NewTournament
from timeutils import make_start_at, payment_deadline, to_iso

log = logging.getLogger(__name__)
router = Router()

# Defense-in-depth: весь визард доступен только администраторам.
router.message.filter(F.from_user.id.in_(ADMIN_IDS))
router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))

_CURRENCIES = ["IDR", "USD", "RUB"]


def _is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def _kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


# ============================================================
#  Переходы между шагами (каждый go_* шлёт промпт и ставит состояние)
# ============================================================

async def _after_step(message: Message, state: FSMContext, default_next):
    """В режиме редактирования возвращаемся в превью, иначе — на след. шаг."""
    data = await state.get_data()
    if data.get("edit_mode"):
        await state.update_data(edit_mode=False)
        await _go_preview(message, state)
    else:
        await default_next(message, state)


async def _go_title(message: Message, state: FSMContext):
    await state.set_state(NewTournament.title)
    await message.answer("Название турнира:")


async def _go_format(message: Message, state: FSMContext):
    formats = await db.list_formats()
    await state.update_data(fmt_opts=formats)
    rows = [[_btn(f, f"twf:{i}")] for i, f in enumerate(formats)]
    rows.append([_btn("✏️ Свой формат", "twf:custom")])
    await message.answer("Формат турнира:", reply_markup=_kb(rows))


async def _go_date(message: Message, state: FSMContext):
    await state.set_state(NewTournament.date)
    await message.answer("📅 Дата турнира (ДД.ММ или ДД.ММ.ГГГГ):")


async def _go_time(message: Message, state: FSMContext):
    await state.set_state(NewTournament.time)
    await message.answer("⏰ Время (например <code>19:00-21:00</code>):")


async def _go_location(message: Message, state: FSMContext):
    locs = await db.list_locations()
    await state.update_data(loc_opts=locs)
    rows = [[_btn(loc["title"][:60], f"twl:{i}")] for i, loc in enumerate(locs)]
    rows.append([_btn("➕ Добавить новую", "twl:new")])
    await message.answer("📍 Локация:", reply_markup=_kb(rows))


async def _go_game_format(message: Message, state: FSMContext):
    await state.set_state(NewTournament.game_format)
    await message.answer(
        "🎮 Формат игры — пришли текст (можно несколько строк), например:\n"
        "<code>• 3 корта\n• 24 очка</code>"
    )


async def _go_levels(message: Message, state: FSMContext):
    data = await state.get_data()
    data.setdefault("levels_sel", [])
    await state.update_data(levels_sel=data["levels_sel"])
    await message.answer("🎯 Уровни игроков — отметь все нужные:",
                         reply_markup=await _levels_kb(state))


async def _levels_kb(state: FSMContext) -> InlineKeyboardMarkup:
    data = await state.get_data()
    levels = await db.list_levels()
    sel = set(data.get("levels_sel", []))
    rows = []
    for i, lv in enumerate(levels):
        mark = "✅ " if lv in sel else "▫️ "
        rows.append([_btn(f"{mark}{lv}", f"twlv:{i}")])
    rows.append([_btn("✏️ Свой уровень", "twlv:custom")])
    rows.append([_btn("✅ Готово", "twlv:done")])
    return _kb(rows)


async def _go_currency(message: Message, state: FSMContext):
    rows = [[_btn(c, f"twc:{c}")] for c in _CURRENCIES]
    await message.answer("💰 Валюта стоимости:", reply_markup=_kb(rows))


async def _go_price(message: Message, state: FSMContext):
    await state.set_state(NewTournament.price)
    await message.answer("💰 Стоимость с человека (число), например <code>350000</code>:")


async def _go_max_pairs(message: Message, state: FSMContext):
    await state.set_state(NewTournament.max_pairs)
    await message.answer(
        "👥 Количество <b>участников</b>: укажи число (например <code>16</code>) "
        "или диапазон (например <code>12-16</code>). Лишние пойдут в лист ожидания.\n"
        "Можно «Без лимита». Если введёшь нечётное — округлим вверх до пары.",
        reply_markup=_kb([[_btn("♾ Без лимита", "twmp:none")]]),
    )


async def _go_extra(message: Message, state: FSMContext):
    await state.set_state(NewTournament.extra)
    await message.answer(
        "📝 Добавить что-то ещё? Пришли текст или нажми «Пропустить»:",
        reply_markup=_kb([[_btn("⏭ Пропустить", "twe:skip")]]),
    )


# ============================================================
#  Превью
# ============================================================

def _preview_tournament(data: dict) -> tuple[dict, dict | None]:
    t = {
        "title": data.get("title"),
        "format_type": data.get("format_type"),
        "date": data.get("date_display"),
        "time_start": data.get("time_start"),
        "time_end": data.get("time_end"),
        "game_format": data.get("game_format"),
        "levels": json.dumps(data.get("levels_sel", []), ensure_ascii=False),
        "currency": data.get("currency"),
        "price_amount": data.get("price_amount"),
        "max_pairs": data.get("max_pairs"),
        "min_pairs": data.get("min_pairs"),
        "extra_note": data.get("extra_note"),
        "start_at": data.get("date_iso"),
    }
    loc = None
    if data.get("location_id"):
        loc = {"title": data.get("location_title"), "maps_url": data.get("location_url")}
    return t, loc


async def _go_preview(message: Message, state: FSMContext):
    data = await state.get_data()
    t, loc = _preview_tournament(data)
    text = "👀 <b>Превью анонса:</b>\n\n" + render_tournament(t, loc, [], [])
    rows = [
        [_btn("✏️ Изменить шаг", "twedit:menu")],
        [_btn("🔄 Заполнить заново", "tw:restart")],
        [_btn("📤 Опубликовать", "tw:pubmenu")],
        [_btn("❌ Отмена", "tw:cancel")],
    ]
    await message.answer(text, reply_markup=_kb(rows))


# Шаги, доступные для редактирования: ключ → (подпись, go-функция)
_EDIT_STEPS = [
    ("title", "Название"),
    ("format", "Формат"),
    ("date", "Дата"),
    ("time", "Время"),
    ("location", "Локация"),
    ("game_format", "Формат игры"),
    ("levels", "Уровни"),
    ("price", "Стоимость"),
    ("max_pairs", "Макс. пар"),
    ("extra", "Доп. текст"),
]
_EDIT_GO = {
    "title": _go_title, "format": _go_format, "date": _go_date, "time": _go_time,
    "location": _go_location, "game_format": _go_game_format, "levels": _go_levels,
    "price": _go_currency, "max_pairs": _go_max_pairs, "extra": _go_extra,
}


@router.callback_query(F.data == "twedit:menu")
async def edit_menu(cb: CallbackQuery, state: FSMContext):
    rows = [[_btn(label, f"twedit:{key}")] for key, label in _EDIT_STEPS]
    rows.append([_btn("⬅️ Назад к превью", "twedit:back")])
    await cb.message.answer("Что изменить?", reply_markup=_kb(rows))
    await cb.answer()


@router.callback_query(F.data == "twedit:back")
async def edit_back(cb: CallbackQuery, state: FSMContext):
    await _go_preview(cb.message, state)
    await cb.answer()


@router.callback_query(F.data.startswith("twedit:"))
async def edit_step(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":", 1)[1]
    go = _EDIT_GO.get(key)
    if not go:
        await cb.answer()
        return
    await state.update_data(edit_mode=True)
    await go(cb.message, state)
    await cb.answer()


@router.callback_query(F.data == "tw:restart")
async def restart(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(NewTournament.title)
    await cb.message.answer("🔄 Начинаем заново.\nНазвание турнира:")
    await cb.answer()


# ============================================================
#  Вход
# ============================================================

@router.message(Command("newtournament"))
async def cmd_new(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    await state.set_state(NewTournament.title)
    await message.answer("🏆 Создаём турнир.\nНазвание:")


@router.callback_query(F.data == "tw:start")
async def cb_new(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    await state.clear()
    await state.set_state(NewTournament.title)
    await cb.message.answer("🏆 Создаём турнир.\nНазвание:")
    await cb.answer()


# ---------- title ----------

@router.message(NewTournament.title, F.text)
async def st_title(message: Message, state: FSMContext):
    title = message.text.strip()[:120]
    if not title:
        await message.answer("Название не может быть пустым. Введи ещё раз:")
        return
    await state.update_data(title=title)
    await state.set_state(None)
    await _after_step(message, state, _go_format)


# ---------- format ----------

@router.callback_query(F.data.startswith("twf:"))
async def st_format(cb: CallbackQuery, state: FSMContext):
    val = cb.data.split(":", 1)[1]
    if val == "custom":
        await state.set_state(NewTournament.fmt_custom)
        await cb.message.answer("Введи название формата:")
        await cb.answer()
        return
    data = await state.get_data()
    opts = data.get("fmt_opts", [])
    fmt = opts[int(val)] if val.isdigit() and int(val) < len(opts) else None
    await state.update_data(format_type=fmt)
    await _after_step(cb.message, state, _go_date)
    await cb.answer()


@router.message(NewTournament.fmt_custom, F.text)
async def st_format_custom(message: Message, state: FSMContext):
    fmt = message.text.strip()[:60]
    await db.add_format(fmt)
    await state.update_data(format_type=fmt)
    await state.set_state(None)
    await _after_step(message, state, _go_date)


# ---------- date ----------

@router.message(NewTournament.date, F.text)
async def st_date(message: Message, state: FSMContext):
    from timeutils import parse_date
    d = parse_date(message.text)
    if not d:
        await message.answer("Не понял дату. Формат ДД.ММ или ДД.ММ.ГГГГ:")
        return
    await state.update_data(date_iso=d.isoformat(), date_display=d.strftime("%d.%m"))
    await _after_step(message, state, _go_time)


# ---------- time ----------

@router.message(NewTournament.time, F.text)
async def st_time(message: Message, state: FSMContext):
    from timeutils import parse_time_range
    ts, te = parse_time_range(message.text)
    if not ts:
        await message.answer("Не понял время. Например <code>19:00-21:00</code>:")
        return
    await state.update_data(time_start=ts, time_end=te)
    await state.set_state(None)
    await _after_step(message, state, _go_location)


# ---------- location ----------

@router.callback_query(F.data.startswith("twl:"))
async def st_location(cb: CallbackQuery, state: FSMContext):
    val = cb.data.split(":", 1)[1]
    if val == "new":
        await state.set_state(NewTournament.loc_new_title)
        await cb.message.answer("Название новой локации:")
        await cb.answer()
        return
    data = await state.get_data()
    opts = data.get("loc_opts", [])
    if val.isdigit() and int(val) < len(opts):
        loc = opts[int(val)]
        await state.update_data(
            location_id=loc["id"], location_title=loc["title"],
            location_url=loc.get("maps_url"),
        )
    await _after_step(cb.message, state, _go_game_format)
    await cb.answer()


@router.message(NewTournament.loc_new_title, F.text)
async def st_loc_title(message: Message, state: FSMContext):
    await state.update_data(_new_loc_title=message.text.strip()[:120])
    await state.set_state(NewTournament.loc_new_url)
    await message.answer("Ссылка на Google Maps (или пришли «-», если нет):")


@router.message(NewTournament.loc_new_url, F.text)
async def st_loc_url(message: Message, state: FSMContext):
    data = await state.get_data()
    title = data.get("_new_loc_title")
    url = message.text.strip()
    url = None if url in ("-", "—", "") else url
    loc_id = await db.add_location(title, url)
    await state.update_data(location_id=loc_id, location_title=title, location_url=url)
    await state.set_state(None)
    await _after_step(message, state, _go_game_format)


# ---------- game format ----------

@router.message(NewTournament.game_format, F.text)
async def st_game_format(message: Message, state: FSMContext):
    await state.update_data(game_format=message.text.strip()[:500])
    await state.set_state(None)
    await _after_step(message, state, _go_levels)


# ---------- levels (multiselect) ----------

@router.callback_query(F.data.startswith("twlv:"))
async def st_levels(cb: CallbackQuery, state: FSMContext):
    val = cb.data.split(":", 1)[1]
    data = await state.get_data()
    sel = list(data.get("levels_sel", []))

    if val == "custom":
        await state.set_state(NewTournament.lv_custom)
        await cb.message.answer("Введи название уровня:")
        await cb.answer()
        return
    if val == "done":
        if not sel:
            await cb.answer("Отметь хотя бы один уровень.", show_alert=True)
            return
        await _after_step(cb.message, state, _go_currency)
        await cb.answer()
        return

    levels = await db.list_levels()
    if val.isdigit() and int(val) < len(levels):
        lv = levels[int(val)]
        if lv in sel:
            sel.remove(lv)
        else:
            sel.append(lv)
        await state.update_data(levels_sel=sel)
        try:
            await cb.message.edit_reply_markup(reply_markup=await _levels_kb(state))
        except Exception:
            pass
    await cb.answer()


@router.message(NewTournament.lv_custom, F.text)
async def st_level_custom(message: Message, state: FSMContext):
    lv = message.text.strip()[:40]
    await db.add_level(lv)
    data = await state.get_data()
    sel = list(data.get("levels_sel", []))
    if lv not in sel:
        sel.append(lv)
    await state.update_data(levels_sel=sel)
    await state.set_state(None)
    await message.answer("Добавил. Отметь ещё или нажми «Готово»:",
                         reply_markup=await _levels_kb(state))


# ---------- currency / price ----------

@router.callback_query(F.data.startswith("twc:"))
async def st_currency(cb: CallbackQuery, state: FSMContext):
    cur = cb.data.split(":", 1)[1]
    await state.update_data(currency=cur)
    await _go_price(cb.message, state)
    await cb.answer()


@router.message(NewTournament.price, F.text)
async def st_price(message: Message, state: FSMContext):
    raw = message.text.strip().replace(".", "").replace(" ", "").replace(",", "")
    if not raw.isdigit():
        await message.answer("Нужно число, например <code>350000</code>:")
        return
    await state.update_data(price_amount=float(raw))
    await state.set_state(None)
    await _after_step(message, state, _go_max_pairs)


# ---------- max pairs ----------

@router.callback_query(F.data == "twmp:none")
async def st_max_none(cb: CallbackQuery, state: FSMContext):
    await state.update_data(max_pairs=None, min_pairs=None)
    await _after_step(cb.message, state, _go_extra)
    await cb.answer()


_RANGE_RE = re.compile(r"^\s*(\d+)\s*[-–—]\s*(\d+)\s*$")


def _to_pairs(n: int) -> int:
    """Игроки → пары (округление вверх)."""
    return (n + 1) // 2


@router.message(NewTournament.max_pairs, F.text)
async def st_max_pairs(message: Message, state: FSMContext):
    raw = message.text.strip()
    m = _RANGE_RE.match(raw)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a <= 0 or b <= 0 or a > b:
            await message.answer("Диапазон должен быть вида <code>12-16</code> (мин ≤ макс).")
            return
        await state.update_data(min_pairs=_to_pairs(a), max_pairs=_to_pairs(b))
    elif raw.isdigit() and int(raw) > 0:
        await state.update_data(min_pairs=None, max_pairs=_to_pairs(int(raw)))
    else:
        await message.answer(
            "Нужно число (<code>16</code>) или диапазон (<code>12-16</code>) участников, "
            "либо нажми «Без лимита»."
        )
        return
    await state.set_state(None)
    await _after_step(message, state, _go_extra)


# ---------- extra ----------

@router.callback_query(F.data == "twe:skip")
async def st_extra_skip(cb: CallbackQuery, state: FSMContext):
    await state.update_data(extra_note=None)
    await _after_step(cb.message, state, _go_preview)
    await cb.answer()


@router.message(NewTournament.extra, F.text)
async def st_extra(message: Message, state: FSMContext):
    await state.update_data(extra_note=message.text.strip()[:500])
    await state.set_state(None)
    await _after_step(message, state, _go_preview)


# ---------- publish ----------

@router.callback_query(F.data == "tw:cancel")
async def st_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.answer("Создание турнира отменено.")
    await cb.answer()


async def _insert_from_data(data: dict, publish_at_iso: str | None, status: str) -> str:
    tid = uuid.uuid4().hex[:10]
    d = _date.fromisoformat(data["date_iso"])
    start_at = make_start_at(d, data.get("time_start"))
    deadline = payment_deadline(start_at)
    await db.insert_tournament({
        "id": tid,
        "title": data.get("title"),
        "format_type": data.get("format_type"),
        "date": data.get("date_display"),
        "time_start": data.get("time_start"),
        "time_end": data.get("time_end"),
        "location_id": data.get("location_id"),
        "game_format": data.get("game_format"),
        "levels": json.dumps(data.get("levels_sel", []), ensure_ascii=False),
        "currency": data.get("currency"),
        "price_amount": data.get("price_amount"),
        "max_pairs": data.get("max_pairs"),
        "min_pairs": data.get("min_pairs"),
        "extra_note": data.get("extra_note"),
        "start_at": to_iso(start_at),
        "payment_deadline": to_iso(deadline),
        "publish_at": publish_at_iso,
        "status_v2": status,
    })
    return tid


@router.callback_query(F.data == "tw:pubmenu")
async def pub_menu(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    rows = [
        [_btn("📤 Опубликовать сейчас", "tw:pubnow")],
        [_btn("🕐 Запланировать", "tw:pubsched")],
        [_btn("⬅️ Назад к превью", "twedit:back")],
    ]
    await cb.message.answer("Когда публиковать анонс?", reply_markup=_kb(rows))
    await cb.answer()


@router.callback_query(F.data == "tw:pubnow")
async def pub_now(cb: CallbackQuery, state: FSMContext, bot: Bot):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    data = await state.get_data()
    tid = await _insert_from_data(data, None, "published")
    await state.clear()
    if GROUP_CHAT_ID:
        await refresh_announcement(bot, tid)
        await cb.message.answer("✅ Турнир создан и анонс опубликован в группе!")
    else:
        await cb.message.answer(
            "✅ Турнир создан. GROUP_CHAT_ID не задан — анонс в группу не отправлен."
        )
    await cb.answer()
    log.info("tournament %s published by admin %s", tid, cb.from_user.id)


@router.callback_query(F.data == "tw:pubsched")
async def pub_sched(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    await state.set_state(NewTournament.schedule_at)
    await cb.message.answer(
        "🕐 Когда опубликовать? Пришли дату и время (по Бали), например "
        "<code>20.06 09:00</code>:"
    )
    await cb.answer()


@router.message(NewTournament.schedule_at, F.text)
async def st_schedule(message: Message, state: FSMContext):
    from timeutils import WITA, now_wita, parse_date, parse_time
    from datetime import datetime
    parts = message.text.strip().split()
    d = parse_date(parts[0]) if parts else None
    tm = parse_time(parts[1]) if len(parts) > 1 else None
    if not d or not tm:
        await message.answer("Не понял. Формат: <code>20.06 09:00</code>:")
        return
    when = datetime.combine(d, tm, tzinfo=WITA)
    if when <= now_wita():
        await message.answer("Это время уже прошло. Укажи будущие дату и время:")
        return
    data = await state.get_data()
    tid = await _insert_from_data(data, to_iso(when), "scheduled")
    await state.clear()
    await message.answer(
        f"✅ Турнир создан. Анонс выйдет автоматически "
        f"<b>{when.strftime('%d.%m в %H:%M')}</b> (Бали).\n"
        "<i>Авто-публикация по расписанию заработает после Этапа 3 (планировщик).</i>"
    )
    log.info("tournament %s scheduled for %s by admin %s", tid, to_iso(when), message.from_user.id)
