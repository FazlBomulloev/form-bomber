import asyncio
import csv
import io
import re
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

_SRC = Path(__file__).resolve().parent

from auth import check_credentials, create_token, verify_token
from config import PORT
from db import (
    db_init, db_recover_stale, db_recover_stale_queues,
    db_get_sessions, db_get_results,
    db_get_active_queue, db_get_last_queue, db_get_queue_clients,
)
import runner
from runner import (
    run_session, run_queue, force_stop_all,
    _ws_clients, _logs_ttl_loop, LOG_DIR,
)


@asynccontextmanager
async def lifespan(app):
    await db_init()
    await db_recover_stale()
    await db_recover_stale_queues()
    ttl_task = asyncio.create_task(_logs_ttl_loop())
    try:
        yield
    finally:
        ttl_task.cancel()
        try:
            await ttl_task
        except (asyncio.CancelledError, Exception):
            pass


app = FastAPI(lifespan=lifespan)
app.mount(
    "/static",
    StaticFiles(directory=str(_SRC / "static")),
    name="static",
)

_PUBLIC_PATHS = {"/login", "/api/login", "/static"}


def _get_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return request.cookies.get("token")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if any(path == p or path.startswith(p + "/") for p in _PUBLIC_PATHS):
        return await call_next(request)

    token = _get_token(request)
    if not token or not verify_token(token):
        if path.startswith("/api/"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return RedirectResponse("/login", status_code=302)

    return await call_next(request)


class LoginRequest(BaseModel):
    login: str
    password: str


class StartRequest(BaseModel):
    urls: list[str]
    phone: str
    firstname: str = ""
    lastname: str = ""
    patronymic: str = ""
    email: str = ""
    comment: str = ""
    claude_key: str = ""
    deepseek_key: str = ""
    ai_provider: str = "deepseek"
    rucaptcha_key: str = ""
    session_name: str = "Проверка"
    max_attempts: int = 3


class ClientData(BaseModel):
    phone: str
    firstname: str = ""
    lastname: str = ""
    patronymic: str = ""
    email: str = ""
    comment: str = ""
    proxy: str = ""


class StartQueueRequest(BaseModel):
    urls: list[str]
    clients: list[ClientData]
    claude_key: str = ""
    deepseek_key: str = ""
    ai_provider: str = "deepseek"
    rucaptcha_key: str = ""
    queue_name: str = "Проверка"
    max_attempts: int = 3


@app.get("/login")
async def login_page():
    return FileResponse(str(_SRC / "static/login.html"))


@app.post("/api/login")
async def api_login(req: LoginRequest):
    if not check_credentials(req.login, req.password):
        return JSONResponse({"error": "Неверный логин или пароль"}, status_code=401)
    token = create_token(req.login)
    resp = JSONResponse({"ok": True, "token": token})
    resp.set_cookie("token", token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 7)
    return resp


@app.get("/")
async def index():
    return FileResponse(str(_SRC / "static/checker_ai.html"))


@app.post("/api/start")
async def api_start(req: StartRequest):
    if not req.urls:
        return {"error": "urls пустой"}
    if not req.phone:
        return {"error": "phone не указан"}

    sid = await run_session(
        req.urls, req.phone,
        req.firstname, req.lastname,
        req.patronymic,
        req.email, req.comment,
        req.claude_key,
        req.rucaptcha_key,
        req.session_name,
        max_attempts=req.max_attempts,
        deepseek_key=req.deepseek_key,
        ai_provider=req.ai_provider,
    )
    return {
        "session_id": sid,
        "total": len(req.urls),
    }


@app.get("/api/sessions")
async def api_sessions():
    return await db_get_sessions()


@app.get("/api/sessions/{sid}/results")
async def api_session_results(sid: str):
    return await db_get_results(sid)


@app.get("/api/sessions/{sid}/export")
async def api_session_export(sid: str):
    rows = await db_get_results(sid)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "url", "status", "method",
        "message", "tokens", "attempt",
    ])
    for r in rows:
        w.writerow([
            r["url"], r["status"], r["method"],
            r["message"], r["tokens_used"],
            r["attempt_no"],
        ])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                f"attachment; "
                f"filename=results_{sid}.csv"
            ),
        },
    )


@app.post("/api/queue/start")
async def api_queue_start(req: StartQueueRequest):
    if not req.urls:
        return {"error": "urls пустой"}
    if not req.clients:
        return {"error": "clients пустой"}
    for i, c in enumerate(req.clients):
        if not c.phone:
            return {
                "error": f"Клиент #{i+1}: "
                "phone не указан"
            }
    try:
        qid = await run_queue(
            req.urls,
            [c.model_dump() for c in req.clients],
            req.claude_key, req.rucaptcha_key,
            req.queue_name, req.max_attempts,
            deepseek_key=req.deepseek_key,
            ai_provider=req.ai_provider,
        )
    except ValueError as e:
        return {"error": str(e)}
    return {
        "queue_id": qid,
        "total_clients": len(req.clients),
        "total_urls": len(req.urls),
    }


@app.get("/api/queue/active")
async def api_active_queue():
    queue = await db_get_active_queue()
    if not queue:
        return {"active": False}
    clients = await db_get_queue_clients(queue["id"])
    client_results = {}
    for c in clients:
        if c.get("session_id"):
            results = await db_get_results(
                c["session_id"],
            )
            client_results[c["id"]] = results
    return {
        "active": True,
        "queue": queue,
        "clients": clients,
        "results": client_results,
    }


@app.get("/api/queue/last")
async def api_last_queue():
    queue = await db_get_last_queue()
    if not queue:
        return {"found": False}
    clients = await db_get_queue_clients(queue["id"])
    client_results = {}
    for c in clients:
        if c.get("session_id"):
            results = await db_get_results(c["session_id"])
            client_results[c["id"]] = results
    return {
        "found": True,
        "queue": queue,
        "clients": clients,
        "results": client_results,
    }


@app.post("/api/queue/stop")
async def api_queue_stop():
    qid = runner._active_queue_id
    if not qid:
        return {"error": "Нет активной очереди"}
    await force_stop_all()
    return {"ok": True, "queue_id": qid}


@app.get("/api/logs/download-all")
async def api_logs_download_all():
    buf = io.BytesIO()
    with zipfile.ZipFile(
        buf, "w", zipfile.ZIP_DEFLATED,
    ) as zf:
        if LOG_DIR.exists():
            for log_file in LOG_DIR.rglob("run.log"):
                arcname = log_file.relative_to(
                    LOG_DIR,
                ).as_posix()
                zf.write(log_file, arcname)
    buf.seek(0)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; '
                f'filename="logs_{ts}.zip"'
            ),
        },
    )


def _safe_domain(domain: str) -> str:
    return re.sub(r'[^a-zA-Z0-9._-]', '_', domain)


@app.get("/api/sessions/{sid}/logs")
async def api_session_logs(sid: str):
    sid_dir = LOG_DIR / sid
    if not sid_dir.exists():
        return []
    items = []
    for d in sorted(sid_dir.iterdir()):
        if not d.is_dir():
            continue
        run_log = d / "run.log"
        if not run_log.exists():
            continue
        st = run_log.stat()
        items.append({
            "domain": d.name,
            "size": st.st_size,
            "mtime": st.st_mtime,
        })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


@app.get("/api/sessions/{sid}/logs/{domain}")
async def api_session_log(sid: str, domain: str):
    safe = _safe_domain(domain)
    log_file = LOG_DIR / sid / safe / "run.log"
    if not log_file.exists():
        return JSONResponse(
            {"error": "лог не найден"}, status_code=404,
        )
    return FileResponse(
        str(log_file),
        media_type="text/plain; charset=utf-8",
    )


@app.post("/api/clients/parse-csv")
async def api_parse_csv(file: UploadFile):
    content = await file.read()
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    clients = []
    for row in reader:
        clients.append({
            "phone": row.get("phone", ""),
            "firstname": row.get("firstname", ""),
            "lastname": row.get("lastname", ""),
            "patronymic": row.get("patronymic", ""),
            "email": row.get("email", ""),
            "comment": row.get("comment", ""),
            "proxy": row.get("proxy", ""),
        })
    return {"clients": clients}


@app.websocket("/ws/queue/{qid}")
async def ws_queue_endpoint(ws: WebSocket, qid: str):
    token = (
        ws.query_params.get("token")
        or ws.cookies.get("token")
    )
    if not token or not verify_token(token):
        await ws.close(code=4001, reason="unauthorized")
        return
    await ws.accept()
    _ws_clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(ws)


@app.websocket("/ws/{sid}")
async def ws_endpoint(ws: WebSocket, sid: str):
    token = ws.query_params.get("token") or ws.cookies.get("token")
    if not token or not verify_token(token):
        await ws.close(code=4001, reason="unauthorized")
        return
    await ws.accept()
    _ws_clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(ws)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=PORT,
        reload=False,
    )
