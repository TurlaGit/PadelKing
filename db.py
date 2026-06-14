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
    -- looking | pending_confirm | active | waitlist | cancelled | removed_unpaid
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

# Статусы, при которых запись считается «живой» (занимает человека).
ACTIVE_REG_STATUSES = ("looking", "pending_confirm", "active", "waitlist")


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
) -> int:
    """Создаёт запись-пару.
    looking=True  → слот «ищет партнёра» (status='looking').
    иначе         → партнёр указан ником, ждём привязки/подтверждения
                    (status='pending_confirm'). Привязку user_id и
                    уведомления делает шаг 1.3.
    Возвращает id новой записи.
    """
    now = _now()
    if looking:
        status, is_looking, partner_username = "looking", 1, None
    else:
        status, is_looking = "pending_confirm", 0
        partner_username = (partner_username or "").lstrip("@").strip() or None

    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            """
            INSERT INTO registrations
                (tournament_id, player_user_id, player_name, player_username,
                 partner_username, is_looking_for_partner, status,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                tid, player_user_id, player_name, player_username,
                partner_username, is_looking, status, now, now,
            ),
        )
        rid = (await cur.fetchone())[0]
        await conn.commit()
        return int(rid)


async def get_registration(rid: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM registrations WHERE id=?", (rid,))
        row = await cur.fetchone()
        return dict(row) if row else None


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
                 max_participants, group_chat_id, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title,
                date=excluded.date,
                time=excluded.time,
                location=excluded.location,
                price=excluded.price,
                description=excluded.description,
                max_participants=excluded.max_participants,
                group_chat_id=COALESCE(excluded.group_chat_id, tournaments.group_chat_id),
                is_active=1
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
    if not known_ids:
        return
    placeholders = ",".join("?" * len(known_ids))
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            f"UPDATE tournaments SET is_active=0 WHERE id NOT IN ({placeholders})",
            known_ids,
        )
        await conn.commit()


async def list_active_tournaments() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM tournaments WHERE is_active=1 "
            "ORDER BY COALESCE(date,''), COALESCE(time,'')"
        )
        return [dict(r) for r in await cur.fetchall()]


async def get_tournament(tid: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM tournaments WHERE id=?", (tid,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_participants(tid: str) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            """
            SELECT * FROM participants
            WHERE tournament_id=? AND status IN ('active','waitlist')
            ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, id ASC
            """,
            (tid,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def get_user_registrations(user_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            """
            SELECT t.*, p.status AS my_status
            FROM participants p
            JOIN tournaments t ON t.id = p.tournament_id
            WHERE p.user_id=? AND p.status IN ('active','waitlist') AND t.is_active=1
            ORDER BY COALESCE(t.date,''), COALESCE(t.time,'')
            """,
            (user_id,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def register_user(
    tid: str, user_id: int, username: str | None, full_name: str
) -> dict:
    """Возвращает {'status': 'active'|'waitlist'|'already'|'no_tournament'}."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("BEGIN IMMEDIATE")
        try:
            cur = await conn.execute(
                "SELECT max_participants FROM tournaments WHERE id=? AND is_active=1",
                (tid,),
            )
            t = await cur.fetchone()
            if not t:
                await conn.rollback()
                return {"status": "no_tournament"}
            cap = int(t["max_participants"])

            cur = await conn.execute(
                "SELECT id, status FROM participants WHERE tournament_id=? AND user_id=?",
                (tid, user_id),
            )
            existing = await cur.fetchone()
            if existing and existing["status"] in ("active", "waitlist"):
                await conn.rollback()
                return {"status": "already"}

            cur = await conn.execute(
                "SELECT COUNT(*) AS c FROM participants "
                "WHERE tournament_id=? AND status='active'",
                (tid,),
            )
            active_count = (await cur.fetchone())["c"]
            new_status = "active" if active_count < cap else "waitlist"

            if existing:
                await conn.execute(
                    "UPDATE participants SET status=?, username=?, full_name=?, joined_at=? "
                    "WHERE id=?",
                    (new_status, username, full_name, _now(), existing["id"]),
                )
            else:
                await conn.execute(
                    "INSERT INTO participants "
                    "(tournament_id, user_id, username, full_name, status, joined_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (tid, user_id, username, full_name, new_status, _now()),
                )
            await conn.commit()
            return {"status": new_status}
        except Exception:
            await conn.rollback()
            raise


async def cancel_registration(tid: str, user_id: int) -> dict | None:
    """Снимает запись. Если освободилось основное место — двигает первого
    из листа ожидания. Возвращает promoted-участника или None."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("BEGIN IMMEDIATE")
        try:
            cur = await conn.execute(
                "SELECT id, status FROM participants WHERE tournament_id=? AND user_id=?",
                (tid, user_id),
            )
            row = await cur.fetchone()
            if not row or row["status"] not in ("active", "waitlist"):
                await conn.rollback()
                return None
            was_active = row["status"] == "active"
            await conn.execute(
                "UPDATE participants SET status='cancelled' WHERE id=?", (row["id"],)
            )

            promoted = None
            if was_active:
                cur = await conn.execute(
                    "SELECT * FROM participants "
                    "WHERE tournament_id=? AND status='waitlist' "
                    "ORDER BY id ASC LIMIT 1",
                    (tid,),
                )
                wl = await cur.fetchone()
                if wl:
                    await conn.execute(
                        "UPDATE participants SET status='active' WHERE id=?",
                        (wl["id"],),
                    )
                    promoted = dict(wl)
            await conn.commit()
            return promoted
        except Exception:
            await conn.rollback()
            raise


async def set_announce_message(tid: str, chat_id: int, message_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE tournaments SET announce_chat_id=?, announce_message_id=? WHERE id=?",
            (chat_id, message_id, tid),
        )
        await conn.commit()
