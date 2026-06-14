from datetime import datetime, timezone

import aiosqlite

from config import DB_PATH

# Legacy-схема (Этап 0). Будет удалена на шаге 1.5, когда `participants`
# заменим на пары. Пока живёт параллельно с новыми таблицами этапа 1.
SCHEMA = """
CREATE TABLE IF NOT EXISTS tournaments (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    date TEXT,
    time TEXT,
    location TEXT,
    price TEXT,
    description TEXT,
    max_participants INTEGER NOT NULL DEFAULT 8,
    group_chat_id INTEGER,
    announce_chat_id INTEGER,
    announce_message_id INTEGER,
    is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS participants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    username TEXT,
    full_name TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    joined_at TEXT NOT NULL,
    UNIQUE(tournament_id, user_id),
    FOREIGN KEY (tournament_id) REFERENCES tournaments(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_participants_tournament_status
    ON participants(tournament_id, status, id);
"""

# --- Этап 1: новая модель (пары, оплаты по игроку, заявки, локации, кеш юзеров) ---
SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS known_users (
    user_id          INTEGER PRIMARY KEY,
    username         TEXT,
    username_lower   TEXT,
    first_name       TEXT,
    last_name        TEXT,
    is_blocked       INTEGER NOT NULL DEFAULT 0,
    first_seen_at    TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_known_users_username_lower
    ON known_users(username_lower);

CREATE TABLE IF NOT EXISTS locations (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    title     TEXT NOT NULL UNIQUE,
    maps_url  TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dict_levels (
    value TEXT PRIMARY KEY,
    is_builtin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dict_formats (
    value TEXT PRIMARY KEY,
    is_builtin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

-- Регистрация = ПАРА. partner_user_id NULL пока партнёр не привязан
-- (ищет / ждёт подтверждения / введён только ник).
CREATE TABLE IF NOT EXISTS registrations (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id            TEXT NOT NULL,
    player_user_id           INTEGER NOT NULL,
    player_name              TEXT,
    player_username          TEXT,
    partner_user_id          INTEGER,
    partner_name             TEXT,
    partner_username         TEXT,
    is_looking_for_partner   INTEGER NOT NULL DEFAULT 0,
    -- «лист ожидания» — отдельный флаг, чтобы не терять partner-состояние
    is_waitlist              INTEGER NOT NULL DEFAULT 0,
    -- looking | pending_confirm | active | cancelled | removed_unpaid
    status                   TEXT NOT NULL DEFAULT 'pending_confirm',
    slot_index               INTEGER,
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL,
    FOREIGN KEY (tournament_id) REFERENCES tournaments(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_registrations_tournament_status
    ON registrations(tournament_id, status);
CREATE INDEX IF NOT EXISTS ix_registrations_player
    ON registrations(player_user_id);
CREATE INDEX IF NOT EXISTS ix_registrations_partner
    ON registrations(partner_user_id);

-- Платёж по КАЖДОМУ игроку пары (player/partner). Если pays_for='both' —
-- партнёрская строка автоматически закрывается тем же платежом при approve.
CREATE TABLE IF NOT EXISTS payments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    registration_id     INTEGER NOT NULL,
    who                 TEXT NOT NULL,   -- 'player' | 'partner'
    user_id             INTEGER,
    -- unpaid | pending_review | paid | paid_manual | rejected
    status              TEXT NOT NULL DEFAULT 'unpaid',
    pays_for            TEXT NOT NULL DEFAULT 'self', -- 'self' | 'both'
    screenshot_file_id  TEXT,
    reviewed_by         INTEGER,
    reviewed_at         TEXT,
    reminder_stage      INTEGER NOT NULL DEFAULT 0,
    last_reminder_at    TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    FOREIGN KEY (registration_id) REFERENCES registrations(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_payments_registration
    ON payments(registration_id);
CREATE INDEX IF NOT EXISTS ix_payments_status
    ON payments(status);

-- Заявка «✋ Хочу в пару» к одиночке (registrations с is_looking_for_partner=1).
CREATE TABLE IF NOT EXISTS pair_requests (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id       TEXT NOT NULL,
    to_registration_id  INTEGER NOT NULL,
    from_user_id        INTEGER NOT NULL,
    from_name           TEXT,
    from_username       TEXT,
    -- pending | accepted | declined | expired | cancelled
    status              TEXT NOT NULL DEFAULT 'pending',
    created_at          TEXT NOT NULL,
    expires_at          TEXT,
    resolved_at         TEXT,
    FOREIGN KEY (tournament_id) REFERENCES tournaments(id) ON DELETE CASCADE,
    FOREIGN KEY (to_registration_id) REFERENCES registrations(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_pair_requests_target_status
    ON pair_requests(to_registration_id, status);
CREATE INDEX IF NOT EXISTS ix_pair_requests_from_status
    ON pair_requests(from_user_id, status);
"""

# Колонки, которые добавляем к существующей tournaments через ALTER TABLE.
# SQLite не умеет IF NOT EXISTS у ADD COLUMN — проверяем сами.
_TOURNAMENT_NEW_COLUMNS: list[tuple[str, str]] = [
    ("format_type",      "TEXT"),
    ("time_start",       "TEXT"),
    ("time_end",         "TEXT"),
    ("location_id",      "INTEGER"),
    ("game_format",      "TEXT"),
    ("levels",           "TEXT"),    # JSON-массив
    ("currency",         "TEXT"),    # IDR | USD | RUB
    ("price_amount",     "REAL"),
    ("max_pairs",        "INTEGER"),
    ("extra_note",       "TEXT"),
    ("start_at",         "TEXT"),    # ISO datetime в WITA
    ("payment_deadline", "TEXT"),    # ISO datetime в WITA
    ("publish_at",       "TEXT"),    # NULL = сразу
    ("pinned",           "INTEGER NOT NULL DEFAULT 0"),
    # draft | scheduled | published | finished | cancelled
    ("status_v2",        "TEXT"),
    # 'content' (из content.md) | 'admin' (создан визардом)
    ("source",           "TEXT"),
]

# Колонки registrations, добавляемые миграцией к уже созданным БД.
_REGISTRATION_NEW_COLUMNS: list[tuple[str, str]] = [
    ("is_waitlist", "INTEGER NOT NULL DEFAULT 0"),
]

_BUILTIN_LEVELS = [
    "Beginner",
    "Low Bronze", "Mid Bronze", "High Bronze",
    "Low Silver", "Mid Silver", "High Silver",
    "Low Gold",   "Mid Gold",   "High Gold",
]
_BUILTIN_FORMATS = [
    "Americana", "Mexicano", "King of the Court", "Mixed", "Stars",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def _existing_columns(conn, table: str) -> set[str]:
    cur = await conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cur.fetchall()}


async def _migrate_tournaments(conn) -> None:
    existing = await _existing_columns(conn, "tournaments")
    for name, decl in _TOURNAMENT_NEW_COLUMNS:
        if name not in existing:
            await conn.execute(f"ALTER TABLE tournaments ADD COLUMN {name} {decl}")


async def _migrate_registrations(conn) -> None:
    existing = await _existing_columns(conn, "registrations")
    for name, decl in _REGISTRATION_NEW_COLUMNS:
        if name not in existing:
            await conn.execute(f"ALTER TABLE registrations ADD COLUMN {name} {decl}")


async def _seed_dictionaries(conn) -> None:
    now = _now()
    for v in _BUILTIN_LEVELS:
        await conn.execute(
            "INSERT OR IGNORE INTO dict_levels(value, is_builtin, created_at) "
            "VALUES (?, 1, ?)",
            (v, now),
        )
    for v in _BUILTIN_FORMATS:
        await conn.execute(
            "INSERT OR IGNORE INTO dict_formats(value, is_builtin, created_at) "
            "VALUES (?, 1, ?)",
            (v, now),
        )


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.executescript(SCHEMA)
        await conn.executescript(SCHEMA_V2)
        await _migrate_tournaments(conn)
        await _migrate_registrations(conn)
        await _seed_dictionaries(conn)
        await conn.commit()


# ============================================================
#  known_users — кеш всех, кто хоть раз контактировал с ботом.
#  Нужен §5.3 (резолв @ника → user_id) и пометке is_blocked.
# ============================================================

async def upsert_known_user(
    user_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
) -> None:
    now = _now()
    uname_lower = username.lower() if username else None
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """
            INSERT INTO known_users
                (user_id, username, username_lower, first_name, last_name,
                 first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username       = excluded.username,
                username_lower = excluded.username_lower,
                first_name     = excluded.first_name,
                last_name      = excluded.last_name,
                last_seen_at   = excluded.last_seen_at,
                -- если человек снова зашёл, снимаем флаг блокировки
                is_blocked     = 0
            """,
            (user_id, username, uname_lower, first_name, last_name, now, now),
        )
        await conn.commit()


async def find_user_by_username(username: str) -> dict | None:
    if not username:
        return None
    uname = username.lstrip("@").strip().lower()
    if not uname:
        return None
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM known_users WHERE username_lower=?",
            (uname,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def mark_user_blocked(user_id: int, blocked: bool = True) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE known_users SET is_blocked=? WHERE user_id=?",
            (1 if blocked else 0, user_id),
        )
        await conn.commit()


# ============================================================
#  locations / dict_levels / dict_formats — справочники для админки
# ============================================================

async def list_locations() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM locations ORDER BY title")
        return [dict(r) for r in await cur.fetchall()]


async def add_location(title: str, maps_url: str | None) -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "INSERT INTO locations(title, maps_url, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(title) DO UPDATE SET maps_url=excluded.maps_url "
            "RETURNING id",
            (title.strip(), (maps_url or "").strip() or None, _now()),
        )
        row = await cur.fetchone()
        await conn.commit()
        return int(row[0])


async def get_location(loc_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM locations WHERE id=?", (loc_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def list_levels() -> list[str]:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT value FROM dict_levels ORDER BY is_builtin DESC, created_at ASC"
        )
        return [r[0] for r in await cur.fetchall()]


async def add_level(value: str) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO dict_levels(value, is_builtin, created_at) "
            "VALUES (?, 0, ?)",
            (value.strip(), _now()),
        )
        await conn.commit()


async def list_formats() -> list[str]:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT value FROM dict_formats ORDER BY is_builtin DESC, created_at ASC"
        )
        return [r[0] for r in await cur.fetchall()]


async def add_format(value: str) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO dict_formats(value, is_builtin, created_at) "
            "VALUES (?, 0, ?)",
            (value.strip(), _now()),
        )
        await conn.commit()


# ============================================================
#  registrations — записи-пары (Этап 1.2+)
# ============================================================

# Статусы, при которых запись «живая» (человек участвует или в листе ожидания).
# Лист ожидания — флаг is_waitlist, а не отдельный статус.
ACTIVE_REG_STATUSES = ("looking", "pending_confirm", "active")
# Статусы, занимающие слот основного состава (для подсчёта лимита).
SLOT_STATUSES = ("looking", "pending_confirm", "active")


async def _tournament_max_pairs(conn, tid: str) -> int | None:
    cur = await conn.execute("SELECT max_pairs FROM tournaments WHERE id=?", (tid,))
    row = await cur.fetchone()
    return row[0] if row else None


async def _count_main_slots(conn, tid: str) -> int:
    placeholders = ",".join("?" * len(SLOT_STATUSES))
    cur = await conn.execute(
        f"SELECT COUNT(*) FROM registrations "
        f"WHERE tournament_id=? AND is_waitlist=0 AND status IN ({placeholders})",
        (tid, *SLOT_STATUSES),
    )
    return (await cur.fetchone())[0]


async def get_user_registration_in(tid: str, user_id: int) -> dict | None:
    """Живая запись, где user участвует как игрок ИЛИ как партнёр."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        placeholders = ",".join("?" * len(ACTIVE_REG_STATUSES))
        cur = await conn.execute(
            f"""
            SELECT * FROM registrations
            WHERE tournament_id=?
              AND status IN ({placeholders})
              AND (player_user_id=? OR partner_user_id=?)
            ORDER BY id ASC LIMIT 1
            """,
            (tid, *ACTIVE_REG_STATUSES, user_id, user_id),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def create_registration(
    tid: str,
    player_user_id: int,
    player_name: str,
    player_username: str | None,
    partner_username: str | None = None,
    looking: bool = False,
) -> dict:
    """Создаёт запись-пару. Если основной состав заполнен (max_pairs) —
    ставит в лист ожидания (is_waitlist=1). Атомарно.

    looking=True → слот «ищет партнёра» (status='looking').
    иначе        → партнёр по нику, status='pending_confirm' (привязка — 1.3).

    Возвращает {'id': int, 'waitlisted': bool}.
    """
    now = _now()
    if looking:
        status, is_looking, partner_username = "looking", 1, None
    else:
        status, is_looking = "pending_confirm", 0
        partner_username = (partner_username or "").lstrip("@").strip() or None

    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("BEGIN IMMEDIATE")
        try:
            max_pairs = await _tournament_max_pairs(conn, tid)
            live = await _count_main_slots(conn, tid)
            is_waitlist = 1 if (max_pairs is not None and live >= max_pairs) else 0

            cur = await conn.execute(
                """
                INSERT INTO registrations
                    (tournament_id, player_user_id, player_name, player_username,
                     partner_username, is_looking_for_partner, is_waitlist, status,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING id
                """,
                (
                    tid, player_user_id, player_name, player_username,
                    partner_username, is_looking, is_waitlist, status, now, now,
                ),
            )
            rid = int((await cur.fetchone())[0])
            await conn.commit()
            return {"id": rid, "waitlisted": bool(is_waitlist)}
        except Exception:
            await conn.rollback()
            raise


async def get_registration(rid: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM registrations WHERE id=?", (rid,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def bind_partner(
    rid: int,
    partner_user_id: int,
    partner_name: str | None,
    partner_username: str | None,
) -> str:
    """Привязывает партнёра к записи (по подтверждению). Атомарно.
    Возвращает: 'ok' | 'stale' | 'self' | 'partner_busy'.
    На 'ok' статус записи становится 'active' (лимит/лист ожидания — шаг 1.5).
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("BEGIN IMMEDIATE")
        try:
            cur = await conn.execute("SELECT * FROM registrations WHERE id=?", (rid,))
            reg = await cur.fetchone()
            if not reg or reg["status"] != "pending_confirm":
                await conn.rollback()
                return "stale"
            if partner_user_id == reg["player_user_id"]:
                await conn.rollback()
                return "self"

            placeholders = ",".join("?" * len(ACTIVE_REG_STATUSES))
            cur = await conn.execute(
                f"""
                SELECT id FROM registrations
                WHERE tournament_id=? AND id<>?
                  AND status IN ({placeholders})
                  AND (player_user_id=? OR partner_user_id=?)
                LIMIT 1
                """,
                (reg["tournament_id"], rid, *ACTIVE_REG_STATUSES,
                 partner_user_id, partner_user_id),
            )
            if await cur.fetchone():
                await conn.rollback()
                return "partner_busy"

            await conn.execute(
                "UPDATE registrations SET partner_user_id=?, partner_name=?, "
                "partner_username=?, status='active', updated_at=? WHERE id=?",
                (partner_user_id, partner_name, partner_username, _now(), rid),
            )
            await conn.commit()
            return "ok"
        except Exception:
            await conn.rollback()
            raise


async def set_registration_status(rid: int, status: str) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE registrations SET status=?, updated_at=? WHERE id=?",
            (status, _now(), rid),
        )
        await conn.commit()


async def list_singles(tid: str, exclude_user_id: int | None = None) -> list[dict]:
    """Записи со слотом «ищет партнёра» (status='looking')."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM registrations "
            "WHERE tournament_id=? AND status='looking' "
            "  AND (? IS NULL OR player_user_id<>?) "
            "ORDER BY id ASC",
            (tid, exclude_user_id, exclude_user_id),
        )
        return [dict(r) for r in await cur.fetchall()]


# ============================================================
#  pair_requests — заявки «✋ Хочу в пару» к одиночкам (Этап 1.4)
# ============================================================

async def create_pair_request(
    tid: str,
    to_registration_id: int,
    from_user_id: int,
    from_name: str | None,
    from_username: str | None,
    expires_at: str | None,
) -> dict:
    """Создаёт заявку Б → одиночке А.
    Возвращает {'status': 'ok'|'exists'|'taken'|'self'|'busy', 'request_id'?}.
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("BEGIN IMMEDIATE")
        try:
            cur = await conn.execute(
                "SELECT * FROM registrations WHERE id=?", (to_registration_id,)
            )
            reg = await cur.fetchone()
            if not reg or reg["status"] != "looking":
                await conn.rollback()
                return {"status": "taken"}
            if from_user_id == reg["player_user_id"]:
                await conn.rollback()
                return {"status": "self"}

            placeholders = ",".join("?" * len(ACTIVE_REG_STATUSES))
            cur = await conn.execute(
                f"""
                SELECT id FROM registrations
                WHERE tournament_id=? AND status IN ({placeholders})
                  AND (player_user_id=? OR partner_user_id=?)
                LIMIT 1
                """,
                (tid, *ACTIVE_REG_STATUSES, from_user_id, from_user_id),
            )
            if await cur.fetchone():
                await conn.rollback()
                return {"status": "busy"}

            cur = await conn.execute(
                "SELECT id FROM pair_requests "
                "WHERE to_registration_id=? AND from_user_id=? AND status='pending'",
                (to_registration_id, from_user_id),
            )
            dup = await cur.fetchone()
            if dup:
                await conn.rollback()
                return {"status": "exists", "request_id": dup["id"]}

            cur = await conn.execute(
                """
                INSERT INTO pair_requests
                    (tournament_id, to_registration_id, from_user_id,
                     from_name, from_username, status, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                RETURNING id
                """,
                (tid, to_registration_id, from_user_id, from_name,
                 from_username, _now(), expires_at),
            )
            req_id = (await cur.fetchone())[0]
            await conn.commit()
            return {"status": "ok", "request_id": int(req_id)}
        except Exception:
            await conn.rollback()
            raise


async def get_pair_request(req_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM pair_requests WHERE id=?", (req_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def accept_pair_request(req_id: int) -> dict:
    """А принимает заявку Б. Атомарно: привязывает Б партнёром, закрывает
    остальные заявки к А (с возвратом их авторов для уведомления) и
    исходящие заявки Б к другим. Партнёр берётся из самой заявки.
    Возвращает {'status': 'ok'|'stale'|'taken'|'partner_busy', ...}.
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("BEGIN IMMEDIATE")
        try:
            cur = await conn.execute("SELECT * FROM pair_requests WHERE id=?", (req_id,))
            req = await cur.fetchone()
            if not req or req["status"] != "pending":
                await conn.rollback()
                return {"status": "stale"}

            cur = await conn.execute(
                "SELECT * FROM registrations WHERE id=?", (req["to_registration_id"],)
            )
            reg = await cur.fetchone()
            if not reg or reg["status"] != "looking":
                await conn.rollback()
                return {"status": "taken"}

            partner_id = req["from_user_id"]
            placeholders = ",".join("?" * len(ACTIVE_REG_STATUSES))
            cur = await conn.execute(
                f"""
                SELECT id FROM registrations
                WHERE tournament_id=? AND id<>?
                  AND status IN ({placeholders})
                  AND (player_user_id=? OR partner_user_id=?)
                LIMIT 1
                """,
                (reg["tournament_id"], reg["id"], *ACTIVE_REG_STATUSES,
                 partner_id, partner_id),
            )
            if await cur.fetchone():
                await conn.rollback()
                return {"status": "partner_busy"}

            now = _now()
            await conn.execute(
                "UPDATE registrations SET partner_user_id=?, partner_name=?, "
                "partner_username=?, is_looking_for_partner=0, status='active', "
                "updated_at=? WHERE id=?",
                (partner_id, req["from_name"], req["from_username"], now, reg["id"]),
            )
            await conn.execute(
                "UPDATE pair_requests SET status='accepted', resolved_at=? WHERE id=?",
                (now, req_id),
            )

            # остальные заявки к этому одиночке → отклоняем, авторов уведомим
            cur = await conn.execute(
                "SELECT from_user_id FROM pair_requests "
                "WHERE to_registration_id=? AND status='pending' AND id<>?",
                (reg["id"], req_id),
            )
            notify_rejected = [r["from_user_id"] for r in await cur.fetchall()]
            await conn.execute(
                "UPDATE pair_requests SET status='cancelled', resolved_at=? "
                "WHERE to_registration_id=? AND status='pending' AND id<>?",
                (now, reg["id"], req_id),
            )
            # исходящие заявки нового партнёра к другим — тихо закрываем
            await conn.execute(
                "UPDATE pair_requests SET status='cancelled', resolved_at=? "
                "WHERE from_user_id=? AND status='pending'",
                (now, partner_id),
            )
            await conn.commit()
            return {
                "status": "ok",
                "registration_id": reg["id"],
                "player_user_id": reg["player_user_id"],
                "partner_user_id": partner_id,
                "notify_rejected": notify_rejected,
            }
        except Exception:
            await conn.rollback()
            raise


# ============================================================
#  payments — оплата по каждому игроку пары (Этап 2)
# ============================================================

async def ensure_pair_payments(rid: int) -> list[dict]:
    """Создаёт платёжные строки для обоих игроков пары (если ещё нет).
    Возвращает [{who, user_id, ...}, ...]. Если пара неполная — []."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM registrations WHERE id=?", (rid,))
        reg = await cur.fetchone()
        if not reg or not reg["player_user_id"] or not reg["partner_user_id"]:
            return []
        members = [("player", reg["player_user_id"]), ("partner", reg["partner_user_id"])]
        now = _now()
        for who, uid in members:
            cur = await conn.execute(
                "SELECT id FROM payments WHERE registration_id=? AND who=?", (rid, who)
            )
            if not await cur.fetchone():
                await conn.execute(
                    "INSERT INTO payments (registration_id, who, user_id, status, "
                    "pays_for, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'unpaid', 'self', ?, ?)",
                    (rid, who, uid, now, now),
                )
        await conn.commit()
        cur = await conn.execute(
            "SELECT * FROM payments WHERE registration_id=? ORDER BY id", (rid,)
        )
        return [dict(r) for r in await cur.fetchall()]


async def get_pair_payments(rid: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM payments WHERE registration_id=? ORDER BY id", (rid,)
        )
        return [dict(r) for r in await cur.fetchall()]


async def get_payment_for_user(rid: int, user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM payments WHERE registration_id=? AND user_id=?",
            (rid, user_id),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def set_payment_pays_for(rid: int, user_id: int, pays_for: str) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE payments SET pays_for=?, updated_at=? "
            "WHERE registration_id=? AND user_id=?",
            (pays_for, _now(), rid, user_id),
        )
        await conn.commit()


# Статусы оплаты, в которых ещё ждём действия игрока/проверки.
PENDING_PAYMENT_STATUSES = ("unpaid", "rejected", "pending_review")


async def get_user_pending_payments(user_id: int) -> list[dict]:
    """Платёжные строки игрока, ожидающие оплаты/проверки, в активных турнирах."""
    pp = ",".join("?" * len(PENDING_PAYMENT_STATUSES))
    rs = ",".join("?" * len(ACTIVE_REG_STATUSES))
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            f"""
            SELECT p.*, r.tournament_id, t.title
            FROM payments p
            JOIN registrations r ON r.id = p.registration_id
            JOIN tournaments t ON t.id = r.tournament_id
            WHERE p.user_id=? AND p.status IN ({pp})
              AND r.status IN ({rs}) AND t.is_active=1
            ORDER BY COALESCE(t.start_at, t.date, ''), t.time
            """,
            (user_id, *PENDING_PAYMENT_STATUSES, *ACTIVE_REG_STATUSES),
        )
        return [dict(r) for r in await cur.fetchall()]


async def set_payment_screenshot(rid: int, user_id: int, file_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE payments SET status='pending_review', screenshot_file_id=?, "
            "updated_at=? WHERE registration_id=? AND user_id=?",
            (file_id, _now(), rid, user_id),
        )
        await conn.commit()


async def get_payment(payment_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM payments WHERE id=?", (payment_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def approve_payment(payment_id: int, mode: str, admin_id: int) -> dict | None:
    """Подтверждает оплату. mode='self' — только плательщик; mode='both' —
    закрывает и партнёрскую строку. Возвращает данные для уведомлений."""
    status = "paid"
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("BEGIN IMMEDIATE")
        try:
            cur = await conn.execute("SELECT * FROM payments WHERE id=?", (payment_id,))
            pay = await cur.fetchone()
            if not pay:
                await conn.rollback()
                return None
            now = _now()
            await conn.execute(
                "UPDATE payments SET status=?, pays_for=?, reviewed_by=?, "
                "reviewed_at=?, updated_at=? WHERE id=?",
                (status, mode, admin_id, now, now, payment_id),
            )
            notified = [pay["user_id"]]
            if mode == "both":
                cur = await conn.execute(
                    "SELECT * FROM payments WHERE registration_id=? AND id<>?",
                    (pay["registration_id"], payment_id),
                )
                other = await cur.fetchone()
                if other:
                    await conn.execute(
                        "UPDATE payments SET status=?, reviewed_by=?, reviewed_at=?, "
                        "updated_at=? WHERE id=?",
                        (status, admin_id, now, now, other["id"]),
                    )
                    if other["user_id"]:
                        notified.append(other["user_id"])
            await conn.commit()
            return {
                "registration_id": pay["registration_id"],
                "mode": mode,
                "notified_user_ids": notified,
            }
        except Exception:
            await conn.rollback()
            raise


async def confirm_payment_manual(rid: int, scope: str, admin_id: int) -> list[int]:
    """Ручное подтверждение без скрина. scope: 'both'|'player'|'partner'.
    Возвращает user_id, кого затронули (для уведомления)."""
    whos = ["player", "partner"] if scope == "both" else [scope]
    affected: list[int] = []
    now = _now()
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        for who in whos:
            cur = await conn.execute(
                "SELECT user_id FROM payments WHERE registration_id=? AND who=?",
                (rid, who),
            )
            row = await cur.fetchone()
            await conn.execute(
                "UPDATE payments SET status='paid_manual', reviewed_by=?, "
                "reviewed_at=?, updated_at=? WHERE registration_id=? AND who=?",
                (admin_id, now, now, rid, who),
            )
            if row and row["user_id"]:
                affected.append(row["user_id"])
        await conn.commit()
    return affected


async def create_manual_pair(
    tid: str,
    player_name: str,
    player_username: str | None,
    player_user_id: int | None,
    partner_name: str,
    partner_username: str | None,
    partner_user_id: int | None,
) -> int:
    """Админ добавляет готовую пару (нал/форс-мажор) — сразу в основной
    состав, обе оплаты paid_manual, без напоминаний. Возвращает rid."""
    now = _now()
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            """
            INSERT INTO registrations
                (tournament_id, player_user_id, player_name, player_username,
                 partner_user_id, partner_name, partner_username,
                 is_looking_for_partner, is_waitlist, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 'active', ?, ?)
            RETURNING id
            """,
            (tid, player_user_id, player_name, player_username,
             partner_user_id, partner_name, partner_username, now, now),
        )
        rid = int((await cur.fetchone())[0])
        for who, uid in [("player", player_user_id), ("partner", partner_user_id)]:
            await conn.execute(
                "INSERT INTO payments (registration_id, who, user_id, status, "
                "pays_for, reviewed_by, reviewed_at, created_at, updated_at) "
                "VALUES (?, ?, ?, 'paid_manual', 'self', NULL, ?, ?, ?)",
                (rid, who, uid, now, now, now),
            )
        await conn.commit()
        return rid


async def reject_payment(payment_id: int, admin_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM payments WHERE id=?", (payment_id,))
        pay = await cur.fetchone()
        if not pay:
            return None
        now = _now()
        await conn.execute(
            "UPDATE payments SET status='rejected', reviewed_by=?, reviewed_at=?, "
            "updated_at=? WHERE id=?",
            (admin_id, now, now, payment_id),
        )
        await conn.commit()
        return {"registration_id": pay["registration_id"], "user_id": pay["user_id"]}


async def get_main_registrations(tid: str) -> list[dict]:
    """Пары основного состава (не в листе ожидания)."""
    placeholders = ",".join("?" * len(SLOT_STATUSES))
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            f"SELECT * FROM registrations "
            f"WHERE tournament_id=? AND is_waitlist=0 AND status IN ({placeholders}) "
            f"ORDER BY id ASC",
            (tid, *SLOT_STATUSES),
        )
        return [dict(r) for r in await cur.fetchall()]


async def get_waitlist_registrations(tid: str) -> list[dict]:
    placeholders = ",".join("?" * len(SLOT_STATUSES))
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            f"SELECT * FROM registrations "
            f"WHERE tournament_id=? AND is_waitlist=1 AND status IN ({placeholders}) "
            f"ORDER BY id ASC",
            (tid, *SLOT_STATUSES),
        )
        return [dict(r) for r in await cur.fetchall()]


async def _promote_one_from_waitlist(conn, tid: str) -> dict | None:
    """Поднимает старейшую пару из листа ожидания, если есть свободный слот.
    Выполняется внутри уже открытой транзакции conn."""
    max_pairs = await _tournament_max_pairs(conn, tid)
    live = await _count_main_slots(conn, tid)
    if max_pairs is not None and live >= max_pairs:
        return None
    placeholders = ",".join("?" * len(SLOT_STATUSES))
    cur = await conn.execute(
        f"SELECT * FROM registrations "
        f"WHERE tournament_id=? AND is_waitlist=1 AND status IN ({placeholders}) "
        f"ORDER BY id ASC LIMIT 1",
        (tid, *SLOT_STATUSES),
    )
    wl = await cur.fetchone()
    if not wl:
        return None
    await conn.execute(
        "UPDATE registrations SET is_waitlist=0, updated_at=? WHERE id=?",
        (_now(), wl["id"]),
    )
    return dict(wl)


async def cancel_my_registration(tid: str, user_id: int) -> dict | None:
    """Игрок отменяет свою запись (как игрок ИЛИ как партнёр).
    Освобождает слот, поднимает пару из листа ожидания.
    Возвращает {'registration', 'other_user_id', 'promoted', 'was_waitlist'} или None.
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("BEGIN IMMEDIATE")
        try:
            placeholders = ",".join("?" * len(ACTIVE_REG_STATUSES))
            cur = await conn.execute(
                f"""
                SELECT * FROM registrations
                WHERE tournament_id=? AND status IN ({placeholders})
                  AND (player_user_id=? OR partner_user_id=?)
                ORDER BY id ASC LIMIT 1
                """,
                (tid, *ACTIVE_REG_STATUSES, user_id, user_id),
            )
            reg = await cur.fetchone()
            if not reg:
                await conn.rollback()
                return None

            was_waitlist = bool(reg["is_waitlist"])
            other = (
                reg["partner_user_id"]
                if user_id == reg["player_user_id"]
                else reg["player_user_id"]
            )
            await conn.execute(
                "UPDATE registrations SET status='cancelled', updated_at=? WHERE id=?",
                (_now(), reg["id"]),
            )
            promoted = None
            if not was_waitlist:
                promoted = await _promote_one_from_waitlist(conn, tid)
            await conn.commit()
            return {
                "registration": dict(reg),
                "other_user_id": other,
                "promoted": promoted,
                "was_waitlist": was_waitlist,
            }
        except Exception:
            await conn.rollback()
            raise


async def decline_pair_request(req_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM pair_requests WHERE id=? AND status='pending'", (req_id,)
        )
        req = await cur.fetchone()
        if not req:
            return None
        await conn.execute(
            "UPDATE pair_requests SET status='declined', resolved_at=? WHERE id=?",
            (_now(), req_id),
        )
        await conn.commit()
        return dict(req)


async def get_registrations(tid: str, statuses: tuple[str, ...] | None = None) -> list[dict]:
    statuses = statuses or ACTIVE_REG_STATUSES
    placeholders = ",".join("?" * len(statuses))
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            f"""
            SELECT * FROM registrations
            WHERE tournament_id=? AND status IN ({placeholders})
            ORDER BY COALESCE(slot_index, 1000000), id ASC
            """,
            (tid, *statuses),
        )
        return [dict(r) for r in await cur.fetchall()]


async def upsert_tournament(t: dict) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """
            INSERT INTO tournaments
                (id, title, date, time, location, price, description,
                 max_participants, group_chat_id, is_active, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'content')
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title,
                date=excluded.date,
                time=excluded.time,
                location=excluded.location,
                price=excluded.price,
                description=excluded.description,
                max_participants=excluded.max_participants,
                group_chat_id=COALESCE(excluded.group_chat_id, tournaments.group_chat_id),
                is_active=1,
                source='content'
            """,
            (
                t["id"],
                t["title"],
                t.get("date"),
                t.get("time"),
                t.get("location"),
                t.get("price"),
                t.get("description"),
                int(t.get("max_participants") or 8),
                t.get("group_chat_id"),
            ),
        )
        await conn.commit()


async def deactivate_missing(known_ids: list[str]) -> None:
    """Гасит только content-турниры, которых больше нет в content.md.
    Турниры из визарда (source='admin') не трогаем."""
    async with aiosqlite.connect(DB_PATH) as conn:
        if known_ids:
            placeholders = ",".join("?" * len(known_ids))
            await conn.execute(
                f"UPDATE tournaments SET is_active=0 "
                f"WHERE source='content' AND id NOT IN ({placeholders})",
                known_ids,
            )
        else:
            await conn.execute(
                "UPDATE tournaments SET is_active=0 WHERE source='content'"
            )
        await conn.commit()


async def insert_tournament(data: dict) -> str:
    """Создаёт турнир из визарда (source='admin'). Ожидает ключи:
    id, title, format_type, date, time_start, time_end, location_id,
    game_format, levels (JSON-строка), currency, price_amount, max_pairs,
    extra_note, start_at, payment_deadline, publish_at, status_v2.
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """
            INSERT INTO tournaments
                (id, title, format_type, date, time_start, time_end, location_id,
                 game_format, levels, currency, price_amount, max_pairs, extra_note,
                 start_at, payment_deadline, publish_at, status_v2, is_active, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'admin')
            """,
            (
                data["id"], data.get("title"), data.get("format_type"),
                data.get("date"), data.get("time_start"), data.get("time_end"),
                data.get("location_id"), data.get("game_format"),
                data.get("levels"), data.get("currency"), data.get("price_amount"),
                data.get("max_pairs"), data.get("extra_note"),
                data.get("start_at"), data.get("payment_deadline"),
                data.get("publish_at"), data.get("status_v2", "published"),
                1 if data.get("publish_at") is None else 1,
            ),
        )
        await conn.commit()
        return data["id"]


async def list_active_tournaments() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM tournaments WHERE is_active=1 "
            "AND (status_v2 IS NULL OR status_v2 NOT IN ('scheduled','finished','cancelled')) "
            "ORDER BY COALESCE(start_at, date, ''), COALESCE(time_start, time, '')"
        )
        return [dict(r) for r in await cur.fetchall()]


async def get_tournament(tid: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM tournaments WHERE id=?", (tid,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_user_live_registrations(user_id: int) -> list[dict]:
    """Живые записи пользователя (как игрок ИЛИ партнёр) + данные турнира.
    Для экрана «Мои записи»."""
    placeholders = ",".join("?" * len(ACTIVE_REG_STATUSES))
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            f"""
            SELECT r.id AS reg_id, r.status AS reg_status, r.is_waitlist,
                   r.is_looking_for_partner, r.player_user_id, r.partner_user_id,
                   t.id AS tid, t.title, t.date, t.time, t.start_at, t.is_active
            FROM registrations r
            JOIN tournaments t ON t.id = r.tournament_id
            WHERE r.status IN ({placeholders}) AND t.is_active=1
              AND (r.player_user_id=? OR r.partner_user_id=?)
            ORDER BY COALESCE(t.start_at, t.date, ''), t.time
            """,
            (*ACTIVE_REG_STATUSES, user_id, user_id),
        )
        return [dict(r) for r in await cur.fetchall()]


# ============================================================
#  Аналитика (Этап 4.3)
# ============================================================

async def top_participants(limit: int = 10) -> list[dict]:
    """Топ игроков по числу участий (как игрок ИЛИ партнёр, статус active)."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            """
            WITH parts AS (
                SELECT player_user_id AS uid, player_name AS nm, player_username AS un
                FROM registrations WHERE status='active' AND player_user_id IS NOT NULL
                UNION ALL
                SELECT partner_user_id AS uid, partner_name AS nm, partner_username AS un
                FROM registrations WHERE status='active' AND partner_user_id IS NOT NULL
            )
            SELECT uid, COUNT(*) AS cnt, MAX(nm) AS nm, MAX(un) AS un
            FROM parts GROUP BY uid ORDER BY cnt DESC, nm ASC LIMIT ?
            """,
            (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def tournament_financials(tid: str) -> dict:
    """Итоги по оплатам турнира: сколько оплачено, ожидается, сумма."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            """
            SELECT
                SUM(CASE WHEN p.status IN ('paid','paid_manual') THEN 1 ELSE 0 END) AS paid,
                COUNT(*) AS total
            FROM payments p
            JOIN registrations r ON r.id = p.registration_id
            WHERE r.tournament_id=? AND r.status='active'
            """,
            (tid,),
        )
        row = await cur.fetchone()
        paid = row["paid"] or 0
        total = row["total"] or 0
        cur = await conn.execute(
            "SELECT price_amount, currency FROM tournaments WHERE id=?", (tid,)
        )
        t = await cur.fetchone()
        price = (t["price_amount"] or 0) if t else 0
        currency = (t["currency"] or "") if t else ""
        return {
            "paid": paid,
            "total": total,
            "amount": paid * price,
            "currency": currency,
        }


# ============================================================
#  Планировщик (Этап 3) — все сравнения по ISO-строкам WITA (+08:00)
# ============================================================

async def list_due_scheduled(now_iso: str) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM tournaments WHERE is_active=1 AND status_v2='scheduled' "
            "AND publish_at IS NOT NULL AND publish_at<=?",
            (now_iso,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def list_published_with_announce() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM tournaments WHERE is_active=1 AND status_v2='published' "
            "AND announce_message_id IS NOT NULL"
        )
        return [dict(r) for r in await cur.fetchall()]


async def list_finished_due(grace_iso: str) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM tournaments WHERE is_active=1 AND status_v2='published' "
            "AND start_at IS NOT NULL AND start_at<?",
            (grace_iso,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def set_pinned(tid: str, pinned: bool) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE tournaments SET pinned=? WHERE id=?", (1 if pinned else 0, tid)
        )
        await conn.commit()


async def set_tournament_status_v2(tid: str, status: str) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE tournaments SET status_v2=? WHERE id=?", (status, tid)
        )
        await conn.commit()


async def set_announce_message(tid: str, chat_id: int, message_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE tournaments SET announce_chat_id=?, announce_message_id=? WHERE id=?",
            (chat_id, message_id, tid),
        )
        await conn.commit()
