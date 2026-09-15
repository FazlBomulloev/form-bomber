import aiohttp

from logger import get_logger

_TIMEOUT = aiohttp.ClientTimeout(total=20)
_LOAD_URL = "https://mod.calltouch.ru/callback_load.php"
_CALL_URL = "https://mod.calltouch.ru/callback_call.php"

async def _collect_hidden_fields(page):
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
    widget_hash = None
    for c in cookies:
        if c["name"] == "_ct_session_id":
            session_id = c["value"]
        elif c["name"] == "_ct_site_id":
            site_id = c["value"]
        elif c["name"] == "_ct_ids":
            v = c.get("value") or ""
            v = v.replace("%3A", ":")
            parts = v.split(":")
            if parts and parts[0]:
                widget_hash = parts[0]
    if not widget_hash:
        try:
            widget_hash = await page.evaluate(r"""() => {
                for (const s of document.querySelectorAll(
                    'script[src*="calltouch"]')) {
                    const m = (s.src||'').match(
                        /[?&]id=([A-Za-z0-9]+)/);
                    if (m) return m[1];
                }
                for (const k of Object.keys(window)) {
                    const m = k.match(/^ctw_([A-Za-z0-9]+)$/);
                    if (m) return m[1];
                }
                return null;
            }""")
        except Exception:
            widget_hash = None
    return session_id, site_id, widget_hash

async def try_calltouch(page, phone, name=""):
    log = get_logger()
    session_id, site_id, widget_hash = (
        await _get_calltouch_cookies(page)
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
            f"site={site_id}, session={session_id[:8]}"
            + (f", widget={widget_hash}"
               if widget_hash else ""),
        )

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
            load_data = None
            for widget_types in (
                ["callback"],
                ["callback", "request"],
                ["request"],
            ):
                payload = {
                    "siteId": site_id,
                    "sessionId": session_id,
                    "widgetTypes": widget_types,
                }
                if widget_hash:
                    payload["widgetHash"] = widget_hash
                    payload["siteHash"] = widget_hash
                try:
                    async with s.post(
                        _LOAD_URL, json=payload,
                    ) as r:
                        cand = await r.json(
                            content_type=None,
                        )
                except Exception:
                    cand = None
                if not cand:
                    continue
                items = cand
                if isinstance(cand, dict) \
                        and "widgets" in cand:
                    items = cand["widgets"]
                if isinstance(items, list):
                    for it in items:
                        if isinstance(it, dict) \
                                and it.get("widgetId") \
                                and it.get("showId"):
                            load_data = it
                            break
                elif isinstance(cand, dict) \
                        and cand.get("widgetId") \
                        and cand.get("showId"):
                    load_data = cand
                if load_data:
                    break

            if not load_data:
                if log:
                    log.warn(
                        "calltouch: нет showId/widgetId "
                        f"(hash={widget_hash or 'none'})"
                    )
                return None

            show_id = load_data.get("showId")
            widget_id = load_data.get("widgetId")
            unit_id = load_data.get("unitId")

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
