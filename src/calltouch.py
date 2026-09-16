import aiohttp
import asyncio
import random
import time
from urllib.parse import urlparse

from logger import get_logger

_TIMEOUT = aiohttp.ClientTimeout(total=20)
_LOAD_URL = "https://mod.calltouch.ru/callback_load.php"
_CALL_URL = "https://mod.calltouch.ru/callback_call.php"
_EVENT_URL = "https://mod.calltouch.ru/widget_event.php"


def _gen_show_id():
    return random.randint(10_000_000_000, 99_999_999_999)


def _to_int(v, default=0):
    try:
        return int(str(v))
    except Exception:
        return default

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

    live_session_id = None
    try:
        live_session_id = await page.evaluate(r"""() => {
            const seek = (obj, depth) => {
                if (!obj || depth > 3) return null;
                if (typeof obj !== 'object') return null;
                if (obj.sessionId
                    && String(obj.sessionId).length >= 8) {
                    return String(obj.sessionId);
                }
                if (obj.id
                    && String(obj.id).length >= 10) {
                    return String(obj.id);
                }
                for (const k of Object.keys(obj)) {
                    if (/^(_|node|owner|parent)/i.test(k))
                        continue;
                    try {
                        const v = obj[k];
                        if (v && typeof v === 'object') {
                            const r = seek(v, depth + 1);
                            if (r) return r;
                        }
                    } catch(e) {}
                }
                return null;
            };
            for (const k of Object.keys(window)) {
                if (!/^ctw_/.test(k)) continue;
                try {
                    const r = seek(window[k], 0);
                    if (r) return r;
                } catch(e) {}
            }
            try {
                if (window.CalltouchDataObject) {
                    const cdo = window.CalltouchDataObject;
                    const r = seek(cdo, 0);
                    if (r) return r;
                }
            } catch(e) {}
            return null;
        }""")
    except Exception:
        live_session_id = None

    if live_session_id and (
        not session_id
        or len(str(live_session_id)) > len(str(session_id))
    ):
        session_id = live_session_id

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

    site_id_int = _to_int(site_id)
    session_id_int = _to_int(session_id)
    if not site_id_int or not session_id_int:
        if log:
            log.warn(
                "calltouch: siteId/sessionId не int "
                f"(site={site_id}, session={session_id})"
            )
        return None

    page_url = page.url
    parsed = urlparse(page_url)
    origin = (
        f"{parsed.scheme}://{parsed.netloc}"
        if parsed.scheme else ""
    )
    referer = page_url

    session_data = {
        "id": session_id_int,
        "url": page_url,
        "source": "(direct)",
        "medium": "(none)",
        "utmCampaign": "",
        "deviceType": "desktop",
        "pools": [],
        "geoCity": None,
        "geoRegion": None,
        "geoCountry": None,
        "daysSinceLastVisit": 1,
        "geoTimezone": None,
    }

    headers = {
        "accept": "*/*",
        "accept-language": (
            "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"
        ),
        "content-type": "application/json",
        "origin": origin,
        "referer": referer,
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/152.0.0.0 Safari/537.36"
        ),
    }

    try:
        async with aiohttp.ClientSession(
            timeout=_TIMEOUT
        ) as s:
            load_data = None
            last_raw = None
            for widget_types in (
                ["callback", "wheel-fortune"],
                ["callback"],
                ["callback", "request"],
            ):
                payload = {
                    "siteId": site_id_int,
                    "sessionId": session_id_int,
                    "sessionData": session_data,
                    "widgetTypes": widget_types,
                    "isMobileDevice": False,
                    "host": "mod.calltouch.ru",
                    "ctObject": "ct",
                }
                if widget_hash:
                    payload["widgetHash"] = widget_hash
                cand = None
                try:
                    async with s.post(
                        _LOAD_URL,
                        json=payload,
                        headers=headers,
                    ) as r:
                        raw = await r.text()
                        last_raw = f"HTTP {r.status} :: {raw[:300]}"
                        try:
                            import json as _json
                            cand = _json.loads(raw)
                        except Exception:
                            cand = None
                except Exception as e:
                    last_raw = f"exc: {e}"
                    cand = None
                if not cand:
                    continue
                if isinstance(cand, dict) \
                        and cand.get("sessionId") \
                        and not session_id_int:
                    session_id_int = int(cand["sessionId"])
                if isinstance(cand, dict) \
                        and isinstance(
                            cand.get("sessionId"), int,
                        ) \
                        and cand["sessionId"] \
                        != session_id_int:
                    session_id_int = cand["sessionId"]
                widget_settings = None
                if isinstance(cand, dict):
                    widget_settings = (
                        cand.get("widgetSettings")
                        or cand.get("widgets")
                    )
                if isinstance(widget_settings, list):
                    for it in widget_settings:
                        if isinstance(it, dict) \
                                and it.get("widgetId"):
                            load_data = dict(it)
                            load_data.setdefault(
                                "showId",
                                cand.get("showId"),
                            )
                            break
                elif isinstance(cand, dict) \
                        and cand.get("widgetId"):
                    load_data = cand
                if load_data:
                    break

            if not load_data:
                if log:
                    log.warn(
                        "calltouch: нет showId/widgetId "
                        f"(hash={widget_hash or 'none'}) "
                        f"raw={last_raw or 'no-response'}"
                    )
                return None

            show_id = load_data.get("showId") or _gen_show_id()
            widget_id = load_data.get("widgetId")
            unit_id = load_data.get("unitId")
            work_mode = load_data.get(
                "workMode", "working_hours",
            )
            widget_type = load_data.get(
                "widgetType", "callback",
            )

            event_payload = {
                "showId": show_id,
                "siteId": site_id_int,
                "events": [{
                    "object": "widget-button",
                    "action": "show",
                    "data": {
                        "widgetId": widget_id,
                        "widgetType": widget_type,
                        "workMode": work_mode,
                        "siteId": site_id_int,
                        "sessionId": session_id_int,
                        "actionType": "auto",
                        "isMobile": False,
                        "isMultiButton": False,
                    },
                }],
            }
            try:
                async with s.post(
                    _EVENT_URL,
                    json=event_payload,
                    headers=headers,
                ):
                    pass
            except Exception:
                pass
            await asyncio.sleep(0.3)

            call_payload = {
                "siteId": site_id_int,
                "widgetId": widget_id,
                "sessionId": session_id_int,
                "showId": show_id,
                "phone": phone,
                "name": name,
                "unitId": unit_id,
                "callbackPeriod": "now",
                "personalDataAgreement": True,
                "sessionData": session_data,
            }
            for hk, hv in hidden_fields.items():
                if hk not in call_payload:
                    call_payload[hk] = hv

            async with s.post(
                _CALL_URL,
                json=call_payload,
                headers=headers,
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
