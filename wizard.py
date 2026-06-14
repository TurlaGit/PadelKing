"""Этап 4.1 — визард «Создать турнир» (для ADMIN_IDS).

Шаги: название → формат → дата → время → локация → формат игры →
уровни (мультивыбор) → валюта → цена → макс. пар → доп. → превью →
публикация. «Изменить шаг»/«Заново»/отложенная публикация — шаг 4.2.
"""
import json
import logging
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
        "👥 Максимум пар (число) — или нажми «Без лимита»:",
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
        [_btn("📤 Опубликовать сейчас", "tw:publish")],
        [_btn("❌ Отмена", "tw:cancel")],
    ]
    await message.answer(text, reply_markup=_kb(rows))


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
    await _go_format(message, state)


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
    await _go_date(cb.message, state)
    await cb.answer()


@router.message(NewTournament.fmt_custom, F.text)
async def st_format_custom(message: Message, state: FSMContext):
    fmt = message.text.strip()[:60]
    await db.add_format(fmt)
    await state.update_data(format_type=fmt)
    await state.set_state(None)
    await _go_date(message, state)


# ---------- date ----------

@router.message(NewTournament.date, F.text)
async def st_date(message: Message, state: FSMContext):
    from timeutils import parse_date
    d = parse_date(message.text)
    if not d:
        await message.answer("Не понял дату. Формат ДД.ММ или ДД.ММ.ГГГГ:")
        return
    await state.update_data(date_iso=d.isoformat(), date_display=d.strftime("%d.%m"))
    await _go_time(message, state)


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
    await _go_location(message, state)


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
    await _go_game_format(cb.message, state)
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
    await _go_game_format(message, state)


# ---------- game format ----------

@router.message(NewTournament.game_format, F.text)
async def st_game_format(message: Message, state: FSMContext):
    await state.update_data(game_format=message.text.strip()[:500])
    await state.set_state(None)
    await _go_levels(message, state)


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
        await _go_currency(cb.message, state)
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
    await _go_max_pairs(message, state)


# ---------- max pairs ----------

@router.callback_query(F.data == "twmp:none")
async def st_max_none(cb: CallbackQuery, state: FSMContext):
    await state.update_data(max_pairs=None)
    await _go_extra(cb.message, state)
    await cb.answer()


@router.message(NewTournament.max_pairs, F.text)
async def st_max_pairs(message: Message, state: FSMContext):
    raw = message.text.strip()
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer("Нужно положительное число или кнопка «Без лимита»:")
        return
    await state.update_data(max_pairs=int(raw))
    await state.set_state(None)
    await _go_extra(message, state)


# ---------- extra ----------

@router.callback_query(F.data == "twe:skip")
async def st_extra_skip(cb: CallbackQuery, state: FSMContext):
    await state.update_data(extra_note=None)
    await _go_preview(cb.message, state)
    await cb.answer()


@router.message(NewTournament.extra, F.text)
async def st_extra(message: Message, state: FSMContext):
    await state.update_data(extra_note=message.text.strip()[:500])
    await state.set_state(None)
    await _go_preview(message, state)


# ---------- publish ----------

@router.callback_query(F.data == "tw:cancel")
async def st_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.answer("Создание турнира отменено.")
    await cb.answer()


@router.callback_query(F.data == "tw:publish")
async def st_publish(cb: CallbackQuery, state: FSMContext, bot: Bot):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Только для администратора.", show_alert=True)
        return
    data = await state.get_data()
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
        "extra_note": data.get("extra_note"),
        "start_at": to_iso(start_at),
        "payment_deadline": to_iso(deadline),
        "publish_at": None,
        "status_v2": "published",
    })
    await state.clear()

    if GROUP_CHAT_ID:
        await refresh_announcement(bot, tid)
        await cb.message.answer("✅ Турнир создан и анонс опубликован в группе!")
    else:
        await cb.message.answer(
            "✅ Турнир создан. GROUP_CHAT_ID не задан — анонс в группу не отправлен."
        )
    await cb.answer()
    log.info("tournament %s created by admin %s", tid, cb.from_user.id)
