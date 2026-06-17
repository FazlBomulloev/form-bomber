import asyncio
import json
import shutil
import time
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
    db_get_queue, db_get_queue_clients,
    db_update_queue_status, db_update_queue_progress,
    db_update_client_status,
    db_get_form_profile, db_save_form_profile,
    db_increment_profile_fail,
    db_delete_form_profile,
)
from ai_provider import ask_ai_sync, collect_full_html
from form_finder import extract_forms, build_smart_plan
from form_filler import (
    execute_action_plan, submit_with_retry,
    fill_all_empty_fields,
)
from captcha import handle_captcha, detect_captcha_overlay
from browser_utils import (
    dismiss_cookie_banners, suppress_widgets,
    has_calltouch, step_shot,
)
from calltouch import try_calltouch

_ai_sem = asyncio.Semaphore(AI_CONCURRENCY)
_ws_clients: set = set()
_browser = None
_pw = None
_browser_lock = asyncio.Lock()
_active_queue_id: str | None = None
_queue_cancel = asyncio.Event()
_active_tasks: set = set()
_active_contexts: set = set()
_active_queue_task = None
LOG_DIR = Path("data/logs")
LOG_RETENTION_DAYS = 3


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


async def _logs_ttl_loop():
    while True:
        try:
            await asyncio.sleep(3600)
            cutoff = time.time() - LOG_RETENTION_DAYS * 86400
            if not LOG_DIR.exists():
                continue
            for sid_dir in LOG_DIR.iterdir():
                try:
                    if not sid_dir.is_dir():
                        continue
                    if sid_dir.stat().st_mtime < cutoff:
                        shutil.rmtree(
                            sid_dir, ignore_errors=True,
                        )
                except Exception:
                    continue
        except asyncio.CancelledError:
            raise
        except Exception:
            pass


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


async def _maybe_save_profile(
    domain: str, result: dict, instructions: dict,
):
    """Сохраняет профиль формы если submit удался."""
    if result.get("status") != "success":
        return
    if not instructions or not instructions.get(
        "actions",
    ):
        return
    method = result.get("method", "")
    if not method.startswith("form_"):
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


async def _reset_browser():
    global _browser, _pw
    _browser = None
    try:
        if _pw:
            await _pw.stop()
    except Exception:
        pass
    _pw = None


async def _get_browser():
    global _browser, _pw
    async with _browser_lock:
        if _browser is not None:
            try:
                _browser.contexts
                return _browser
            except Exception:
                await _reset_browser()
        _pw = await async_playwright().start()
        _browser = await _pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features="
                "AutomationControlled",
                "--no-sandbox",
            ],
        )
        return _browser


async def _ws_broadcast(data: dict):
    msg = json.dumps(data, ensure_ascii=False)
    dead = set()
    for ws in _ws_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    _ws_clients.difference_update(dead)


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
    """Общая логика: заполнить → капча → submit → детект.
    context — Frame для iframe-форм, иначе page.
    Возвращает (result_dict или None, fill_result)."""
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
    claude_key: str = "",
    rucaptcha_key: str = "",
    attempt_no: int = 1,
    max_retries: int = 6,
    prev_hint: dict = None,
    proxy: dict = None,
    session_id: str = "",
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

        # ── 1. Браузер ──────────────────────────
        _logger.step("browser", "запуск")
        ctx_kwargs = {
            "user_agent": USER_AGENT,
            "viewport": {"width": 1280, "height": 900},
            "ignore_https_errors": True,
        }
        if proxy:
            ctx_kwargs["proxy"] = proxy
        for _br_try in range(2):
            try:
                browser = await _get_browser()
                ctx = await browser.new_context(
                    **ctx_kwargs,
                )
                _active_contexts.add(ctx)
                page = await ctx.new_page()
                break
            except Exception:
                await _reset_browser()
                if _br_try == 1:
                    raise
                _logger.warn("браузер упал, перезапуск")
                await asyncio.sleep(1)
        step_dir = _logger.site_dir

        try:
            _logger.step("navigate", url)
            await page.goto(
                url, wait_until="domcontentloaded",
                timeout=30000,
            )
            await asyncio.sleep(2)

            try:
                await page.evaluate(
                    COOKIE_CONSENT_SCRIPT
                )
            except Exception:
                pass
            await dismiss_cookie_banners(page)

            # ── Решаем captcha overlay до поиска формы
            pre_cap = await detect_captcha_overlay(page)
            if pre_cap:
                _logger.warn(
                    f"captcha overlay при загрузке: "
                    f"{pre_cap}"
                )
                from captcha import (
                    _try_click_smartcaptcha,
                    _solve_smartcaptcha_overlay,
                    _extract_smartcaptcha_sitekey,
                    _solve_captcha,
                    _inject_captcha_token,
                )
                cap_solved = False
                # Попробуем кликнуть чекбокс
                clicked = (
                    await _try_click_smartcaptcha(page)
                )
                if clicked:
                    await asyncio.sleep(3)
                    still = (
                        await detect_captcha_overlay(page)
                    )
                    if not still:
                        cap_solved = True
                        _logger.ok(
                            "captcha overlay: клик помог"
                        )
                if not cap_solved and rucaptcha_key:
                    sc_res = (
                        await _solve_smartcaptcha_overlay(
                            page, url, rucaptcha_key,
                        )
                    )
                    if sc_res == "ok":
                        cap_solved = True
                        _logger.ok(
                            "captcha overlay решена API"
                        )
                    else:
                        sitekey = (
                            await
                            _extract_smartcaptcha_sitekey(
                                page,
                            )
                        )
                        if sitekey:
                            token = await _solve_captcha(
                                "yandex", sitekey,
                                url, rucaptcha_key,
                            )
                            if token:
                                ok = (
                                    await
                                    _inject_captcha_token(
                                        page, "yandex",
                                        token,
                                    )
                                )
                                if ok:
                                    cap_solved = True
                                    _logger.ok(
                                        "captcha overlay "
                                        "решена yandex API"
                                    )
                if cap_solved:
                    await asyncio.sleep(2)
                    try:
                        await page.reload(
                            wait_until=(
                                "domcontentloaded"
                            ),
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

            # ── 1.5. Кеш профиля формы ─────────
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
                        # запись профиля — единым хвостом
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

            # ── 2. Поиск формы ─────────────────
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

                # ── 3. Строим план ─────────────────
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
                        # ── 4. Заполняем + submit ──
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

            # ── 5. AI fallback ─────────────────
            if (
                result["status"] != "success"
                and claude_key
            ):
                _logger.step(
                    "ai",
                    "отправляем HTML в Claude",
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
                            page_html, url, claude_key,
                        )
                        tokens += ai_tokens
                        _logger.log_ai(
                            f"html={len(page_html)}",
                            ai_plan, ai_tokens,
                            "claude",
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
                            "claude",
                            error=str(e)[:200],
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
                            step_dir, "claude",
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
                            "method": "form_claude",
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

            # ── Нет ключа AI и нет формы ───────
            if (
                result["status"] != "success"
                and not claude_key
                and not form_json
            ):
                result["message"] = (
                    "Форма не найдена, "
                    "AI ключ не указан"
                )
                result["reason_code"] = "no_form"

            # ── Calltouch fallback ─────────────
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

            # ── Проверка captcha overlay ───────
            if result["status"] not in (
                "success", "captcha",
            ):
                cap_type = (
                    await detect_captcha_overlay(page)
                )
                if cap_type:
                    _logger.warn(
                        f"captcha overlay: {cap_type}"
                    )
                    # Пробуем решить overlay
                    from captcha import (
                        _try_click_smartcaptcha
                        as _tcs_final,
                        _solve_smartcaptcha_overlay
                        as _sso_final,
                        _detect_image_captcha
                        as _dic_final,
                    )
                    final_solved = False
                    if cap_type in (
                        "yandex_smartcaptcha",
                        "captcha_overlay",
                    ):
                        try:
                            cl = await _tcs_final(page)
                            if cl:
                                await asyncio.sleep(3)
                                st = (
                                    await
                                    detect_captcha_overlay(
                                        page
                                    )
                                )
                                if not st:
                                    final_solved = True
                        except Exception:
                            pass
                        if (
                            not final_solved
                            and rucaptcha_key
                        ):
                            try:
                                r = await _sso_final(
                                    page, url,
                                    rucaptcha_key,
                                )
                                if r == "ok":
                                    final_solved = True
                            except Exception:
                                pass
                    elif (
                        cap_type == "image_captcha"
                        and rucaptcha_key
                    ):
                        try:
                            r = await _dic_final(
                                page, rucaptcha_key,
                            )
                            if r == "ok":
                                final_solved = True
                        except Exception:
                            pass
                    if final_solved:
                        _logger.ok(
                            "captcha overlay решена "
                            "в финале"
                        )
                        result.update({
                            "status": "uncertain",
                            "method": (
                                "captcha_overlay"
                                "_solved"
                            ),
                            "message": (
                                "Капча-оверлей "
                                "решена"
                            ),
                        })
                    else:
                        result.update({
                            "status": "captcha",
                            "method": "captcha_overlay",
                            "message": (
                                f"Капча-оверлей: "
                                f"{cap_type}"
                            ),
                            "reason_code": "captcha",
                        })

            # ── Финальный статус ────────────────
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
    claude_key, rucaptcha_key,
    session_id, sem, max_retries=3,
    proxy: dict = None,
    queue_id: str = None,
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
                    claude_key, rucaptcha_key,
                    attempt_no=attempt,
                    max_retries=max_retries,
                    prev_hint=prev_hint,
                    proxy=proxy,
                    session_id=session_id,
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
    claude_key, rucaptcha_key,
    max_attempts,
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
            claude_key, rucaptcha_key,
            sid, sem, max_attempts,
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
):
    _cleanup_data_except_db()
    sid = str(uuid.uuid4())[:8]
    await db_create_session(
        sid, session_name or sid, len(urls),
    )
    _queue_cancel.clear()
    asyncio.create_task(
        _run_session_bg(
            sid, urls, phone,
            firstname, lastname, patronymic,
            email, comment,
            claude_key, rucaptcha_key,
            max_attempts,
        )
    )
    return sid


# ── Multi-client queue ─────────────────────────


async def _run_client(
    queue_id: str, client: dict,
    urls: list, claude_key: str,
    rucaptcha_key: str, max_attempts: int,
):
    sid = str(uuid.uuid4())[:8]
    client_id = client["id"]
    proxy = parse_proxy(client.get("proxy", ""))

    parts = [
        client.get("firstname", ""),
        client.get("lastname", ""),
    ]
    label = " ".join(p for p in parts if p)
    session_name = (
        f"{label} ({client['phone']})"
        if label else client["phone"]
    )

    await db_create_session(
        sid, session_name, len(urls),
        queue_id=queue_id,
        client_id=client_id,
    )
    await db_update_client_status(
        client_id, "running", session_id=sid,
    )

    await _ws_broadcast({
        "type": "client_start",
        "queue_id": queue_id,
        "client_id": client_id,
        "session_id": sid,
        "client_position": client["position"],
        "client_name": session_name,
        "total_urls": len(urls),
    })

    sem = asyncio.Semaphore(CONCURRENCY)
    tasks = [
        _process_one(
            u.strip(),
            client["phone"],
            client.get("firstname", ""),
            client.get("lastname", ""),
            client.get("patronymic", ""),
            client.get("email", ""),
            client.get("comment", ""),
            claude_key, rucaptcha_key,
            sid, sem, max_attempts,
            proxy=proxy,
            queue_id=queue_id,
        )
        for u in urls if u.strip()
    ]
    try:
        await asyncio.gather(
            *tasks, return_exceptions=True,
        )
    finally:
        await db_finish_session(sid)
        final_status = (
            "cancelled" if _queue_cancel.is_set()
            else "done"
        )
        await db_update_client_status(
            client_id, final_status,
        )

        await _ws_broadcast({
            "type": "client_done",
            "queue_id": queue_id,
            "client_id": client_id,
            "session_id": sid,
            "client_position": client["position"],
            "status": final_status,
        })
    return sid


async def _run_queue_bg(queue_id: str):
    global _active_queue_id, _active_queue_task
    _active_queue_id = queue_id
    _queue_cancel.clear()

    final = "done"
    try:
        queue = await db_get_queue(queue_id)
        clients = await db_get_queue_clients(queue_id)
        urls = json.loads(queue["urls"])

        await db_update_queue_status(queue_id, "running")
        await _ws_broadcast({
            "type": "queue_start",
            "queue_id": queue_id,
            "total_clients": len(clients),
            "total_urls": len(urls),
        })

        start_idx = queue.get("current_client_idx", 0)

        for i, client in enumerate(
            clients[start_idx:], start=start_idx,
        ):
            if _queue_cancel.is_set():
                break

            await _wait_for_working_hours(queue_id)
            if _queue_cancel.is_set():
                break

            await db_update_queue_progress(
                queue_id, i, i,
            )

            try:
                await _run_client(
                    queue_id, client, urls,
                    queue["claude_key"],
                    queue["rucaptcha_key"],
                    queue["max_attempts"],
                )
            except asyncio.CancelledError:
                break

            await db_update_queue_progress(
                queue_id, i + 1, i + 1,
            )

        if _queue_cancel.is_set():
            final = "cancelled"
    except asyncio.CancelledError:
        final = "cancelled"
    finally:
        await db_update_queue_status(queue_id, final)
        _active_queue_id = None
        _active_queue_task = None
        _active_tasks.clear()
        _active_contexts.clear()

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
    max_attempts: int = 3,
) -> str:
    global _active_queue_id
    global _active_queue_task
    if _active_queue_id:
        raise ValueError("Очередь уже запущена")

    _cleanup_data_except_db()

    qid = str(uuid.uuid4())[:8]

    await db_create_queue(
        qid, queue_name or qid, urls,
        claude_key, rucaptcha_key,
        max_attempts, len(clients),
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
