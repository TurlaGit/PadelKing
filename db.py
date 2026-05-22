from datetime import datetime, timezone

import aiosqlite

from config import DB_PATH

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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.executescript(SCHEMA)
        await conn.commit()


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
