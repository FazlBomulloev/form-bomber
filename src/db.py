import json

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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS queue (
                id                TEXT PRIMARY KEY,
                name              TEXT,
                status            TEXT DEFAULT 'pending',
                urls              TEXT DEFAULT '[]',
                claude_key        TEXT DEFAULT '',
                rucaptcha_key     TEXT DEFAULT '',
                max_attempts      INTEGER DEFAULT 3,
                total_clients     INTEGER DEFAULT 0,
                done_clients      INTEGER DEFAULT 0,
                current_client_idx INTEGER DEFAULT 0,
                created_at        TEXT DEFAULT
                    (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS form_profiles (
                domain           TEXT PRIMARY KEY,
                form_selector    TEXT DEFAULT '',
                submit_selector  TEXT DEFAULT '',
                actions_json     TEXT DEFAULT '[]',
                has_captcha      INTEGER DEFAULT 0,
                captcha_type     TEXT DEFAULT '',
                success_method   TEXT DEFAULT '',
                success_signal   TEXT DEFAULT '',
                success_match    TEXT DEFAULT '',
                success_count    INTEGER DEFAULT 0,
                fail_count       INTEGER DEFAULT 0,
                last_success_at  TEXT,
                last_failed_at   TEXT,
                created_at       TEXT DEFAULT
                    (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS queue_clients (
                id         INTEGER PRIMARY KEY
                           AUTOINCREMENT,
                queue_id   TEXT NOT NULL,
                position   INTEGER NOT NULL,
                phone      TEXT NOT NULL,
                firstname  TEXT DEFAULT '',
                lastname   TEXT DEFAULT '',
                patronymic TEXT DEFAULT '',
                email      TEXT DEFAULT '',
                comment    TEXT DEFAULT '',
                proxy      TEXT DEFAULT '',
                status     TEXT DEFAULT 'pending',
                session_id TEXT DEFAULT NULL,
                created_at TEXT DEFAULT
                    (datetime('now','localtime'))
            )
        """)
        for col, default in [
            ("reason_code", "TEXT DEFAULT ''"),
            ("attempt_no", "INTEGER DEFAULT 1"),
            ("queue_id", "TEXT DEFAULT ''"),
            ("client_id", "INTEGER DEFAULT 0"),
        ]:
            try:
                await db.execute(
                    f"ALTER TABLE {'results' if col in ('reason_code', 'attempt_no') else 'sessions'} "
                    f"ADD COLUMN {col} {default}"
                )
            except Exception:
                pass
        await db.commit()


async def db_create_session(
    sid: str, name: str, total: int,
    queue_id: str = "", client_id: int = 0,
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO sessions"
            "(id,name,total,status,queue_id,client_id) "
            "VALUES(?,?,?,'running',?,?)",
            (sid, name, total, queue_id, client_id),
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


# ── Queue ──────────────────────────────────────


async def db_create_queue(
    qid: str, name: str, urls: list,
    claude_key: str, rucaptcha_key: str,
    max_attempts: int, total_clients: int,
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO queue"
            "(id,name,urls,claude_key,rucaptcha_key,"
            "max_attempts,total_clients) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                qid, name,
                json.dumps(urls, ensure_ascii=False),
                claude_key, rucaptcha_key,
                max_attempts, total_clients,
            ),
        )
        await db.commit()


async def db_add_queue_client(
    queue_id: str, position: int,
    phone: str, firstname: str, lastname: str,
    patronymic: str, email: str, comment: str,
    proxy: str,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO queue_clients"
            "(queue_id,position,phone,firstname,"
            "lastname,patronymic,email,comment,proxy) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                queue_id, position, phone,
                firstname, lastname, patronymic,
                email, comment, proxy,
            ),
        )
        await db.commit()
        return cur.lastrowid


async def db_get_queue(qid: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM queue WHERE id=?", (qid,),
        ) as c:
            row = await c.fetchone()
            return dict(row) if row else None


async def db_get_queue_clients(qid: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM queue_clients "
            "WHERE queue_id=? ORDER BY position",
            (qid,),
        ) as c:
            return [dict(r) for r in await c.fetchall()]


async def db_update_queue_status(qid: str, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE queue SET status=? WHERE id=?",
            (status, qid),
        )
        await db.commit()


async def db_update_queue_progress(
    qid: str, current_idx: int, done: int,
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE queue SET current_client_idx=?,"
            "done_clients=? WHERE id=?",
            (current_idx, done, qid),
        )
        await db.commit()


async def db_update_client_status(
    client_id: int, status: str,
    session_id: str = None,
):
    async with aiosqlite.connect(DB_PATH) as db:
        if session_id is not None:
            await db.execute(
                "UPDATE queue_clients "
                "SET status=?,session_id=? WHERE id=?",
                (status, session_id, client_id),
            )
        else:
            await db.execute(
                "UPDATE queue_clients "
                "SET status=? WHERE id=?",
                (status, client_id),
            )
        await db.commit()


async def db_get_active_queue():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM queue "
            "WHERE status IN ('running','paused_hours') "
            "ORDER BY created_at DESC LIMIT 1"
        ) as c:
            row = await c.fetchone()
            return dict(row) if row else None


async def db_get_last_queue():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM queue "
            "ORDER BY created_at DESC LIMIT 1"
        ) as c:
            row = await c.fetchone()
            return dict(row) if row else None


async def db_recover_stale_queues():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE queue SET status='done' "
            "WHERE status IN ('running','paused_hours')"
        )
        await db.execute(
            "UPDATE queue_clients SET status='done' "
            "WHERE status IN ('running','paused_hours')"
        )
        await db.commit()


# ── Form profiles cache ────────────────────────


async def db_get_form_profile(domain: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM form_profiles WHERE domain=?",
            (domain,),
        ) as c:
            row = await c.fetchone()
            return dict(row) if row else None


async def db_save_form_profile(
    domain: str,
    form_selector: str,
    submit_selector: str,
    actions: list,
    has_captcha: bool,
    captcha_type: str,
    success_method: str,
    success_signal: str,
    success_match: str,
):
    actions_json = json.dumps(
        actions, ensure_ascii=False,
    )
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO form_profiles ("
            "domain,form_selector,submit_selector,"
            "actions_json,has_captcha,captcha_type,"
            "success_method,success_signal,"
            "success_match,success_count,fail_count,"
            "last_success_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,1,0,"
            "datetime('now','localtime')) "
            "ON CONFLICT(domain) DO UPDATE SET "
            "form_selector=excluded.form_selector,"
            "submit_selector=excluded.submit_selector,"
            "actions_json=excluded.actions_json,"
            "has_captcha=excluded.has_captcha,"
            "captcha_type=excluded.captcha_type,"
            "success_method=excluded.success_method,"
            "success_signal=excluded.success_signal,"
            "success_match=excluded.success_match,"
            "success_count=success_count+1,"
            "fail_count=0,"
            "last_success_at="
            "datetime('now','localtime')",
            (
                domain, form_selector or "",
                submit_selector or "", actions_json,
                1 if has_captcha else 0,
                captcha_type or "",
                success_method or "",
                success_signal or "",
                (success_match or "")[:200],
            ),
        )
        await db.commit()


async def db_increment_profile_fail(domain: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE form_profiles SET "
            "fail_count=fail_count+1,"
            "last_failed_at="
            "datetime('now','localtime') "
            "WHERE domain=?",
            (domain,),
        )
        await db.commit()
        async with db.execute(
            "SELECT fail_count FROM form_profiles "
            "WHERE domain=?",
            (domain,),
        ) as c:
            row = await c.fetchone()
            return row[0] if row else 0


async def db_delete_form_profile(domain: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM form_profiles WHERE domain=?",
            (domain,),
        )
        await db.commit()
