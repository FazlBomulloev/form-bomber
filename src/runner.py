import asyncio
import json
import uuid
from pathlib import Path

from playwright.async_api import async_playwright

from config import (
    USER_AGENT, CONCURRENCY,
    AI_CONCURRENCY, COOKIE_CONSENT_SCRIPT,
)
from models import domain_from_url
from logger import SiteLogger, _site_logger_var
from db import (
    db_create_session, db_add_result,
    db_finish_session,
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
LOG_DIR = Path("data/logs")


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
    )

    form_el = fill_result["form_el"]
    await fill_all_empty_fields(
        ctx, phone,
        firstname, lastname, patronymic,
        email, comment, form_el,
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
):
    domain = domain_from_url(url)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _logger = SiteLogger(domain, url, LOG_DIR)
    _log_token = _site_logger_var.set(_logger)

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
        global _browser
        if _browser is None:
            pw = await async_playwright().start()
            _browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features="
                    "AutomationControlled",
                    "--no-sandbox",
                ],
            )

        ctx = await _browser.new_context(
            user_agent=USER_AGENT,
            viewport={
                "width": 1280, "height": 900,
            },
            ignore_https_errors=True,
        )
        page = await ctx.new_page()
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

            # ── 2. Поиск формы ─────────────────
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
            instructions = None
            tokens = 0
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
                        if result["status"] in (
                            "success", "captcha",
                        ):
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
                        _logger.log_ai(
                            "", {}, 0, "",
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
            try:
                await ctx.close()
            except Exception:
                pass

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
    _logger.finish(result)
    _site_logger_var.reset(_log_token)
    return result


async def _process_one(
    url, phone, firstname, lastname, patronymic,
    email, comment,
    claude_key, rucaptcha_key,
    session_id, sem, max_retries=3,
):
    async with sem:
        prev_hint = None
        for attempt in range(1, max_retries + 1):
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
                await asyncio.sleep(2)

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
    await asyncio.gather(*tasks)
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
    sid = str(uuid.uuid4())[:8]
    await db_create_session(
        sid, session_name or sid, len(urls),
    )
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
