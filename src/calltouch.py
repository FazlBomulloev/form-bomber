import aiohttp

from logger import get_logger

_TIMEOUT = aiohttp.ClientTimeout(total=20)
_LOAD_URL = "https://mod.calltouch.ru/callback_load.php"
_CALL_URL = "https://mod.calltouch.ru/callback_call.php"


async def _collect_hidden_fields(page):
    """Считывает все hidden-поля форм страницы (name→value)
    для прямого POST-фолбэка. Возвращает
    (hidden_dict, csrf_names). Так прямой POST несёт
    существующие _token/csrf/nonce, а не только phone."""
    try:
        return await page.evaluate(r"""() => {
            const hidden = {};
            const els = document.querySelectorAll(
                'input[type="hidden"]');
            for (const el of els) {
                if (!el.name) continue;
                if (el.disabled) continue;
                hidden[el.name] = el.value || '';
            }
            const re = /csrf|_?token|nonce|authenticity/i;
            const csrf = Object.keys(hidden)
                .filter(n => re.test(n));
            return {hidden, csrf};
        }""")
    except Exception:
        return {"hidden": {}, "csrf": []}


async def _get_calltouch_cookies(page):
    cookies = await page.context.cookies()
    session_id = None
    site_id = None
    for c in cookies:
        if c["name"] == "_ct_session_id":
            session_id = c["value"]
        elif c["name"] == "_ct_site_id":
            site_id = c["value"]
    return session_id, site_id


async def try_calltouch(page, phone, name=""):
    log = get_logger()
    session_id, site_id = await _get_calltouch_cookies(
        page
    )
    if not session_id or not site_id:
        if log:
            log.warn(
                "calltouch: cookies не найдены "
                f"(session={bool(session_id)}, "
                f"site={bool(site_id)})"
            )
        return None

    if log:
        log.step(
            "calltouch",
            f"site={site_id}, session={session_id[:8]}",
        )

    # Собираем hidden/CSRF-поля исходной формы, чтобы
    # прямой POST нёс все существующие поля (_token/csrf/
    # nonce), а не только phone+name.
    hidden_data = await _collect_hidden_fields(page)
    hidden_fields = hidden_data.get("hidden", {}) or {}
    csrf_names = hidden_data.get("csrf", []) or []
    if log:
        if csrf_names:
            log.step(
                "calltouch",
                f"hidden={len(hidden_fields)}, "
                f"csrf/token поля: "
                f"{', '.join(csrf_names)}",
            )
        elif hidden_fields:
            log.step(
                "calltouch",
                f"hidden={len(hidden_fields)} "
                "(csrf/token не найден)",
            )

    try:
        async with aiohttp.ClientSession(
            timeout=_TIMEOUT
        ) as s:
            async with s.post(
                _LOAD_URL,
                json={
                    "siteId": site_id,
                    "sessionId": session_id,
                    "widgetTypes": ["callback"],
                },
            ) as r:
                load_data = await r.json(
                    content_type=None
                )

            if not load_data:
                if log:
                    log.warn("calltouch: load пустой ответ")
                return None

            show_id = load_data.get("showId")
            widget_id = load_data.get("widgetId")
            unit_id = load_data.get("unitId")

            if not show_id or not widget_id:
                if log:
                    log.warn(
                        "calltouch: нет showId/widgetId"
                    )
                return None

            call_payload = {
                "siteId": site_id,
                "widgetId": widget_id,
                "sessionId": session_id,
                "showId": show_id,
                "phone": phone,
                "name": name,
                "unitId": unit_id,
                "callbackPeriod": "now",
                "personalDataAgreement": True,
            }
            # Добавляем существующие hidden/CSRF-поля формы,
            # не перезатирая ключи Calltouch API.
            for hk, hv in hidden_fields.items():
                if hk not in call_payload:
                    call_payload[hk] = hv

            async with s.post(
                _CALL_URL,
                json=call_payload,
            ) as r2:
                call_data = await r2.json(
                    content_type=None
                )

            if call_data and call_data.get("techNumber"):
                if log:
                    log.ok(
                        "calltouch: звонок заказан, "
                        f"techNumber="
                        f"{call_data['techNumber']}"
                    )
                return {
                    "status": "success",
                    "method": "calltouch_api",
                    "message": (
                        "Calltouch: звонок заказан"
                    ),
                }

            if log:
                log.warn(
                    "calltouch: call ответ без "
                    f"techNumber: "
                    f"{str(call_data)[:120]}"
                )
            return None

    except Exception as e:
        if log:
            log.err(
                "calltouch",
                exc=e,
            )
        return None
