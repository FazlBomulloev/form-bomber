import asyncio
import json
import re
import shutil
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from config import (
    USER_AGENT, CONCURRENCY,
    AI_CONCURRENCY, COOKIE_CONSENT_SCRIPT,
    WORK_HOUR_START, WORK_HOUR_END, MSK_UTC_OFFSET,
)
from models import domain_from_url
from logger import SiteLogger, _site_logger_var
from db import (
    db_create_session, db_add_result,
    db_finish_session,
    db_create_queue, db_add_queue_client,
    db_add_queue_group, db_get_queue_groups,
    db_get_queue, db_get_queue_clients,
    db_update_queue_status, db_update_queue_progress,
    db_update_client_status,
    db_get_form_profile, db_save_form_profile,
    db_increment_profile_fail,
    db_delete_form_profile,
)
from ai_provider import (
    ask_ai_sync, collect_full_html,
)
from form_finder import extract_forms, build_smart_plan
from form_filler import (
    execute_action_plan, submit_with_retry,
    fill_all_empty_fields,
)
from captcha import handle_captcha, detect_captcha_overlay
from browser_utils import (
    dismiss_cookie_banners, suppress_widgets,
    has_calltouch, step_shot,
    apply_stealth, build_stealth_context_kwargs,
    install_resource_blocker,
)
from calltouch import try_calltouch

_ai_sem = asyncio.Semaphore(AI_CONCURRENCY)
_ws_clients: set = set()
_browsers: list = []
_browser_use_count: list = []
_pw = None
_browser_lock = asyncio.Lock()
BROWSER_RECYCLE_AFTER = 15
_active_queue_id: str | None = None
_queue_cancel = asyncio.Event()
_active_tasks: set = set()
_active_contexts: set = set()
_active_queue_task = None
LOG_DIR = Path("data/logs")

def _cleanup_data_except_db():
    data_dir = Path("data")
    if not data_dir.exists():
        return
    for item in data_dir.iterdir():
        try:
            if item.is_file() and item.suffix == ".db":
                continue
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink()
        except Exception:
            pass
    LOG_DIR.mkdir(parents=True, exist_ok=True)

async def force_stop_all():
    _queue_cancel.set()

    for t in list(_active_tasks):
        try:
            t.cancel()
        except Exception:
            pass

    global _active_queue_task
    if _active_queue_task and not _active_queue_task.done():
        try:
            _active_queue_task.cancel()
        except Exception:
            pass

    for ctx in list(_active_contexts):
        try:
            await ctx.close()
        except Exception:
            pass
    _active_contexts.clear()

_MSK = timezone(timedelta(hours=MSK_UTC_OFFSET))

PROFILE_TTL_DAYS = 90
PROFILE_FAIL_THRESHOLD = 2

def _detect_signal_from_match(match: str) -> str:
    m = (match or "")[:15].upper()
    if m.startswith("NET"):
        return "net"
    if m.startswith("XHR"):
        return "xhr"
    if "WPCF7" in m:
        return "dom_wpcf7"
    if "TILDA" in m:
        return "dom_tilda"
    return "dom"

def _is_profile_usable(profile: dict) -> bool:
    if not profile:
        return False
    if (
        profile.get("fail_count", 0)
        >= PROFILE_FAIL_THRESHOLD
    ):
        return False
    last_ok = profile.get("last_success_at")
    if not last_ok:
        return False
    try:
        dt = datetime.strptime(
            last_ok, "%Y-%m-%d %H:%M:%S",
        )
    except Exception:
        return False
    age_days = (datetime.now() - dt).days
    if age_days > PROFILE_TTL_DAYS:
        return False
    actions = profile.get("actions_json") or "[]"
    try:
        if not json.loads(actions):
            return False
    except Exception:
        return False
    return True

def _profile_to_instructions(profile: dict) -> dict:
    try:
        actions = json.loads(
            profile.get("actions_json") or "[]",
        )
    except Exception:
        actions = []
    return {
        "form_found": True,
        "form_selector": (
            profile.get("form_selector") or None
        ),
        "actions": actions,
        "has_captcha": bool(
            profile.get("has_captcha", 0),
        ),
        "captcha_type": (
            profile.get("captcha_type") or None
        ),
        "notes": "Из кеша профиля формы",
    }

_UNSTABLE_SEL_RE = re.compile(
    r"#(?:input_\d{10,}"
    r"|tildafield_[a-z0-9]{6,}"
    r"|tilda-[a-z0-9]{6,}"
    r"|rec\d{8,}"
    r"|el_\d{10,})",
    re.I,
)

def _has_unstable_selectors(instructions: dict) -> bool:
    for a in instructions.get("actions") or []:
        sel = a.get("selector") or ""
        if _UNSTABLE_SEL_RE.search(sel):
            return True
    fs = instructions.get("form_selector") or ""
    if _UNSTABLE_SEL_RE.search(fs):
        return True
    return False

async def _maybe_save_profile(
    domain: str, result: dict, instructions: dict,
):
    if result.get("status") != "success":
        return
    if not instructions or not instructions.get(
        "actions",
    ):
        return
    method = result.get("method", "")
    if not method.startswith("form_"):
        return
    if _has_unstable_selectors(instructions):
        log = _site_logger_var.get(None)
        if log:
            log.warn(
                f"profile_cache: пропуск {domain} — "
                f"нестабильные Tilda-селекторы",
            )
        return
    submit_sel = ""
    for a in instructions.get("actions") or []:
        if a.get("action") == "submit":
            submit_sel = a.get("selector", "")
            break
    log = _site_logger_var.get(None)
    try:
        await db_save_form_profile(
            domain=domain,
            form_selector=(
                instructions.get("form_selector") or ""
            ),
            submit_selector=submit_sel,
            actions=instructions.get("actions") or [],
            has_captcha=bool(
                instructions.get("has_captcha", False),
            ),
            captcha_type=(
                instructions.get("captcha_type") or ""
            ),
            success_method=method,
            success_signal=_detect_signal_from_match(
                result.get("message", ""),
            ),
            success_match=result.get("message", ""),
        )
        if log:
            log.ok(f"profile_cache: сохранён для {domain}")
    except Exception as e:
        if log:
            log.warn(
                f"profile_cache save error: "
                f"{str(e)[:100]}",
            )

def _is_working_hours() -> bool:
    now = datetime.now(_MSK)
    start = now.replace(
        hour=WORK_HOUR_START[0],
        minute=WORK_HOUR_START[1],
        second=0, microsecond=0,
    )
    end = now.replace(
        hour=WORK_HOUR_END[0],
        minute=WORK_HOUR_END[1],
        second=0, microsecond=0,
    )
    return start <= now < end

def _seconds_until_work_start() -> float:
    now = datetime.now(_MSK)
    next_start = now.replace(
        hour=WORK_HOUR_START[0],
        minute=WORK_HOUR_START[1],
        second=0, microsecond=0,
    )
    if now >= next_start:
        next_start += timedelta(days=1)
    return (next_start - now).total_seconds()

async def _wait_for_working_hours(queue_id: str):
    if _is_working_hours():
        return
    await db_update_queue_status(
        queue_id, "paused_hours",
    )
    wait_secs = _seconds_until_work_start()
    resume_at = (
        datetime.now(_MSK)
        + timedelta(seconds=wait_secs)
    ).strftime("%H:%M")
    await _ws_broadcast({
        "type": "paused",
        "queue_id": queue_id,
        "reason": "working_hours",
        "resume_at_msk": resume_at,
        "wait_seconds": int(wait_secs),
    })
    while not _is_working_hours():
        if _queue_cancel.is_set():
            return
        try:
            await asyncio.wait_for(
                _queue_cancel.wait(), timeout=30,
            )
            return
        except asyncio.TimeoutError:
            pass
    await db_update_queue_status(queue_id, "running")
    await _ws_broadcast({
        "type": "resumed",
        "queue_id": queue_id,
    })

def parse_proxy(raw: str) -> dict | None:
    if not raw or not raw.strip():
        return None
    raw = raw.strip()
    if raw.startswith(("http://", "https://", "socks5://")):
        parsed = urlparse(raw)
        result = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
        if parsed.username:
            result["username"] = parsed.username
        if parsed.password:
            result["password"] = parsed.password
        return result
    parts = raw.split(":")
    if len(parts) == 4:
        host, port, user, pwd = parts
        return {
            "server": f"http://{host}:{port}",
            "username": user,
            "password": pwd,
        }
    if len(parts) == 2:
        return {"server": f"http://{parts[0]}:{parts[1]}"}
    return None

async def _reset_browsers():
    global _browsers, _browser_use_count, _pw
    for br in list(_browsers):
        try:
            await br.close()
        except Exception:
            pass
    _browsers = []
    _browser_use_count = []
    try:
        if _pw:
            await _pw.stop()
    except Exception:
        pass
    _pw = None

async def _ensure_browsers(count: int = 2):
    global _browsers, _browser_use_count, _pw
    async with _browser_lock:
        alive = []
        alive_counts = []
        for i, br in enumerate(_browsers):
            try:
                br.contexts
                cnt = (
                    _browser_use_count[i]
                    if i < len(_browser_use_count) else 0
                )
                if cnt >= BROWSER_RECYCLE_AFTER:
                    try:
                        await br.close()
                    except Exception:
                        pass
                    continue
                alive.append(br)
                alive_counts.append(cnt)
            except Exception:
                pass
        _browsers = alive
        _browser_use_count = alive_counts
        if len(_browsers) >= count:
            return _browsers[:count]
        if _pw is None:
            _pw = await async_playwright().start()
        while len(_browsers) < count:
            br = await _pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            _browsers.append(br)
            _browser_use_count.append(0)
        return _browsers[:count]

async def _get_browser(idx: int = 0):
    browsers = await _ensure_browsers(max(1, idx + 1))
    real_idx = idx % len(browsers)
    if real_idx < len(_browser_use_count):
        _browser_use_count[real_idx] += 1
    return browsers[real_idx]

async def _recycle_browser_if_needed(idx: int = 0):
    global _browsers, _browser_use_count
    async with _browser_lock:
        if idx < 0 or idx >= len(_browsers):
            return
        cnt = (
            _browser_use_count[idx]
            if idx < len(_browser_use_count) else 0
        )
        if cnt < BROWSER_RECYCLE_AFTER:
            return
        br = _browsers[idx]
        try:
            await br.close()
        except Exception:
            pass
        _browsers.pop(idx)
        if idx < len(_browser_use_count):
            _browser_use_count.pop(idx)

async def _ws_broadcast(data: dict):
    msg = json.dumps(data, ensure_ascii=False)
    dead = set()
    for ws in _ws_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    _ws_clients.difference_update(dead)

async def _resolve_captcha_overlay(page, cap_type, url, rucaptcha_key):
    from captcha import (
        _try_click_smartcaptcha,
        _solve_smartcaptcha_overlay,
        _extract_smartcaptcha_sitekey,
        _solve_captcha,
        _inject_captcha_token,
        _detect_image_captcha,
    )
    if cap_type in ("yandex_smartcaptcha", "captcha_overlay"):
        try:
            if await _try_click_smartcaptcha(page):
                await asyncio.sleep(3)
                if not await detect_captcha_overlay(page):
                    return True
        except Exception:
            pass
        if rucaptcha_key:
            try:
                if await _solve_smartcaptcha_overlay(page, url, rucaptcha_key) == "ok":
                    return True
            except Exception:
                pass
            try:
                sk = await _extract_smartcaptcha_sitekey(page)
                if sk:
                    tok = await _solve_captcha("yandex", sk, url, rucaptcha_key)
                    if tok and await _inject_captcha_token(page, "yandex", tok):
                        return True
            except Exception:
                pass
    elif cap_type == "image_captcha" and rucaptcha_key:
        try:
            if await _detect_image_captcha(page, rucaptcha_key) == "ok":
                return True
        except Exception:
            pass
    return False

def _classify_reason(result: dict) -> str:
    status = result.get("status", "")
    msg = (result.get("message") or "").lower()
    if status == "success":
        return "ok"
    if "капча" in msg or "captcha" in msg:
        return "captcha"
    if "телефон" in msg or "phone" in msg:
        return "no_phone"
    if "форма не найдена" in msg:
        return "no_form"
    if "timeout" in msg or "таймаут" in msg:
        return "timeout"
    if "ошибка" in msg or "error" in msg:
        return "server_error"
    return "unknown"

async def _try_fill_and_submit(
    page, instructions, phone,
    firstname, lastname, patronymic,
    email, comment, rucaptcha_key, url,
    step_dir, method_name,
    context=None,
):
    ctx = context or page
    log = _site_logger_var.get(None)
    actions = instructions.get("actions", [])
    if not actions:
        return None, None

    if log:
        log.step("fill", f"actions={len(actions)}")
    fill_result = await execute_action_plan(
        ctx, actions, phone,
        firstname, lastname, patronymic,
        email, comment,
        form_selector=instructions.get(
            "form_selector"
        ),
        step_dir=step_dir,
        honeypots=instructions.get("honeypots"),
        csrf_token=instructions.get("csrf_token"),
    )

    form_el = fill_result["form_el"]
    await fill_all_empty_fields(
        ctx, phone,
        firstname, lastname, patronymic,
        email, comment, form_el,
        honeypots=fill_result.get("honeypots"),
    )

    if not fill_result["phone_ok"]:
        if log:
            log.err("fill", "телефон не заполнен")
        return {
            "status": "failed",
            "method": f"form_{method_name}",
            "message": "Не удалось заполнить телефон",
            "reason_code": "no_phone",
        }, fill_result

    captcha_result = await handle_captcha(
        page, url, rucaptcha_key,
        has_captcha_hint=instructions.get(
            "has_captcha", False
        ),
        captcha_type_hint=instructions.get(
            "captcha_type"
        ),
        captcha_hint=instructions.get("captcha_hint"),
    )
    if captcha_result == "no_key":
        return {
            "status": "captcha",
            "method": "captcha_found",
            "message": (
                "Капча найдена, "
                "ключ RuCaptcha не указан"
            ),
        }, fill_result
    if captcha_result == "solve_failed":
        return {
            "status": "captcha",
            "method": "captcha_found",
            "message": (
                "Капча найдена, "
                "не удалось решить"
            ),
        }, fill_result

    captcha_unresolved = (
        captcha_result == "inject_failed"
    )

    dom_result = await submit_with_retry(
        ctx,
        fill_result["submit_sel"],
        form_el,
        phone, firstname, lastname,
        patronymic, email, comment,
        step_dir=step_dir,
        max_submits=3,
        captcha_unresolved=captcha_unresolved,
        page_for_shot=page if context else None,
        rucaptcha_key=rucaptcha_key,
    )
    state = dom_result.get("state", "unchanged")

    if state == "success":
        return {
            "status": "success",
            "method": f"form_{method_name}",
            "message": (
                f"Успех: "
                f"{dom_result.get('match', '')}"
            ),
        }, fill_result
    if state == "validation_error":
        return {
            "status": "failed",
            "method": f"form_{method_name}",
            "message": (
                "Ошибка валидации: "
                + dom_result.get("match", "")
            ),
            "reason_code": "validation",
        }, fill_result
    if state == "error":
        return {
            "status": "failed",
            "method": f"form_{method_name}",
            "message": (
                "Ошибка: "
                + dom_result.get("match", "")
            ),
            "reason_code": "server_error",
        }, fill_result
    if state == "captcha_required":
        return {
            "status": "captcha",
            "method": f"form_{method_name}",
            "message": (
                "Сервер требует капчу: "
                + dom_result.get("match", "")
            ),
            "reason_code": "captcha",
        }, fill_result

    return None, fill_result

async def check_site_v2(
    url: str, phone: str,
    firstname: str = "", lastname: str = "",
    patronymic: str = "",
    email: str = "", comment: str = "",
    ai_key: str = "",
    rucaptcha_key: str = "",
    attempt_no: int = 1,
    max_retries: int = 6,
    prev_hint: dict = None,
    proxy: dict = None,
    session_id: str = "",
    ai_provider: str = "deepseek",
    browser_idx: int = 0,
):
    domain = domain_from_url(url)
    sid_log_dir = (
        LOG_DIR / session_id if session_id else LOG_DIR
    )
    sid_log_dir.mkdir(parents=True, exist_ok=True)
    _logger = SiteLogger(domain, url, sid_log_dir)
    _log_token = _site_logger_var.set(_logger)
    ctx = None

    result = {
        "url": url, "status": "failed",
        "method": "none", "message": "",
        "tokens_used": 0,
        "ai_instructions": None,
        "reason_code": "", "attempt_no": attempt_no,
    }

    try:
        _logger.step(
            "начало",
            f"попытка {attempt_no}/{max_retries}",
        )

        _logger.step("browser", "запуск")
        ctx_kwargs = {
            "user_agent": USER_AGENT,
            "viewport": {"width": 1280, "height": 900},
            "ignore_https_errors": True,
        }
        ctx_kwargs = build_stealth_context_kwargs(ctx_kwargs)
        if proxy:
            ctx_kwargs["proxy"] = proxy
        for _br_try in range(2):
            try:
                browser = await _get_browser(browser_idx)
                ctx = await browser.new_context(
                    **ctx_kwargs,
                )
                await apply_stealth(ctx)
                await install_resource_blocker(ctx)
                _active_contexts.add(ctx)
                page = await ctx.new_page()
                break
            except Exception:
                await _reset_browsers()
                if _br_try == 1:
                    raise
                _logger.warn("браузер упал, перезапуск")
                await asyncio.sleep(1)
        step_dir = _logger.site_dir

        try:
            _logger.step("navigate", url)
            _goto_try = 0
            while True:
                try:
                    await page.goto(
                        url, wait_until="domcontentloaded",
                        timeout=30000,
                    )
                    break
                except Exception as _ge:
                    _goto_try += 1
                    _msg = str(_ge)
                    _crashed = (
                        "Page crashed" in _msg
                        or "TargetClosed" in _msg
                        or "Target page, context or browser"
                        in _msg
                    )
                    if _crashed and _goto_try < 2:
                        _logger.warn(
                            "page crashed на goto, "
                            "пересоздаём контекст",
                        )
                        try:
                            await ctx.close()
                        except Exception:
                            pass
                        _active_contexts.discard(ctx)
                        browser = await _get_browser(browser_idx)
                        ctx = await browser.new_context(
                            **ctx_kwargs,
                        )
                        await apply_stealth(ctx)
                        await install_resource_blocker(ctx)
                        _active_contexts.add(ctx)
                        page = await ctx.new_page()
                        await asyncio.sleep(1)
                        continue
                    raise
            await asyncio.sleep(2)

            try:
                await page.evaluate(
                    COOKIE_CONSENT_SCRIPT
                )
            except Exception:
                pass
            await dismiss_cookie_banners(page)

            pre_cap = await detect_captcha_overlay(page)
            if pre_cap:
                _logger.warn(
                    f"captcha overlay при загрузке: {pre_cap}"
                )
                if await _resolve_captcha_overlay(
                    page, pre_cap, url, rucaptcha_key,
                ):
                    _logger.ok("captcha overlay решена")
                    await asyncio.sleep(2)
                    try:
                        await page.reload(
                            wait_until="domcontentloaded",
                            timeout=15000,
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(2)

            await step_shot(
                page, "01_loaded", step_dir
            )

            instructions = None
            instructions_used = None
            tokens = 0
            cache_tried = False

            if attempt_no == 1:
                cached = await db_get_form_profile(
                    domain,
                )
                if cached and _is_profile_usable(cached):
                    cache_tried = True
                    _logger.step(
                        "profile_cache",
                        f"hit (success={cached.get('success_count')} "
                        f"fail={cached.get('fail_count')})",
                    )
                    cached_instr = (
                        _profile_to_instructions(cached)
                    )
                    sub_c, _fill_c = (
                        await _try_fill_and_submit(
                            page, cached_instr, phone,
                            firstname, lastname,
                            patronymic,
                            email, comment,
                            rucaptcha_key, url,
                            step_dir, "cached",
                        )
                    )
                    if (
                        sub_c
                        and sub_c.get("status")
                        == "success"
                    ):
                        result.update(sub_c)
                        instructions = cached_instr
                        instructions_used = cached_instr
                        _logger.ok(
                            "profile_cache: применён успешно",
                        )
                    else:
                        new_fc = (
                            await db_increment_profile_fail(
                                domain,
                            )
                        )
                        _logger.warn(
                            f"profile_cache: не сработал "
                            f"(fail_count={new_fc})",
                        )
                        if (
                            new_fc
                            >= PROFILE_FAIL_THRESHOLD
                        ):
                            await db_delete_form_profile(
                                domain,
                            )
                            _logger.warn(
                                "profile_cache: удалён "
                                "(превышен порог фейлов)",
                            )
                elif cached:
                    _logger.step(
                        "profile_cache",
                        f"stale (fail={cached.get('fail_count')} "
                        f"last={cached.get('last_success_at')})",
                    )

            if result["status"] != "success":
                _logger.step("extract", "ищем форму")
                form_json, form_ctx = (
                    await extract_forms(page)
                )
                has_ct = await has_calltouch(page)
                keep_ct = not form_json and has_ct
                await suppress_widgets(
                    page, keep_calltouch=keep_ct,
                )
                await step_shot(
                    page, "02_form_found", step_dir
                )

                iframe_ctx = (
                    form_ctx.frame
                    if form_ctx and form_ctx.frame
                    else None
                )

                if form_json:
                    _logger.step(
                        "smart_plan",
                        "строим эвристику",
                    )
                    instructions = build_smart_plan(
                        form_json
                    )
                    if (
                        instructions
                        and instructions.get("actions")
                    ):
                        sub, fill_res = (
                            await _try_fill_and_submit(
                                page, instructions, phone,
                                firstname, lastname,
                                patronymic,
                                email, comment,
                                rucaptcha_key, url,
                                step_dir, "smart",
                                context=iframe_ctx,
                            )
                        )
                        if sub:
                            result.update(sub)
                            if (
                                result["status"]
                                == "success"
                            ):
                                instructions_used = (
                                    instructions
                                )
                            if result["status"] in (
                                "success", "captcha",
                            ):
                                await _maybe_save_profile(
                                    domain, result,
                                    instructions_used,
                                )
                                _logger.finish(result)
                                _site_logger_var.reset(
                                    _log_token
                                )
                                return result
                        elif fill_res:
                            result.update({
                                "status": "uncertain",
                                "method": "form_smart",
                                "message": (
                                    "DOM не изменился "
                                    "после заполнения"
                                ),
                            })
            else:
                form_json = None
                iframe_ctx = None

            if (
                result["status"] != "success"
                and ai_key
            ):
                _logger.step(
                    "ai",
                    f"отправляем HTML в {ai_provider}",
                )
                page_html = await collect_full_html(
                    page
                )
                async with _ai_sem:
                    try:
                        (
                            ai_plan, ai_tokens, _,
                        ) = await asyncio.to_thread(
                            ask_ai_sync,
                            page_html, url, ai_key,
                            ai_provider, None,
                        )
                        tokens += ai_tokens
                        _logger.log_ai(
                            f"html={len(page_html)}",
                            ai_plan, ai_tokens,
                            ai_provider,
                        )
                    except Exception as e:
                        raw_text = getattr(
                            e, "raw_text", "",
                        )
                        err_tokens = getattr(
                            e, "tokens", 0,
                        )
                        tokens += err_tokens
                        if raw_text:
                            try:
                                (_logger.site_dir
                                 / "ai_raw.txt").write_text(
                                    raw_text,
                                    encoding="utf-8",
                                )
                            except Exception:
                                pass
                        _logger.log_ai(
                            "", {}, err_tokens,
                            ai_provider,
                            error=str(e)[:600],
                        )
                        ai_plan = None

                if (
                    ai_plan
                    and ai_plan.get("form_found")
                    and ai_plan.get("actions")
                ):
                    sub2, _ = (
                        await _try_fill_and_submit(
                            page, ai_plan, phone,
                            firstname, lastname,
                            patronymic,
                            email, comment,
                            rucaptcha_key, url,
                            step_dir, ai_provider,
                            context=iframe_ctx,
                        )
                    )
                    if sub2:
                        result.update(sub2)
                        if (
                            result["status"]
                            == "success"
                        ):
                            instructions = ai_plan
                            instructions_used = ai_plan
                    else:
                        result.update({
                            "status": "uncertain",
                            "method": f"form_{ai_provider}",
                            "message": (
                                "DOM не изменился "
                                "после AI заполнения"
                            ),
                        })
                elif ai_plan and not ai_plan.get(
                    "form_found"
                ):
                    result["message"] = (
                        "AI не нашёл форму"
                    )
                    result["reason_code"] = "no_form"
                elif not form_json:
                    result["message"] = (
                        "Форма не найдена"
                    )
                    result["reason_code"] = "no_form"

            if (
                result["status"] != "success"
                and not ai_key
                and not form_json
            ):
                result["message"] = (
                    "Форма не найдена, "
                    "AI ключ не указан"
                )
                result["reason_code"] = "no_form"

            if (
                result["status"] != "success"
                and keep_ct
            ):
                _logger.step(
                    "calltouch",
                    "форм нет, пробуем Calltouch API",
                )
                ct_result = await try_calltouch(
                    page, phone, firstname,
                )
                if ct_result:
                    result.update(ct_result)

            if result["status"] not in ("success", "captcha"):
                cap_type = await detect_captcha_overlay(page)
                if cap_type:
                    _logger.warn(f"captcha overlay: {cap_type}")
                    if await _resolve_captcha_overlay(
                        page, cap_type, url, rucaptcha_key,
                    ):
                        _logger.ok(
                            "captcha overlay решена в финале"
                        )
                        result.update({
                            "status": "uncertain",
                            "method": "captcha_overlay_solved",
                            "message": "Капча-оверлей решена",
                        })
                    else:
                        result.update({
                            "status": "captcha",
                            "method": "captcha_overlay",
                            "message": f"Капча-оверлей: {cap_type}",
                            "reason_code": "captcha",
                        })

            if result["status"] not in (
                "success", "captcha", "failed",
            ):
                if not result.get("message"):
                    result["message"] = (
                        "Не удалось определить "
                        "результат"
                    )
                result["status"] = "uncertain"

            result["tokens_used"] = tokens
            result["ai_instructions"] = instructions

        finally:
            if ctx is not None:
                _active_contexts.discard(ctx)
                try:
                    await ctx.close()
                except Exception:
                    pass
            try:
                await _recycle_browser_if_needed(browser_idx)
            except Exception:
                pass

    except asyncio.CancelledError:
        result["status"] = "cancelled"
        result["method"] = "cancelled"
        result["message"] = "Остановлено пользователем"
        result["reason_code"] = "cancelled"
        _logger.warn("отмена: stop по запросу")
        _logger.finish(result)
        _site_logger_var.reset(_log_token)
        raise
    except Exception as e:
        result["message"] = (
            f"Критическая ошибка: "
            f"{type(e).__name__}: {str(e)[:200]}"
        )
        result["reason_code"] = "crash"
        _logger.err("check_site", exc=e)

    result["reason_code"] = (
        result.get("reason_code")
        or _classify_reason(result)
    )
    try:
        await _maybe_save_profile(
            domain, result, instructions_used,
        )
    except Exception:
        pass
    _logger.finish(result)
    _site_logger_var.reset(_log_token)
    return result

async def _process_one(
    url, phone, firstname, lastname, patronymic,
    email, comment,
    ai_key, rucaptcha_key,
    session_id, sem, max_retries=3,
    proxy: dict = None,
    queue_id: str = None,
    ai_provider: str = "deepseek",
):
    current = asyncio.current_task()
    if current is not None:
        _active_tasks.add(current)
    try:
        async with sem:
            if queue_id:
                await _wait_for_working_hours(queue_id)
                if _queue_cancel.is_set():
                    return None

            prev_hint = None
            result = None
            for attempt in range(1, max_retries + 1):
                if _queue_cancel.is_set():
                    return None
                await _ws_broadcast({
                    "type": "attempt",
                    "url": url,
                    "attempt_no": attempt,
                    "max_attempts": max_retries,
                    "retrying": attempt > 1,
                })
                result = await check_site_v2(
                    url, phone,
                    firstname, lastname, patronymic,
                    email, comment,
                    ai_key, rucaptcha_key,
                    attempt_no=attempt,
                    max_retries=max_retries,
                    prev_hint=prev_hint,
                    proxy=proxy,
                    session_id=session_id,
                    ai_provider=ai_provider,
                )
                await db_add_result(
                    session_id, url, result,
                )
                await _ws_broadcast({
                    "type": "attempt_result",
                    "url": url,
                    "attempt_no": attempt,
                    "max_attempts": max_retries,
                    "status": result["status"],
                    "reason_code": result.get(
                        "reason_code", ""
                    ),
                })
                if result["status"] in (
                    "success", "captcha",
                ):
                    break
                prev_hint = {
                    "reason_code": result.get(
                        "reason_code", ""
                    ),
                    "status": result["status"],
                    "message": result.get(
                        "message", ""
                    ),
                }
                if attempt < max_retries:
                    try:
                        await asyncio.wait_for(
                            _queue_cancel.wait(),
                            timeout=2,
                        )
                        return None
                    except asyncio.TimeoutError:
                        pass

            if result is None:
                return None

            msg = {
                "type": "result",
                "url": url,
                "status": result["status"],
                "method": result["method"],
                "message": result.get("message", ""),
                "tokens_used": result.get(
                    "tokens_used", 0
                ),
                "ai_notes": (
                    (result.get("ai_instructions")
                     or {}).get("notes", "")
                ),
                "reason_code": result.get(
                    "reason_code", ""
                ),
                "attempt_no": result.get(
                    "attempt_no", 1
                ),
                "max_attempts": max_retries,
            }
            await _ws_broadcast(msg)
            return result
    except asyncio.CancelledError:
        await _ws_broadcast({
            "type": "attempt_result",
            "url": url,
            "attempt_no": 0,
            "max_attempts": max_retries,
            "status": "cancelled",
            "reason_code": "cancelled",
        })
        return None
    finally:
        if current is not None:
            _active_tasks.discard(current)

async def _run_session_bg(
    sid, urls, phone,
    firstname, lastname, patronymic,
    email, comment,
    ai_key, rucaptcha_key,
    max_attempts,
    ai_provider: str = "deepseek",
):
    await _ws_broadcast({
        "type": "start",
        "session_id": sid,
        "total": len(urls),
    })

    sem = asyncio.Semaphore(CONCURRENCY)
    tasks = [
        _process_one(
            u.strip(), phone,
            firstname, lastname, patronymic,
            email, comment,
            ai_key, rucaptcha_key,
            sid, sem, max_attempts,
            ai_provider=ai_provider,
        )
        for u in urls if u.strip()
    ]
    try:
        await asyncio.gather(
            *tasks, return_exceptions=True,
        )
    finally:
        await db_finish_session(sid)
        await _ws_broadcast({
            "type": "done",
            "session_id": sid,
        })

async def run_session(
    urls: list, phone: str,
    firstname: str = "", lastname: str = "",
    patronymic: str = "",
    email: str = "", comment: str = "",
    claude_key: str = "",
    rucaptcha_key: str = "",
    session_name: str = "",
    max_attempts: int = 3,
    deepseek_key: str = "",
    ai_provider: str = "deepseek",
):
    _cleanup_data_except_db()
    sid = str(uuid.uuid4())[:8]
    await db_create_session(
        sid, session_name or sid, len(urls),
    )
    ai_provider = (ai_provider or "deepseek").lower()
    ai_key = (
        deepseek_key if ai_provider == "deepseek"
        else claude_key
    )
    _queue_cancel.clear()
    asyncio.create_task(
        _run_session_bg(
            sid, urls, phone,
            firstname, lastname, patronymic,
            email, comment,
            ai_key, rucaptcha_key,
            max_attempts,
            ai_provider=ai_provider,
        )
    )
    return sid

async def _ensure_profile_session(
    queue_id: str, client: dict,
) -> str:
    if client.get("session_id"):
        return client["session_id"]
    sid = str(uuid.uuid4())[:8]
    parts = [client.get("firstname", ""), client.get("lastname", "")]
    label = " ".join(p for p in parts if p)
    session_name = (
        f"{label} ({client['phone']})"
        if label else client["phone"]
    )
    await db_create_session(
        sid, session_name, 0,
        queue_id=queue_id, client_id=client["id"],
    )
    await db_update_client_status(
        client["id"], "running", session_id=sid,
    )
    client["session_id"] = sid
    return sid

async def _process_url_once(
    task: dict, client: dict, sid: str,
    ai_key: str, rucaptcha_key: str,
    ai_provider: str, worker_id: int,
    tabs_per_browser: int,
) -> dict:
    url = task["url"]
    comment = task.get("comment", "")
    proxy = parse_proxy(client.get("proxy", ""))
    browser_idx = worker_id // max(1, tabs_per_browser)

    current = asyncio.current_task()
    if current is not None:
        _active_tasks.add(current)
    try:
        if _queue_cancel.is_set():
            return {"url": url, "status": "cancelled"}
        await _ws_broadcast({
            "type": "attempt",
            "url": url,
            "attempt_no": 1,
            "max_attempts": 1,
            "retrying": False,
        })
        try:
            result = await check_site_v2(
                url,
                client["phone"],
                client.get("firstname", ""),
                client.get("lastname", ""),
                client.get("patronymic", ""),
                client.get("email", ""),
                comment,
                ai_key, rucaptcha_key,
                attempt_no=1, max_retries=1,
                proxy=proxy,
                session_id=sid,
                ai_provider=ai_provider,
                browser_idx=browser_idx,
            )
        except asyncio.CancelledError:
            return {"url": url, "status": "cancelled"}
        except Exception as e:
            result = {
                "url": url, "status": "failed",
                "method": "crash",
                "message": f"{type(e).__name__}: {str(e)[:120]}",
                "tokens_used": 0,
                "reason_code": "crash",
                "attempt_no": 1,
            }
        await db_add_result(sid, url, result)
        await _ws_broadcast({
            "type": "result",
            "url": url,
            "status": result["status"],
            "method": result.get("method", ""),
            "message": result.get("message", ""),
            "tokens_used": result.get("tokens_used", 0),
            "ai_notes": (
                (result.get("ai_instructions") or {}).get("notes", "")
            ),
            "reason_code": result.get("reason_code", ""),
            "attempt_no": 1,
            "max_attempts": 1,
            "client_id": client["id"],
        })
        return result
    finally:
        if current is not None:
            _active_tasks.discard(current)

async def _process_chunk(
    chunk: list, client: dict, sid: str,
    ai_key: str, rucaptcha_key: str,
    ai_provider: str,
    concurrency: int, tabs_per_browser: int,
) -> list:
    sem = asyncio.Semaphore(concurrency)
    results = []
    lock = asyncio.Lock()

    async def worker(worker_id: int, task: dict):
        async with sem:
            r = await _process_url_once(
                task, client, sid,
                ai_key, rucaptcha_key,
                ai_provider, worker_id, tabs_per_browser,
            )
            async with lock:
                results.append({"task": task, "result": r})

    tasks = [
        asyncio.create_task(worker(i % concurrency, t))
        for i, t in enumerate(chunk)
    ]
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        pass
    return results

async def _run_queue_bg(queue_id: str):
    global _active_queue_id, _active_queue_task
    _active_queue_id = queue_id
    _queue_cancel.clear()

    final = "done"
    try:
        queue = await db_get_queue(queue_id)
        clients = await db_get_queue_clients(queue_id)
        groups = await db_get_queue_groups(queue_id)

        sites: dict = {}
        for g in groups:
            try:
                urls_list = json.loads(g.get("urls") or "[]")
            except Exception:
                urls_list = []
            for u in urls_list:
                u = (u or "").strip()
                if not u or u in sites:
                    continue
                sites[u] = {
                    "url": u,
                    "group_id": g["id"],
                    "comment": g.get("comment", ""),
                    "status": "pending",
                    "tried": set(),
                }

        if not sites:
            legacy_urls = json.loads(queue.get("urls") or "[]")
            for u in legacy_urls:
                u = (u or "").strip()
                if not u or u in sites:
                    continue
                sites[u] = {
                    "url": u,
                    "group_id": None,
                    "comment": "",
                    "status": "pending",
                    "tried": set(),
                }

        chunk_size = int(queue.get("chunk_size", 100) or 100)
        rest_seconds = int(queue.get("rest_seconds", 180) or 180)
        browser_count = int(queue.get("browser_count", 2) or 2)
        tabs_per_browser = int(queue.get("tabs_per_browser", 2) or 2)
        concurrency = max(1, browser_count * tabs_per_browser)
        drop_after_tries = min(2, max(1, len(clients)))
        ai_provider = (queue.get("ai_provider") or "deepseek").lower()
        ai_key = (
            queue.get("deepseek_key", "")
            if ai_provider == "deepseek"
            else queue.get("claude_key", "")
        )
        rucaptcha_key = queue.get("rucaptcha_key", "")

        await db_update_queue_status(queue_id, "running")
        await _ws_broadcast({
            "type": "queue_start",
            "queue_id": queue_id,
            "total_clients": len(clients),
            "total_urls": len(sites),
            "chunk_size": chunk_size,
            "rest_seconds": rest_seconds,
            "concurrency": concurrency,
        })

        active_clients = list(clients)
        rr_idx = 0

        while sites and active_clients:
            if _queue_cancel.is_set():
                break

            client = active_clients[rr_idx % len(active_clients)]
            cid = client["id"]

            candidates = [
                s for s in sites.values()
                if s["status"] == "pending" and cid not in s["tried"]
            ]
            if not candidates:
                active_clients.pop(rr_idx % len(active_clients))
                if not active_clients:
                    break
                continue

            chunk = candidates[:chunk_size]
            sid = await _ensure_profile_session(queue_id, client)

            await _wait_for_working_hours(queue_id)
            if _queue_cancel.is_set():
                break

            await _ws_broadcast({
                "type": "chunk_start",
                "queue_id": queue_id,
                "client_id": cid,
                "session_id": sid,
                "client_position": client["position"],
                "chunk_size": len(chunk),
                "remaining_sites": len(sites),
            })

            chunk_results = await _process_chunk(
                chunk, client, sid,
                ai_key, rucaptcha_key,
                ai_provider,
                concurrency, tabs_per_browser,
            )

            success_urls = {
                r["task"]["url"]
                for r in chunk_results
                if (r.get("result") or {}).get("status") == "success"
            }
            for s in chunk:
                s["tried"].add(cid)
                if s["url"] in success_urls:
                    s["status"] = "success"

            done_urls = []
            for url, s in list(sites.items()):
                if s["status"] == "success":
                    done_urls.append(url)
                    del sites[url]
                elif len(s["tried"]) >= drop_after_tries:
                    done_urls.append(url)
                    del sites[url]

            done_clients = sum(
                1 for c in clients if c["id"] not in {c2["id"] for c2 in active_clients}
            )
            await db_update_queue_progress(
                queue_id, rr_idx, done_clients,
            )

            await _ws_broadcast({
                "type": "chunk_done",
                "queue_id": queue_id,
                "client_id": cid,
                "session_id": sid,
                "chunk_size": len(chunk),
                "remaining_sites": len(sites),
                "dropped": len(done_urls),
            })

            if _queue_cancel.is_set():
                break

            try:
                await _reset_browsers()
            except Exception:
                pass
            _active_contexts.clear()

            if sites and rest_seconds > 0:
                await _ws_broadcast({
                    "type": "rest",
                    "queue_id": queue_id,
                    "seconds": rest_seconds,
                })
                try:
                    await asyncio.wait_for(
                        _queue_cancel.wait(),
                        timeout=rest_seconds,
                    )
                    break
                except asyncio.TimeoutError:
                    pass

            rr_idx += 1

        if _queue_cancel.is_set():
            final = "cancelled"
    except asyncio.CancelledError:
        final = "cancelled"
    finally:
        try:
            for c in await db_get_queue_clients(queue_id):
                if c.get("session_id"):
                    await db_finish_session(c["session_id"])
                await db_update_client_status(
                    c["id"],
                    "cancelled" if final == "cancelled" else "done",
                )
        except Exception:
            pass
        await db_update_queue_status(queue_id, final)
        _active_queue_id = None
        _active_queue_task = None
        _active_tasks.clear()
        _active_contexts.clear()
        try:
            await _reset_browsers()
        except Exception:
            pass

        await _ws_broadcast({
            "type": "queue_done",
            "queue_id": queue_id,
            "status": final,
        })

async def run_queue(
    urls: list, clients: list,
    claude_key: str = "",
    rucaptcha_key: str = "",
    queue_name: str = "",
    max_attempts: int = 1,
    deepseek_key: str = "",
    ai_provider: str = "deepseek",
    groups: list = None,
    chunk_size: int = 100,
    rest_seconds: int = 180,
    browser_count: int = 2,
    tabs_per_browser: int = 2,
) -> str:
    global _active_queue_id
    global _active_queue_task
    if _active_queue_id:
        raise ValueError("Очередь уже запущена")

    _cleanup_data_except_db()

    qid = str(uuid.uuid4())[:8]
    ai_provider = (ai_provider or "deepseek").lower()

    groups = groups or []
    all_urls = list(urls or [])
    for g in groups:
        all_urls.extend(g.get("urls", []) or [])
    seen = set()
    dedup_urls = []
    for u in all_urls:
        u = (u or "").strip()
        if u and u not in seen:
            seen.add(u)
            dedup_urls.append(u)

    await db_create_queue(
        qid, queue_name or qid, dedup_urls,
        claude_key, rucaptcha_key,
        max_attempts, len(clients),
        deepseek_key=deepseek_key,
        ai_provider=ai_provider,
        chunk_size=chunk_size,
        rest_seconds=rest_seconds,
        browser_count=browser_count,
        tabs_per_browser=tabs_per_browser,
    )

    if not groups and dedup_urls:
        groups = [{"name": "", "comment": "", "urls": dedup_urls}]
    for i, g in enumerate(groups):
        await db_add_queue_group(
            qid, i,
            g.get("name", ""),
            g.get("comment", ""),
            g.get("urls", []) or [],
        )

    for i, c in enumerate(clients):
        await db_add_queue_client(
            qid, i, c["phone"],
            c.get("firstname", ""),
            c.get("lastname", ""),
            c.get("patronymic", ""),
            c.get("email", ""),
            c.get("comment", ""),
            c.get("proxy", ""),
        )

    _queue_cancel.clear()
    _active_queue_task = asyncio.create_task(
        _run_queue_bg(qid)
    )
    return qid
