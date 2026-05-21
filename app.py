import csv
import io
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import PORT
from db import (
    db_init, db_recover_stale,
    db_get_sessions, db_get_results,
)
from models import profiles_load, profiles_save
from runner import run_session, _ws_clients


@asynccontextmanager
async def lifespan(app):
    await db_init()
    await db_recover_stale()
    yield


app = FastAPI(lifespan=lifespan)
app.mount(
    "/static",
    StaticFiles(directory="static"),
    name="static",
)


class StartRequest(BaseModel):
    urls: list[str]
    phone: str
    firstname: str = ""
    lastname: str = ""
    patronymic: str = ""
    email: str = ""
    comment: str = ""
    claude_key: str = ""
    rucaptcha_key: str = ""
    session_name: str = "Проверка"
    max_attempts: int = 3


@app.get("/")
async def index():
    return FileResponse("static/checker_ai.html")


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


@app.get("/api/profiles")
async def api_profiles():
    return profiles_load()


@app.get("/api/profiles/count")
async def api_profiles_count():
    p = profiles_load()
    return {"count": len(p)}


@app.delete("/api/profiles/{domain:path}")
async def api_profile_delete(domain: str):
    profiles = profiles_load()
    if domain in profiles:
        del profiles[domain]
        profiles_save(profiles)
        return {"deleted": domain}
    return {"error": "не найден"}


@app.delete("/api/profiles")
async def api_profiles_clear():
    profiles_save({})
    return {"cleared": True}


@app.websocket("/ws/{sid}")
async def ws_endpoint(ws: WebSocket, sid: str):
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
