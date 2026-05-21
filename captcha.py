import asyncio

import aiohttp

from config import RUCAPTCHA_IN, RUCAPTCHA_RES
from logger import get_logger

_TIMEOUT = aiohttp.ClientTimeout(total=30)


async def _get_sitekey(page):
    try:
        return await page.evaluate(r"""() => {
            // reCAPTCHA v2/v3
            let el = document.querySelector(
                '.g-recaptcha[data-sitekey]'
            );
            if (el) return {
                type: 'recaptcha',
                key: el.getAttribute('data-sitekey'),
            };
            el = document.querySelector(
                '[data-sitekey]'
            );
            if (el) {
                const k = el.getAttribute(
                    'data-sitekey') || '';
                const cls = (
                    el.className || ''
                ).toLowerCase();
                if (cls.includes('h-captcha'))
                    return {type: 'hcaptcha', key: k};
                if (cls.includes('cf-turnstile'))
                    return {type: 'turnstile', key: k};
                if (cls.includes('smart-captcha')
                    || k.startsWith('ysc1_'))
                    return {type: 'yandex', key: k};
                return {type: 'recaptcha', key: k};
            }
            // hCaptcha
            el = document.querySelector(
                '.h-captcha[data-sitekey]'
            );
            if (el) return {
                type: 'hcaptcha',
                key: el.getAttribute('data-sitekey'),
            };
            // Turnstile
            el = document.querySelector(
                '.cf-turnstile[data-sitekey]'
            );
            if (el) return {
                type: 'turnstile',
                key: el.getAttribute('data-sitekey'),
            };
            // Yandex SmartCaptcha
            el = document.querySelector(
                '[data-sitekey]'
                + '[class*="smart-captcha" i]'
            );
            if (!el) el = document.querySelector(
                '#smartcaptcha,[id*="smart-captcha" i]'
            );
            if (el) {
                const k = el.getAttribute(
                    'data-sitekey'
                );
                if (k) return {
                    type: 'yandex', key: k,
                };
            }
            // Yandex SmartCaptcha: sitekey из iframe
            const scIframes = document.querySelectorAll(
                'iframe[src*="smartcaptcha" i],'
                + 'iframe[src*="captcha-cloud" i],'
                + 'iframe[src*="captcha.yandex" i]'
            );
            for (const f of scIframes) {
                const src = f.src || '';
                const m = src.match(
                    /sitekey=([^&]+)/i
                );
                if (m) return {
                    type: 'yandex', key: m[1],
                };
            }

            // Yandex SmartCaptcha: sitekey из скрипта
            const scScripts = document.querySelectorAll(
                'script[src*="smartcaptcha" i],'
                + 'script[src*="captcha-cloud" i]'
            );
            for (const s of scScripts) {
                const src = s.src || '';
                const m = src.match(
                    /sitekey=([^&]+)/i
                );
                if (m) return {
                    type: 'yandex', key: m[1],
                };
            }

            // invisible / enterprise reCAPTCHA
            const scripts = Array.from(
                document.querySelectorAll('script')
            );
            for (const s of scripts) {
                const src = s.src || '';
                if (src.includes('recaptcha/api.js')
                    || src.includes(
                        'recaptcha/enterprise.js')
                ) {
                    const ent = src.includes(
                        'enterprise.js');
                    const m = src.match(
                        /render=([^&]+)/
                    );
                    if (m && m[1] !== 'explicit')
                        return {
                            type: 'recaptcha',
                            key: m[1],
                            enterprise: ent,
                        };
                }
            }

            // grecaptcha config object
            try {
                if (window.___grecaptcha_cfg) {
                    const cfg =
                        window.___grecaptcha_cfg;
                    const clients = cfg.clients || {};
                    for (const cid of
                        Object.keys(clients)) {
                        const cl = clients[cid];
                        for (const k of
                            Object.keys(cl)) {
                            const v = cl[k];
                            if (!v || typeof v
                                !== 'object') continue;
                            for (const k2 of
                                Object.keys(v)) {
                                const v2 = v[k2];
                                if (!v2 || typeof v2
                                    !== 'object')
                                    continue;
                                const sk = v2.sitekey
                                    || v2.key;
                                if (sk) return {
                                    type: 'recaptcha',
                                    key: sk,
                                };
                            }
                        }
                    }
                }
            } catch(e) {}

            return null;
        }""")
    except Exception:
        return None


async def _solve_smartcaptcha_overlay(
    page, page_url, rucaptcha_key,
):
    """Решает Yandex SmartCaptcha, перекрывающую
    всю страницу (checkbox 'I'm not a robot')."""
    log = get_logger()
    if not rucaptcha_key:
        return None

    sitekey = await page.evaluate(r"""() => {
        // 1. sitekey из iframe src
        const iframes = document.querySelectorAll(
            'iframe');
        for (const f of iframes) {
            const src = (f.src||'').toLowerCase();
            if (!/smartcaptcha|captcha-cloud|captcha\.yandex/
                .test(src)) continue;
            const m = f.src.match(
                /sitekey=([^&]+)/i);
            if (m) return m[1];
        }
        // 2. data-sitekey на контейнере
        const els = document.querySelectorAll(
            '[data-sitekey]');
        for (const el of els) {
            const sig = ((el.className||'')
                + ' ' + (el.id||'')).toLowerCase();
            if (/captcha|smartcaptcha/.test(sig))
                return el.getAttribute('data-sitekey');
        }
        // 3. из inline-скриптов
        const scripts = document.querySelectorAll(
            'script:not([src])');
        for (const s of scripts) {
            const t = s.textContent || '';
            const m = t.match(
                /sitekey['":\s]+['"]([^'"]+)['"]/i);
            if (m && /captcha/i.test(t))
                return m[1];
        }
        return null;
    }""")

    if not sitekey:
        if log:
            log.log_captcha(
                "smartcaptcha_overlay",
                error="sitekey не найден",
            )
        return None

    if log:
        log.log_captcha(
            "smartcaptcha_overlay",
            sitekey=sitekey[:20],
        )

    token = await _solve_captcha(
        "yandex", sitekey,
        page_url, rucaptcha_key,
    )
    if not token:
        return None

    try:
        await page.evaluate(r"""token => {
            // Инжектируем в smart-token input
            const inps = document.querySelectorAll(
                'input[name="smart-token"],'
                + 'input[name="smartCaptchaToken"],'
                + '[name*="captcha-token" i],'
                + 'input[type="hidden"]'
            );
            for (const inp of inps) {
                const nm = (inp.name||'').toLowerCase();
                if (/smart|captcha|token/.test(nm)) {
                    inp.value = token;
                }
            }
            // Callback
            if (window.smartCaptcha) {
                try {
                    window.smartCaptcha
                        .execute();
                } catch(e) {}
            }
        }""", token)

        # Попробуем кликнуть чекбокс
        for sel in [
            'iframe[src*="smartcaptcha"]',
            'iframe[src*="captcha-cloud"]',
        ]:
            frame_el = await page.query_selector(sel)
            if not frame_el:
                continue
            try:
                frame = await frame_el.content_frame()
                if not frame:
                    continue
                cb = await frame.query_selector(
                    'input[type="checkbox"],'
                    '.CheckboxCaptcha-Anchor,'
                    '[class*="checkbox" i],'
                    'button'
                )
                if cb:
                    await cb.click()
                    await asyncio.sleep(2)
            except Exception:
                continue

        # Удаляем overlay
        await page.evaluate(r"""() => {
            const ovs = document.querySelectorAll(
                '[class*="captcha" i],'
                + '[id*="captcha" i]');
            for (const ov of ovs) {
                try {
                    const st = getComputedStyle(ov);
                    if (st.position === 'fixed'
                        || st.position === 'absolute') {
                        const r =
                            ov.getBoundingClientRect();
                        if (r.width > innerWidth * 0.3
                            && r.height
                                > innerHeight * 0.3)
                            ov.style.setProperty(
                                'display','none',
                                'important');
                    }
                } catch(e) {}
            }
        }""")

        if log:
            log.log_captcha(
                "smartcaptcha_solved",
                token=token[:30],
            )
        return "ok"
    except Exception as e:
        if log:
            log.log_captcha(
                "smartcaptcha_inject_error",
                error=str(e)[:80],
            )
        return None


async def _detect_slider_captcha(page, rucaptcha_key):
    """Детектирует и решает slider-капчи
    (передвинуть ползунок)."""
    log = get_logger()
    if not rucaptcha_key:
        return None
    try:
        info = await page.evaluate(r"""() => {
            const sliders = document.querySelectorAll(
                '[class*="slider" i][class*="captcha" i],'
                + '[class*="slide-verify" i],'
                + '[class*="drag" i][class*="captcha" i],'
                + '[class*="slider-captcha" i],'
                + '[class*="geetest" i],'
                + '.nc_wrapper,.nc-container'
            );
            for (const sl of sliders) {
                try {
                    const st = getComputedStyle(sl);
                    if (st.display === 'none')
                        continue;
                    const r =
                        sl.getBoundingClientRect();
                    if (r.width < 50 || r.height < 15)
                        continue;
                    // Ищем ползунок
                    const handle = sl.querySelector(
                        '[class*="handle" i],'
                        + '[class*="btn" i],'
                        + '[class*="drag" i],'
                        + '[class*="knob" i],'
                        + '.nc_iconfont'
                    );
                    if (!handle) continue;
                    const hr =
                        handle.getBoundingClientRect();
                    return {
                        x: Math.round(hr.x + hr.width/2),
                        y: Math.round(hr.y + hr.height/2),
                        track: Math.round(
                            r.width - hr.width),
                        w: Math.round(r.width),
                    };
                } catch(e) {}
            }
            return null;
        }""")
        if not info:
            return None

        if log:
            log.log_captcha(
                "slider_found",
                track=info['track'],
            )

        x = info['x']
        y = info['y']
        track = info['track']

        await page.mouse.move(x, y)
        await asyncio.sleep(0.2)
        await page.mouse.down()
        await asyncio.sleep(0.1)

        steps = 20
        for i in range(1, steps + 1):
            dx = int(track * (i / steps))
            ease = dx + int(
                3 * (0.5 - abs(i/steps - 0.5))
            )
            await page.mouse.move(
                x + ease, y + (i % 3 - 1),
            )
            await asyncio.sleep(0.02 + (i % 5) * 0.01)

        await page.mouse.move(x + track, y)
        await asyncio.sleep(0.1)
        await page.mouse.up()
        await asyncio.sleep(1.5)

        if log:
            log.log_captcha("slider_dragged")
        return "ok"

    except Exception as e:
        if log:
            log.log_captcha(
                "slider_error", error=str(e)[:80],
            )
        return None


async def _solve_captcha(
    captcha_type, sitekey, page_url, rucaptcha_key,
    enterprise=False,
):
    log = get_logger()
    if not rucaptcha_key or not sitekey:
        return None

    method_map = {
        "recaptcha": "userrecaptcha",
        "hcaptcha": "hcaptcha",
        "turnstile": "turnstile",
        "yandex": "yandex",
    }
    method = method_map.get(captcha_type)
    if not method:
        return None

    if log:
        log.log_captcha(
            "submit",
            type=captcha_type,
            sitekey=sitekey[:20],
        )

    key_param = (
        "googlekey" if captcha_type == "recaptcha"
        else "sitekey"
    )
    params = {
        "key": rucaptcha_key,
        "method": method,
        key_param: sitekey,
        "pageurl": page_url,
        "json": 1,
    }
    if enterprise and captcha_type == "recaptcha":
        params["enterprise"] = 1

    try:
        async with aiohttp.ClientSession(
            timeout=_TIMEOUT
        ) as s:
            async with s.post(
                RUCAPTCHA_IN, data=params
            ) as r:
                resp = await r.json(
                    content_type=None
                )
            if resp.get("status") != 1:
                if log:
                    log.log_captcha(
                        "error_submit",
                        error=resp.get(
                            "request", "?"
                        ),
                    )
                return None
            task_id = resp["request"]
            if log:
                log.log_captcha(
                    "task_created",
                    task_id=task_id,
                )

            for attempt in range(30):
                await asyncio.sleep(5)
                async with s.get(
                    RUCAPTCHA_RES,
                    params={
                        "key": rucaptcha_key,
                        "action": "get",
                        "id": task_id,
                        "json": 1,
                    },
                ) as r2:
                    res = await r2.json(
                        content_type=None
                    )
                if res.get("status") == 1:
                    token = res["request"]
                    if log:
                        log.log_captcha(
                            "solved",
                            attempt=attempt,
                            token=token[:30],
                        )
                    return token
                if res.get("request") not in (
                    "CAPCHA_NOT_READY",
                    "CAPTCHA_NOT_READY",
                ):
                    if log:
                        log.log_captcha(
                            "error_poll",
                            error=res.get(
                                "request", "?"
                            ),
                        )
                    return None
    except Exception as e:
        if log:
            log.log_captcha(
                "exception", error=str(e)[:120],
            )
        return None
    return None


async def _inject_captcha_token(
    page, captcha_type, token,
):
    log = get_logger()
    try:
        if captcha_type == "recaptcha":
            await page.evaluate(
                r"""token => {
                // Заполняем ВСЕ textarea с ответом
                const tas = document.querySelectorAll(
                    '#g-recaptcha-response,'
                    + 'textarea[name='
                    + '"g-recaptcha-response"],'
                    + 'textarea[id*="g-recaptcha"]'
                );
                for (const ta of tas) {
                    ta.style.display = '';
                    ta.value = token;
                }
                // data-callback
                try {
                    const cb = (
                        document.querySelector(
                            '.g-recaptcha'
                        ) || {}
                    ).getAttribute(
                        'data-callback'
                    );
                    if (cb && window[cb])
                        window[cb](token);
                } catch(e) {}
                // enterprise callback из ___grecaptcha_cfg
                try {
                    if (window.___grecaptcha_cfg) {
                        const cl = window.___grecaptcha_cfg
                            .clients || {};
                        for (const cid of Object.keys(cl)) {
                            const c = cl[cid];
                            for (const k of Object.keys(c)) {
                                const v = c[k];
                                if (!v || typeof v !== 'object')
                                    continue;
                                for (const k2 of Object.keys(v)) {
                                    const v2 = v[k2];
                                    if (!v2 || typeof v2
                                        !== 'object') continue;
                                    if (v2.callback) {
                                        try { v2.callback(token); }
                                        catch(e2) {}
                                    }
                                }
                            }
                        }
                    }
                } catch(e) {}
            }""", token)

        elif captcha_type == "hcaptcha":
            await page.evaluate(
                r"""token => {
                const ta = document.querySelector(
                    'textarea[name='
                    + '"h-captcha-response"],'
                    + '[name="g-recaptcha-response"]'
                );
                if (ta) {
                    ta.style.display = '';
                    ta.value = token;
                }
                if (typeof hcaptcha !== 'undefined') {
                    try {
                        const cb = (
                            document.querySelector(
                                '.h-captcha'
                            ) || {}
                        ).getAttribute(
                            'data-callback'
                        );
                        if (cb && window[cb])
                            window[cb](token);
                    } catch(e) {}
                }
            }""", token)

        elif captcha_type == "turnstile":
            await page.evaluate(
                r"""token => {
                const inp = document.querySelector(
                    'input[name='
                    + '"cf-turnstile-response"]'
                );
                if (inp) inp.value = token;
                if (typeof turnstile !== 'undefined') {
                    try {
                        const cb = (
                            document.querySelector(
                                '.cf-turnstile'
                            ) || {}
                        ).getAttribute(
                            'data-callback'
                        );
                        if (cb && window[cb])
                            window[cb](token);
                    } catch(e) {}
                }
            }""", token)

        elif captcha_type == "yandex":
            await page.evaluate(
                r"""token => {
                const inp = document.querySelector(
                    'input[name="smart-token"],'
                    + '[name="smartCaptchaToken"]'
                );
                if (inp) inp.value = token;
            }""", token)

        if log:
            log.log_captcha(
                "injected",
                type=captcha_type,
                token=token[:30],
            )
        return True
    except Exception as e:
        if log:
            log.log_captcha(
                "inject_error",
                error=str(e)[:120],
            )
        return False


async def _detect_math_captcha(page):
    log = get_logger()
    try:
        result = await page.evaluate(r"""() => {
            const body = document.body.innerText || '';
            const m = body.match(
                /(\d+)\s*([\+\-\*])\s*(\d+)\s*=\s*\?/
            );
            if (!m) return null;
            const inputs = document.querySelectorAll(
                'input[type="text"], input[type="number"], '
                + 'input:not([type])'
            );
            for (const inp of inputs) {
                const sig = (
                    (inp.name||'') + ' '
                    + (inp.id||'') + ' '
                    + (inp.placeholder||'') + ' '
                    + (inp.className||'')
                ).toLowerCase();
                if (/captcha|code|код|ответ|answer|result|math/
                    .test(sig)) {
                    let sel = null;
                    if (inp.id)
                        sel = '#' + inp.id;
                    else if (inp.name)
                        sel = 'input[name="'+inp.name+'"]';
                    else if (inp.placeholder)
                        sel = 'input[placeholder="'
                            + inp.placeholder + '"]';
                    if (sel) return {
                        a: parseInt(m[1]),
                        op: m[2],
                        b: parseInt(m[3]),
                        sel: sel,
                    };
                }
            }
            return null;
        }""")
        if not result:
            return None
        a, op, b = result["a"], result["op"], result["b"]
        if op == "+":
            answer = a + b
        elif op == "-":
            answer = a - b
        elif op == "*":
            answer = a * b
        else:
            return None
        sel = result["sel"]
        try:
            el = await page.query_selector(sel)
            if el:
                await el.fill(str(answer))
                if log:
                    log.log_captcha(
                        "math_solved",
                        expr=f"{a}{op}{b}={answer}",
                        sel=sel,
                    )
                return "ok"
        except Exception as e:
            if log:
                log.log_captcha(
                    "math_fill_error",
                    error=str(e)[:80],
                )
        return None
    except Exception:
        return None


async def _detect_image_captcha(page, rucaptcha_key):
    log = get_logger()
    if not rucaptcha_key:
        return None
    try:
        info = await page.evaluate(r"""() => {
            function findInput(ctx) {
                if (!ctx) return null;
                const sels = [
                    'input[type="text"]',
                    'input:not([type])',
                    'input[type="number"]',
                ];
                for (const s of sels) {
                    const inp = ctx.querySelector(s);
                    if (!inp) continue;
                    const sig = (
                        (inp.name||'') + ' '
                        + (inp.id||'') + ' '
                        + (inp.placeholder||'')
                    ).toLowerCase();
                    if (/phone|tel|email|имя|name|телефон/
                        .test(sig)) continue;
                    return inp;
                }
                return null;
            }
            function mkSel(el) {
                if (!el) return null;
                if (el.id) return '#' + el.id;
                if (el.name)
                    return el.tagName.toLowerCase()
                        + '[name="'+el.name+'"]';
                if (el.placeholder)
                    return el.tagName.toLowerCase()
                        + '[placeholder="'
                        + el.placeholder + '"]';
                return null;
            }

            // Стратегия 1: img с captcha в атрибутах
            const imgs =
                document.querySelectorAll('img');
            for (const img of imgs) {
                const sig = (
                    (img.className||'') + ' '
                    + (img.id||'') + ' '
                    + (img.alt||'') + ' '
                    + (img.src||'')
                ).toLowerCase();
                if (!/captcha|capcha|security.?image|security.?code|verify.?code|vericode/
                    .test(sig)) continue;
                const r = img.getBoundingClientRect();
                if (r.width < 30 || r.height < 15)
                    continue;
                const p = img.parentElement;
                const pp = p ? p.parentElement : null;
                const ppp = pp ? pp.parentElement : null;
                const inp = findInput(p)
                    || findInput(pp) || findInput(ppp);
                if (!inp) continue;
                let imgSel = img.id
                    ? '#' + img.id : null;
                return {
                    imgSel, inpSel: mkSel(inp),
                    src: img.src,
                };
            }

            // Стратегия 2: текст "введите код" рядом
            //   с картинкой
            const textPat =
                /введите.{0,15}(код|проверочн|captcha)|код.{0,10}картинк|enter.{0,10}(code|captcha)|verification.{0,10}code/i;
            const all = document.querySelectorAll('*');
            for (const el of all) {
                const t = (el.innerText||'')
                    .trim();
                if (t.length > 150 || t.length < 5)
                    continue;
                if (!textPat.test(t)) continue;
                const ctx = el.closest('form')
                    || el.parentElement?.parentElement
                        ?.parentElement
                    || el.parentElement;
                if (!ctx) continue;
                const nearImgs =
                    ctx.querySelectorAll('img');
                for (const img of nearImgs) {
                    const r =
                        img.getBoundingClientRect();
                    if (r.width < 30 || r.height < 15)
                        continue;
                    if (r.width > 500
                        && r.height > 500)
                        continue;
                    const inp = findInput(ctx);
                    if (!inp) continue;
                    let imgSel = img.id
                        ? '#' + img.id : null;
                    return {
                        imgSel, inpSel: mkSel(inp),
                        src: img.src,
                        context: true,
                    };
                }
            }

            return null;
        }""")
        if not info:
            return None
        import base64
        img_el = None
        if info.get("imgSel"):
            img_el = await page.query_selector(
                info["imgSel"]
            )
        if not img_el and info.get("src"):
            try:
                src_esc = info["src"].replace(
                    '"', '\\"'
                )
                img_el = await page.query_selector(
                    f'img[src="{src_esc}"]'
                )
            except Exception:
                pass
        if not img_el:
            return None
        try:
            img_bytes = await img_el.screenshot(
                type="png", timeout=5000,
            )
        except Exception:
            return None
        b64 = base64.b64encode(img_bytes).decode()
        async with aiohttp.ClientSession(
            timeout=_TIMEOUT
        ) as s:
            async with s.post(
                RUCAPTCHA_IN,
                data={
                    "key": rucaptcha_key,
                    "method": "base64",
                    "body": b64,
                    "json": 1,
                },
            ) as r:
                resp = await r.json(content_type=None)
            if resp.get("status") != 1:
                if log:
                    log.log_captcha(
                        "image_submit_error",
                        error=resp.get("request", "?"),
                    )
                return None
            task_id = resp["request"]
            for _ in range(24):
                await asyncio.sleep(5)
                async with s.get(
                    RUCAPTCHA_RES,
                    params={
                        "key": rucaptcha_key,
                        "action": "get",
                        "id": task_id,
                        "json": 1,
                    },
                ) as r2:
                    res = await r2.json(
                        content_type=None
                    )
                if res.get("status") == 1:
                    text = res["request"]
                    inp_el = await page.query_selector(
                        info["inpSel"]
                    )
                    if inp_el:
                        await inp_el.fill(text)
                        if log:
                            log.log_captcha(
                                "image_solved",
                                text=text[:20],
                            )
                        return "ok"
                    return None
                if res.get("request") not in (
                    "CAPCHA_NOT_READY",
                    "CAPTCHA_NOT_READY",
                ):
                    return None
        return None
    except Exception as e:
        if log:
            log.log_captcha(
                "image_error", error=str(e)[:80],
            )
        return None


async def _detect_icon_captcha(page, rucaptcha_key):
    log = get_logger()
    if not rucaptcha_key:
        return None
    try:
        info = await page.evaluate(r"""() => {
            const texts = document.querySelectorAll('*');
            for (const el of texts) {
                const t = (el.innerText || '')
                    .trim().toLowerCase();
                if (!/выберите|select|click/i.test(t))
                    continue;
                if (t.length > 120) continue;
                const parent = el.parentElement;
                if (!parent) continue;
                const imgs = parent.querySelectorAll(
                    'img, [class*="icon"], [class*="captcha"]'
                );
                if (imgs.length < 3) continue;
                const container = parent;
                const r = container.getBoundingClientRect();
                if (r.width < 50 || r.height < 50) continue;
                return {
                    instruction: t.substring(0, 100),
                    x: Math.round(r.x),
                    y: Math.round(r.y),
                    w: Math.round(r.width),
                    h: Math.round(r.height),
                };
            }
            return null;
        }""")
        if not info:
            return None
        import base64
        try:
            img_bytes = await page.screenshot(
                type="png",
                clip={
                    "x": info["x"], "y": info["y"],
                    "width": info["w"],
                    "height": info["h"],
                },
                timeout=5000,
            )
        except Exception:
            return None
        b64 = base64.b64encode(img_bytes).decode()
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60)
        ) as s:
            async with s.post(
                RUCAPTCHA_IN,
                data={
                    "key": rucaptcha_key,
                    "method": "base64",
                    "body": b64,
                    "textinstructions": info[
                        "instruction"
                    ],
                    "json": 1,
                    "coordinatescaptcha": 1,
                },
            ) as r:
                resp = await r.json(content_type=None)
            if resp.get("status") != 1:
                if log:
                    log.log_captcha(
                        "icon_submit_error",
                        error=resp.get("request", "?"),
                    )
                return None
            task_id = resp["request"]
            for _ in range(30):
                await asyncio.sleep(5)
                async with s.get(
                    RUCAPTCHA_RES,
                    params={
                        "key": rucaptcha_key,
                        "action": "get",
                        "id": task_id,
                        "json": 1,
                    },
                ) as r2:
                    res = await r2.json(
                        content_type=None
                    )
                if res.get("status") == 1:
                    raw = res["request"]
                    import re
                    if isinstance(raw, list):
                        coords = [
                            (str(c.get("x", 0)),
                             str(c.get("y", 0)))
                            for c in raw
                            if isinstance(c, dict)
                        ]
                    else:
                        coords_str = str(raw)
                        coords = re.findall(
                            r'x=(\d+),?\s*y=(\d+)',
                            coords_str,
                        )
                    for cx, cy in coords:
                        abs_x = info["x"] + int(cx)
                        abs_y = info["y"] + int(cy)
                        await page.mouse.click(
                            abs_x, abs_y,
                        )
                        await asyncio.sleep(0.3)
                    if log:
                        log.log_captcha(
                            "icon_solved",
                            clicks=len(coords),
                        )
                    return "ok" if coords else None
                if res.get("request") not in (
                    "CAPCHA_NOT_READY",
                    "CAPTCHA_NOT_READY",
                ):
                    return None
        return None
    except Exception as e:
        if log:
            log.log_captcha(
                "icon_error", error=str(e)[:80],
            )
        return None


async def detect_captcha_overlay(page) -> str:
    """Обнаруживает полноэкранные капча-оверлеи.
    Возвращает тип капчи или пустую строку."""
    try:
        return await page.evaluate(r"""() => {
            const body = (
                document.body.innerText || ''
            ).toLowerCase();

            // Yandex SmartCaptcha overlay
            const ySC = document.querySelector(
                '[class*="smart-captcha" i],'
                + '#smartcaptcha,'
                + 'iframe[src*="smartcaptcha" i],'
                + 'iframe[src*="captcha-cloud" i]'
            );
            if (ySC) {
                try {
                    const st = getComputedStyle(ySC);
                    if (st.display !== 'none'
                        && st.visibility !== 'hidden')
                        return 'yandex_smartcaptcha';
                } catch(e) {}
            }

            // "I'm not a robot" / SmartCaptcha text
            if (/i.m not a robot|не робот|check.+box.*human|press to continue/i
                .test(body)) {
                const iframes = document.querySelectorAll(
                    'iframe');
                for (const f of iframes) {
                    const src = (f.src||'').toLowerCase();
                    if (/captcha|smartcaptcha|recaptcha/
                        .test(src)) {
                        const r =
                            f.getBoundingClientRect();
                        if (r.width > 200
                            && r.height > 100)
                            return 'captcha_overlay';
                    }
                }
                const overlays =
                    document.querySelectorAll(
                        '[class*="captcha" i],'
                        + '[id*="captcha" i]'
                    );
                for (const ov of overlays) {
                    try {
                        const st = getComputedStyle(ov);
                        const r =
                            ov.getBoundingClientRect();
                        if (st.display !== 'none'
                            && r.width > 200
                            && r.height > 100)
                            return 'captcha_overlay';
                    } catch(e) {}
                }
            }

            // Визуальная капча: картинка + поле
            const captchaImgs =
                document.querySelectorAll('img');
            for (const img of captchaImgs) {
                const sig = (
                    (img.className||'') + ' '
                    + (img.id||'') + ' '
                    + (img.alt||'') + ' '
                    + (img.src||'')
                ).toLowerCase();
                if (!/captcha|capcha|security.?code|verify.?code/
                    .test(sig)) continue;
                const r = img.getBoundingClientRect();
                if (r.width < 30 || r.height < 15)
                    continue;
                return 'image_captcha';
            }

            // Текст "введите проверочный код"
            if (/введите проверочный код|введите код с картинки|enter.+captcha/i
                .test(body)) {
                return 'image_captcha';
            }

            return '';
        }""")
    except Exception:
        return ""


async def handle_captcha(
    page, page_url, rucaptcha_key,
    has_captcha_hint=False,
    captcha_type_hint=None,
):
    # 1. Математическая капча
    math_res = await _detect_math_captcha(page)
    if math_res == "ok":
        return "ok"

    # 2. Slider-капча
    slider_res = await _detect_slider_captcha(
        page, rucaptcha_key,
    )
    if slider_res == "ok":
        return "ok"

    # 3. Sitekey-капчи (reCAPTCHA, hCaptcha, etc.)
    info = await _get_sitekey(page)

    if not info:
        # 4. SmartCaptcha overlay (без sitekey в DOM)
        cap_overlay = await detect_captcha_overlay(
            page
        )
        if cap_overlay in (
            "yandex_smartcaptcha", "captcha_overlay",
        ):
            sc_res = await _solve_smartcaptcha_overlay(
                page, page_url, rucaptcha_key,
            )
            if sc_res == "ok":
                return "ok"
            if not rucaptcha_key:
                return "no_key"
            return "solve_failed"

        # 5. Картинка с текстом
        img_res = await _detect_image_captcha(
            page, rucaptcha_key,
        )
        if img_res == "ok":
            return "ok"

        # 6. Иконки
        icon_res = await _detect_icon_captcha(
            page, rucaptcha_key,
        )
        if icon_res == "ok":
            return "ok"

    if not info and not has_captcha_hint:
        return None

    if info:
        captcha_type = info["type"]
        sitekey = info["key"]
        is_enterprise = info.get("enterprise", False)
    elif captcha_type_hint:
        captcha_type = captcha_type_hint
        sitekey = None
        return None
    else:
        return None

    if not rucaptcha_key:
        log = get_logger()
        if log:
            log.warn(
                f"Капча {captcha_type} найдена, "
                "но RuCaptcha ключ не указан"
            )
        return "no_key"

    token = await _solve_captcha(
        captcha_type, sitekey,
        page_url, rucaptcha_key,
        enterprise=is_enterprise,
    )
    if not token:
        return "solve_failed"

    ok = await _inject_captcha_token(
        page, captcha_type, token,
    )
    return "ok" if ok else "inject_failed"
