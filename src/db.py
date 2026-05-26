import aiosqlite
from pathlib import Path
from config import DB_PATH


async def db_init():
    Path("data").mkdir(exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id         TEXT PRIMARY KEY,
                name       TEXT,
                status     TEXT DEFAULT 'running',
                total      INTEGER DEFAULT 0,
                success    INTEGER DEFAULT 0,
                failed     INTEGER DEFAULT 0,
                tokens     INTEGER DEFAULT 0,
                created_at TEXT DEFAULT
                    (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS results (
                id          INTEGER PRIMARY KEY
                            AUTOINCREMENT,
                session_id  TEXT,
                url         TEXT,
                status      TEXT,
                method      TEXT,
                message     TEXT,
                tokens_used INTEGER DEFAULT 0,
                profile_saved INTEGER DEFAULT 0,
                ai_notes    TEXT,
                reason_code TEXT DEFAULT '',
                attempt_no  INTEGER DEFAULT 1,
                created_at  TEXT DEFAULT
                    (datetime('now','localtime'))
            )
        """)
        try:
            await db.execute(
                "ALTER TABLE results "
                "ADD COLUMN reason_code "
                "TEXT DEFAULT ''"
            )
        except Exception:
            pass
        try:
            await db.execute(
                "ALTER TABLE results "
                "ADD COLUMN attempt_no "
                "INTEGER DEFAULT 1"
            )
        except Exception:
            pass
        await db.commit()


async def db_create_session(
    sid: str, name: str, total: int
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO sessions"
            "(id,name,total,status) "
            "VALUES(?,?,?,'running')",
            (sid, name, total),
        )
        await db.commit()


async def db_add_result(sid: str, url: str, res: dict):
    status = res["status"]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO results("
            "session_id,url,status,method,message,"
            "tokens_used,profile_saved,ai_notes,"
            "reason_code,attempt_no"
            ") VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                sid, url, status, res["method"],
                res["message"],
                res.get("tokens_used", 0),
                1 if res.get("profile_saved") else 0,
                (res.get("ai_instructions") or {}).get(
                    "notes", ""
                ),
                res.get("reason_code", ""),
                int(res.get("attempt_no", 1) or 1),
            ),
        )
        if status in ("success", "captcha"):
            await db.execute(
                "UPDATE sessions "
                "SET success=success+1, "
                "tokens=tokens+? WHERE id=?",
                (res.get("tokens_used", 0), sid),
            )
        else:
            await db.execute(
                "UPDATE sessions "
                "SET failed=failed+1, "
                "tokens=tokens+? WHERE id=?",
                (res.get("tokens_used", 0), sid),
            )
        await db.commit()


async def db_finish_session(sid: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE sessions SET status='done' "
            "WHERE id=?", (sid,),
        )
        await db.commit()


async def db_recover_stale():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE sessions SET status='done' "
            "WHERE status='running'"
        )
        await db.commit()


async def db_get_sessions():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM sessions "
            "ORDER BY created_at DESC LIMIT 100"
        ) as c:
            return [dict(r) for r in await c.fetchall()]


async def db_get_results(sid: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM results "
            "WHERE session_id=? "
            "ORDER BY created_at",
            (sid,),
        ) as c:
            return [dict(r) for r in await c.fetchall()]
