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
        // 2b. Любой data-sitekey начинающийся с ysc
        for (const el of els) {
            const k = el.getAttribute('data-sitekey') || '';
            if (k.startsWith('ysc1_')) return k;
        }
        // 3. из inline-скриптов
        const scripts = document.querySelectorAll(
            'script:not([src])');
        for (const s of scripts) {
            const t = s.textContent || '';
            const m = t.match(
                /sitekey['":\s]+['"]([^'"]{10,})['"]/i);
            if (m && /captcha/i.test(t))
                return m[1];
        }
        // 4. Tilda: sitekey из window переменных
        try {
            if (window.tildaForm
                && window.tildaForm.captchaKey)
                return window.tildaForm.captchaKey;
        } catch(e) {}
        // 5. Любой script src с sitekey
        const extScripts = document.querySelectorAll(
            'script[src*="smartcaptcha" i],'
            + 'script[src*="captcha-cloud" i],'
            + 'script[src*="captcha.yandex" i]'
        );
        for (const s of extScripts) {
            const m = (s.src||'').match(
                /sitekey=([^&]+)/i);
            if (m) return m[1];
        }
        // 6. meta-теги
        const meta = document.querySelector(
            'meta[name*="captcha" i][content]');
        if (meta) {
            const c = meta.getAttribute('content');
            if (c && c.length > 10) return c;
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

        # Кликаем чекбокс через все фреймы
        #   (включая вложенные)
        _SC_P = (
            "smartcaptcha", "captcha-cloud",
            "captcha-api", "captcha.yandex",
        )
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                f = frame
                is_sc = False
                while f and f != page.main_frame:
                    if any(
                        p in f.url.lower()
                        for p in _SC_P
                    ):
                        is_sc = True
                        break
                    f = f.parent_frame
                if not is_sc:
                    continue
                cb = await frame.query_selector(
                    'input[type="checkbox"],'
                    '.CheckboxCaptcha-Anchor,'
                    '[class*="checkbox" i],'
                    '[role="checkbox"],'
                    'button'
                )
                if cb:
                    await cb.click()
                    await asyncio.sleep(2)
                    break
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
    enterprise=False, max_tries=4,
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

    for solve_try in range(max_tries):
        if log:
            log.log_captcha(
                "submit",
                type=captcha_type,
                sitekey=sitekey[:20],
                try_no=solve_try + 1,
            )
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
                    err_code = res.get("request", "")
                    if err_code not in (
                        "CAPCHA_NOT_READY",
                        "CAPTCHA_NOT_READY",
                    ):
                        if log:
                            log.log_captcha(
                                "error_poll",
                                error=err_code or "?",
                            )
                        if (
                            err_code
                            == "ERROR_CAPTCHA_UNSOLVABLE"
                            and solve_try + 1 < max_tries
                        ):
                            if log:
                                log.log_captcha(
                                    "retry_unsolvable",
                                )
                            await asyncio.sleep(3)
                            break
                        return None
                else:
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
            const patterns = [
                /(\d+)\s*([\+\-\*×xх])\s*(\d+)\s*=\s*\??/,
                /сколько\s+(?:будет\s+)?(\d+)\s*([\+\-\*×xх])\s*(\d+)/i,
                /решите[:\s]*(\d+)\s*([\+\-\*×xх])\s*(\d+)/i,
                /введите\s+(?:результат|ответ)[:\s]*(\d+)\s*([\+\-\*×xх])\s*(\d+)/i,
                /(\d+)\s*(плюс|минус|умножить)\s+(\d+)/i,
                /(?:пример|задача)[:\s]*(\d+)\s*([\+\-\*×xх])\s*(\d+)/i,
            ];
            let a, op, b;
            let found = false;
            for (const pat of patterns) {
                const m = body.match(pat);
                if (!m) continue;
                a = parseInt(m[1]);
                let rawOp = m[2].toLowerCase();
                if (rawOp === 'плюс' || rawOp === '+') op = '+';
                else if (rawOp === 'минус' || rawOp === '-') op = '-';
                else if (rawOp === 'умножить' || /[*×xх]/.test(rawOp)) op = '*';
                else op = rawOp;
                b = parseInt(m[3]);
                found = true;
                break;
            }
            if (!found) return null;

            function mkSel(inp) {
                if (inp.id) return '#' + inp.id;
                if (inp.name)
                    return 'input[name="'+inp.name+'"]';
                if (inp.placeholder)
                    return 'input[placeholder="'
                        + inp.placeholder + '"]';
                return null;
            }
            function isVis(el) {
                try {
                    const st = getComputedStyle(el);
                    if (st.display === 'none'
                        || st.visibility === 'hidden') return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 10 && r.height > 10;
                } catch(e) { return false; }
            }
            const skipRe = /phone|tel|email|имя|name|телефон|почт|фамили|отчеств|фио|comment|сообщ/i;
            const inputs = document.querySelectorAll(
                'input[type="text"], input[type="number"], '
                + 'input:not([type])'
            );
            // Сначала ищем по captcha-признакам
            for (const inp of inputs) {
                if (!isVis(inp)) continue;
                const sig = (
                    (inp.name||'') + ' '
                    + (inp.id||'') + ' '
                    + (inp.placeholder||'') + ' '
                    + (inp.className||'')
                ).toLowerCase();
                if (/captcha|code|код|ответ|answer|result|math|quiz|квиз|пример/
                    .test(sig)) {
                    const sel = mkSel(inp);
                    if (sel) return {a, op, b, sel};
                }
            }
            // Fallback: любой видимый пустой input не phone/email/name
            for (const inp of inputs) {
                if (!isVis(inp)) continue;
                const sig = (
                    (inp.name||'') + ' '
                    + (inp.id||'') + ' '
                    + (inp.placeholder||'')
                ).toLowerCase();
                if (skipRe.test(sig)) continue;
                if ((inp.value||'').trim()) continue;
                const sel = mkSel(inp);
                if (sel) return {a, op, b, sel};
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

            // findCaptchaInput: ищет input для captcha
            // по name/id/class, содержащим "captcha"/"cap"
            function findCaptchaInput(ctx) {
                if (!ctx) return null;
                const sels = [
                    'input[name*="captcha" i]',
                    'input[id*="captcha" i]',
                    'input[class*="captcha" i]',
                    'input[name="cap"]',
                    'input[name*="capcha" i]',
                    'input[name*="verify" i]',
                    'input[name*="security_code" i]',
                ];
                for (const s of sels) {
                    const inp = ctx.querySelector(s);
                    if (inp && inp.type !== 'hidden')
                        return inp;
                }
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
                // сначала ищем captcha-специфичный input
                // в ближайшей форме
                const form = img.closest('form');
                let inp = findCaptchaInput(form)
                    || findCaptchaInput(p)
                    || findCaptchaInput(pp)
                    || findCaptchaInput(ppp);
                // fallback: общий поиск текстового input
                if (!inp) {
                    inp = findInput(p)
                        || findInput(pp)
                        || findInput(ppp)
                        || findInput(form);
                }
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
                    const inp = findCaptchaInput(ctx)
                        || findInput(ctx);
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


async def _try_click_smartcaptcha(page):
    """Пробует кликнуть чекбокс SmartCaptcha
    напрямую (iframe, вложенный iframe, или DOM)."""
    log = get_logger()

    _SC_PAT = (
        "smartcaptcha", "captcha-cloud",
        "captcha-api", "captcha.yandex",
        "captcha.ya.net", "tildaapi",
    )
    _CB_SEL = (
        'input[type="checkbox"],'
        '.CheckboxCaptcha-Anchor,'
        '[class*="checkbox" i],'
        'button[class*="check" i],'
        '[role="checkbox"],'
        '[data-testid="checkbox"],'
        '.CheckboxCaptcha-Button'
    )

    # 1. Обход ВСЕХ фреймов (включая вложенные)
    #    через page.frames — решает проблему
    #    Tilda SmartCaptcha с вложенными iframe.
    #    Retry до 3 раз с паузой, т.к. iframe капчи
    #    может ещё грузиться после submit.
    for attempt in range(3):
        if attempt > 0:
            await asyncio.sleep(2)
        sc_frame_found = False
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                f = frame
                is_sc = False
                while f and f != page.main_frame:
                    if any(
                        p in f.url.lower()
                        for p in _SC_PAT
                    ):
                        is_sc = True
                        break
                    f = f.parent_frame
                if not is_sc:
                    try:
                        is_sc = await frame.evaluate(
                            r"""() => {
                            const t = (
                                document.body.innerText
                                ||''
                            ).toLowerCase();
                            return /не робот|not a robot|я не робот/
                                .test(t);
                        }""")
                    except Exception:
                        pass
                if not is_sc:
                    continue
                sc_frame_found = True
                cb = await frame.query_selector(
                    _CB_SEL
                )
                if not cb:
                    try:
                        cb = await frame.wait_for_selector(
                            _CB_SEL, timeout=3000,
                            state="visible",
                        )
                    except Exception:
                        pass
                if cb:
                    await cb.click()
                    if log:
                        log.log_captcha(
                            "smartcaptcha_checkbox_click",
                            frame_url=frame.url[:60],
                        )
                    await asyncio.sleep(3)
                    return True
            except Exception:
                continue
        if not sc_frame_found:
            break

    # 2. Ищем по тексту в DOM
    try:
        clicked = await page.evaluate(r"""() => {
            const all = document.querySelectorAll(
                'input[type="checkbox"],'
                + '[role="checkbox"],'
                + '[class*="Checkbox" i],'
                + '[class*="captcha" i] input');
            for (const el of all) {
                try {
                    const r = el.getBoundingClientRect();
                    if (r.width < 5 || r.height < 5)
                        continue;
                    const parent = el.closest(
                        '[class*="captcha" i],'
                        + '[id*="captcha" i]');
                    if (parent) {
                        el.click();
                        return true;
                    }
                } catch(e) {}
            }
            const caps = document.querySelectorAll(
                '[class*="CheckboxCaptcha" i],'
                + '[class*="smartcaptcha" i] label,'
                + '[class*="smart-captcha" i] label');
            for (const c of caps) {
                try {
                    const r = c.getBoundingClientRect();
                    if (r.width > 10 && r.height > 10) {
                        c.click();
                        return true;
                    }
                } catch(e) {}
            }
            return false;
        }""")
        if clicked:
            if log:
                log.log_captcha(
                    "smartcaptcha_dom_click",
                )
            await asyncio.sleep(3)
            return True
    except Exception:
        pass

    return False


async def _extract_smartcaptcha_sitekey(page):
    """Ищет sitekey SmartCaptcha в Tilda и
    других CMS."""
    try:
        return await page.evaluate(r"""() => {
            // 1. iframe src
            for (const f of document.querySelectorAll(
                'iframe')) {
                const src = (f.src||'');
                if (!/smartcaptcha|captcha-cloud|captcha-api|captcha\.yandex/i
                    .test(src)) continue;
                const m = src.match(
                    /sitekey=([^&]+)/i);
                if (m) return m[1];
            }
            // 2. data-sitekey
            for (const el of document.querySelectorAll(
                '[data-sitekey]')) {
                const k = el.getAttribute('data-sitekey');
                if (!k || k.startsWith('6L')) continue;
                return k;
            }
            // 3. Tilda form config
            const tForms = document.querySelectorAll(
                '[data-tilda-captchakey]');
            for (const f of tForms) {
                const k = f.getAttribute(
                    'data-tilda-captchakey');
                if (k) return k;
            }
            // 4. Tilda: smartcaptcha render container
            const scCont = document.querySelector(
                '#smartcaptcha,'
                + '[id*="smartcaptcha" i],'
                + '.t-form__captcha-wrapper,'
                + '[class*="t-captcha" i],'
                + '[class*="captchabox" i]');
            if (scCont) {
                const k = scCont.getAttribute(
                    'data-sitekey');
                if (k) return k;
            }
            // 5. inline scripts
            for (const s of document.querySelectorAll(
                'script:not([src])')) {
                const t = s.textContent || '';
                if (!/captcha/i.test(t)) continue;
                const m = t.match(
                    /sitekey['":\s]+['"]([^'"]{10,})['"]/i);
                if (m && !m[1].startsWith('6L'))
                    return m[1];
            }
            // 6. window variables
            try {
                if (window.tildaForm
                    && window.tildaForm.captchaKey)
                    return window.tildaForm.captchaKey;
            } catch(e) {}
            try {
                if (window.smartCaptcha
                    && window.smartCaptcha._sitekey)
                    return window.smartCaptcha._sitekey;
            } catch(e) {}
            // 7. script src
            for (const s of document.querySelectorAll(
                'script[src*="captcha" i]')) {
                const m = (s.src||'').match(
                    /sitekey=([^&]+)/i);
                if (m) return m[1];
            }
            // 8. data-captcha-key
            const dck = document.querySelector(
                '[data-captcha-key]');
            if (dck) {
                const v = dck.getAttribute(
                    'data-captcha-key');
                if (v && v.length > 5
                    && !v.startsWith('6L'))
                    return v;
            }
            // 9. Tilda: form attrs with captcha
            for (const f of document.querySelectorAll(
                'form')) {
                for (const attr of f.attributes) {
                    const nm = attr.name.toLowerCase();
                    if (!/captcha/.test(nm)) continue;
                    const v = attr.value;
                    if (v && v.length > 5
                        && !v.startsWith('6L'))
                        return v;
                }
            }
            // 10. captchaKey в inline-скриптах
            for (const s of document.querySelectorAll(
                'script:not([src])')) {
                const t = s.textContent || '';
                const m = t.match(
                    /captchaKey['":\s]+['"]([^'"]{10,})['"]/i);
                if (m && !m[1].startsWith('6L'))
                    return m[1];
            }
            // 11. SmartCaptcha render container
            //     с data-sitekey
            for (const el of document.querySelectorAll(
                'div[id^="smartcaptcha"],'
                + 'div[id^="smart-captcha"],'
                + '[class*="smartcaptcha" i],'
                + '[class*="smart-captcha" i],'
                + '.t-form__captcha-wrapper')) {
                for (const attr of el.attributes) {
                    const v = attr.value;
                    if (v && v.length > 10
                        && v.length < 100
                        && !v.startsWith('6L')
                        && /^[a-zA-Z0-9_-]+$/
                            .test(v))
                        return v;
                }
            }
            return null;
        }""")
    except Exception:
        pass

    # Fallback: ищем sitekey в URL вложенных фреймов
    #   (page.frames обходит все уровни вложенности)
    import re as _re
    try:
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            url = frame.url or ""
            if not _re.search(
                r"smartcaptcha|captcha-cloud|"
                r"captcha-api|captcha\.yandex",
                url, _re.IGNORECASE,
            ):
                continue
            m = _re.search(
                r"sitekey=([^&]+)", url, _re.IGNORECASE,
            )
            if m:
                return m.group(1)
    except Exception:
        pass

    # Fallback 2: Tilda captcha iframe
    # (forms.tildaapi.com/procces/captcha/) —
    # sitekey внутри DOM cross-origin iframe,
    # но Playwright может обращаться к фреймам напрямую
    try:
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            furl = (frame.url or "").lower()
            if "tildaapi" not in furl:
                continue
            try:
                sk = await frame.evaluate(r"""() => {
                    // data-sitekey на контейнере SmartCaptcha
                    const el = document.querySelector(
                        '[data-sitekey]');
                    if (el) return el.getAttribute(
                        'data-sitekey');
                    // SmartCaptcha widget container
                    const sc = document.querySelector(
                        '#smartcaptcha,'
                        + '[id*="smartcaptcha" i],'
                        + '[class*="smart-captcha" i],'
                        + '[class*="smartcaptcha" i]');
                    if (sc) {
                        const k = sc.getAttribute(
                            'data-sitekey');
                        if (k) return k;
                    }
                    // iframe внутри Tilda captcha iframe
                    for (const f of document.querySelectorAll(
                        'iframe')) {
                        const src = (f.src || '');
                        if (/smartcaptcha|captcha/i
                            .test(src)) {
                            const m = src.match(
                                /sitekey=([^&]+)/i);
                            if (m) return m[1];
                        }
                    }
                    // inline scripts внутри iframe
                    for (const s of document.querySelectorAll(
                        'script:not([src])')) {
                        const t = s.textContent || '';
                        const m = t.match(
                            /sitekey['":\s]+['"]([^'"]{10,})['"]/i);
                        if (m) return m[1];
                    }
                    return null;
                }""")
                if sk:
                    return sk
            except Exception:
                pass
            # вложенные фреймы внутри tildaapi фрейма
            for sub in frame.child_frames:
                sub_url = (sub.url or "").lower()
                m = _re.search(
                    r"sitekey=([^&]+)",
                    sub_url, _re.IGNORECASE,
                )
                if m:
                    return m.group(1)
                try:
                    sk2 = await sub.evaluate(r"""() => {
                        const el = document.querySelector(
                            '[data-sitekey]');
                        if (el) return el.getAttribute(
                            'data-sitekey');
                        return null;
                    }""")
                    if sk2:
                        return sk2
                except Exception:
                    pass
    except Exception:
        pass

    return None


async def _handle_tilda_needcaptcha(
    page, page_url, rucaptcha_key,
):
    """Специальная обработка Tilda needcaptcha:
    ждём popup SmartCaptcha, решаем, вызываем Tilda
    re-submit."""
    log = get_logger()

    for wait in range(6):
        await asyncio.sleep(2)
        try:
            popup_ready = await page.evaluate(r"""() => {
                // Tilda SmartCaptcha popup
                const sels = [
                    '.t-form__captcha-error',
                    '[class*="t-captcha" i]',
                    '.t-popup[style*="display: block"]',
                    '.t-popup[style*="display:block"]',
                    '.t-popup.t-popup_show',
                ];
                for (const s of sels) {
                    const el = document.querySelector(s);
                    if (el) {
                        try {
                            const st = getComputedStyle(el);
                            if (st.display !== 'none'
                                && st.visibility !== 'hidden')
                                return 'tilda_popup';
                        } catch(e) {}
                    }
                }
                // SmartCaptcha iframe
                for (const f of document.querySelectorAll(
                    'iframe')) {
                    const src = (f.src||'').toLowerCase();
                    if (/smartcaptcha|captcha-cloud|captcha-api|captcha\.yandex|captcha\.ya\.net|tildaapi/
                        .test(src)) return 'sc_iframe';
                }
                // Tilda captcha box
                const tcb = document.querySelector(
                    '#tildaformcaptchabox,'
                    + '#captchaIframeBox');
                if (tcb) {
                    try {
                        const st = getComputedStyle(tcb);
                        if (st.display !== 'none')
                            return 'tilda_captchabox';
                    } catch(e) {}
                }
                // SmartCaptcha render container
                const sc = document.querySelector(
                    'div[id^="smartcaptcha-"],'
                    + 'div[id^="smart-captcha-"],'
                    + '#smartcaptcha,'
                    + '[class*="smartcaptcha" i],'
                    + '[class*="CheckboxCaptcha" i]');
                if (sc) return 'sc_container';
                // Tilda text patterns
                const body = (
                    document.body.innerText || ''
                ).toLowerCase();
                if (/поставьте галочку|check the box|не робот|not a robot|smartcaptcha/i
                    .test(body)) return 'sc_text';
                return null;
            }""")
        except Exception:
            return None

        if not popup_ready:
            continue

        if log:
            log.log_captcha(
                "tilda_needcaptcha_detected",
                signal=popup_ready,
            )

        clicked = await _try_click_smartcaptcha(page)
        if clicked:
            await asyncio.sleep(3)
            # Проверяем решена ли капча:
            #   checkbox checked во вложенных фреймах
            sc_solved = False
            for fr in page.frames:
                if fr == page.main_frame:
                    continue
                try:
                    checked = await fr.evaluate(
                        r"""() => {
                        const cb = document.querySelector(
                            'input[type="checkbox"]');
                        return cb && cb.checked;
                    }""")
                    if checked:
                        sc_solved = True
                        break
                except Exception:
                    continue
            still = not sc_solved
            if still:
                still = await page.evaluate(r"""() => {
                    for (const f of document
                        .querySelectorAll('iframe')) {
                        const src = (
                            f.src||''
                        ).toLowerCase();
                        if (/smartcaptcha|captcha-cloud|tildaapi/
                            .test(src)) {
                            const r = f
                                .getBoundingClientRect();
                            if (r.width > 50
                                && r.height > 30)
                                return true;
                        }
                    }
                    // Tilda captcha box
                    const tcb = document.querySelector(
                        '#tildaformcaptchabox');
                    if (tcb) {
                        try {
                            const st = getComputedStyle(
                                tcb);
                            if (st.display !== 'none')
                                return true;
                        } catch(e) {}
                    }
                    return false;
                }""")
            if not still:
                if log:
                    log.log_captcha(
                        "tilda_sc_click_passed",
                    )
                # Tilda auto-resubmits after checkbox,
                # then shows success, then may redirect
                orig_base = page_url.split('#')[0] \
                    .split('?')[0].rstrip('/')
                for _aw in range(3):
                    await asyncio.sleep(2)
                    # Check 1: page redirected
                    try:
                        cur = page.url.split('#')[0] \
                            .split('?')[0].rstrip('/')
                        if cur != orig_base:
                            if log:
                                log.log_captcha(
                                    "tilda_auto_resubmitted",
                                    redirect=page.url[:60],
                                )
                            return "tilda_auto_submitted"
                    except Exception:
                        return "tilda_auto_submitted"
                    # Check 2: Tilda success box visible
                    try:
                        has_suc = await page.evaluate(
                            r"""() => {
                            const sbs = document
                                .querySelectorAll(
                                '.t-form__successbox');
                            for (const sb of sbs) {
                                try {
                                    const st =
                                        getComputedStyle(sb);
                                    if (st.display !== 'none')
                                        return true;
                                } catch(e) {}
                            }
                            const cb = document.querySelector(
                                '#tildaformcaptchabox');
                            if (cb) {
                                try {
                                    const st =
                                        getComputedStyle(cb);
                                    if (st.display === 'none')
                                        return true;
                                } catch(e) {}
                            }
                            return false;
                        }""")
                        if has_suc:
                            if log:
                                log.log_captcha(
                                    "tilda_auto_resubmitted",
                                    signal="success_text",
                                )
                            return "tilda_auto_submitted"
                    except Exception:
                        return "tilda_auto_submitted"
                    # Check 3: XHR has OK response
                    try:
                        auto_ok = await page.evaluate(
                            r"""() => {
                            const xhr =
                                window.__fbXHR || [];
                            for (const e of xhr) {
                                const b = (e.b || '');
                                if (/"message"\s*:\s*"OK"/i
                                    .test(b)) return true;
                                if (e.s >= 200
                                    && e.s < 300
                                    && /"ok"/i.test(b)
                                    && !/needcaptcha/i
                                        .test(b))
                                    return true;
                            }
                            return false;
                        }""")
                        if auto_ok:
                            if log:
                                log.log_captcha(
                                    "tilda_auto_resubmitted",
                                    signal="xhr_ok",
                                )
                            return "tilda_auto_submitted"
                    except Exception:
                        pass
                # Fallback: Tilda didn't auto-submit
                try:
                    await page.evaluate(r"""() => {
                        const btn = document.querySelector(
                            'button[type="submit"].t-submit,'
                            + '.t-form__submit button,'
                            + 'button.t-submit');
                        if (btn) btn.click();
                    }""")
                except Exception:
                    pass
                return "ok"

        if not rucaptcha_key:
            if log:
                log.log_captcha(
                    "tilda_no_rucaptcha_key",
                )
            return None

        sitekey = await _extract_smartcaptcha_sitekey(
            page,
        )
        if not sitekey:
            # Tilda-specific: из data-tilda-captchakey
            try:
                sitekey = await page.evaluate(r"""() => {
                    const el = document.querySelector(
                        '[data-tilda-captchakey]');
                    if (el) return el.getAttribute(
                        'data-tilda-captchakey');
                    // Tilda stores captcha key on the form
                    const forms = document.querySelectorAll(
                        'form[data-tilda-req],'
                        + 'form.t-form');
                    for (const f of forms) {
                        for (const attr of f.attributes) {
                            if (/captcha/i.test(attr.name)
                                && attr.value.length > 8)
                                return attr.value;
                        }
                    }
                    return null;
                }""")
            except Exception:
                pass

        if not sitekey:
            if log:
                log.log_captcha(
                    "tilda_no_sitekey",
                )
            return None

        if log:
            log.log_captcha(
                "tilda_sitekey_found",
                sitekey=sitekey[:20],
            )

        token = await _solve_captcha(
            "yandex", sitekey,
            page_url, rucaptcha_key,
        )
        if not token:
            return "solve_failed"

        # Inject token into Tilda's form
        try:
            await page.evaluate(r"""t => {
                // Standard SmartCaptcha hidden inputs
                const inps = document.querySelectorAll(
                    'input[name="smart-token"],'
                    + 'input[name="smartCaptchaToken"],'
                    + '[name*="captcha-token" i],'
                    + '[name*="captcha" i][type="hidden"]');
                for (const i of inps) i.value = t;
                // Tilda: hidden input in active form
                const forms = document.querySelectorAll(
                    'form.t-form');
                for (const f of forms) {
                    let inp = f.querySelector(
                        'input[name="smart-token"]');
                    if (!inp) {
                        inp = document.createElement('input');
                        inp.type = 'hidden';
                        inp.name = 'smart-token';
                        f.appendChild(inp);
                    }
                    inp.value = t;
                }
                // SmartCaptcha callback
                if (window.smartCaptcha) {
                    try { window.smartCaptcha.execute(); }
                    catch(e) {}
                }
            }""", token)
        except Exception:
            pass

        if log:
            log.log_captcha(
                "tilda_token_injected",
                token=token[:30],
            )

        # Tilda: trigger form re-submit
        try:
            await page.evaluate(r"""() => {
                const btn = document.querySelector(
                    'button[type="submit"].t-submit,'
                    + '.t-form__submit button,'
                    + 'button.t-submit');
                if (btn) btn.click();
            }""")
        except Exception:
            pass
        await asyncio.sleep(2)
        return "ok"

    if not rucaptcha_key:
        if log:
            log.log_captcha(
                "tilda_nc_no_popup_no_key",
            )
        return None

    if log:
        log.log_captcha(
            "tilda_nc_no_popup_fallback",
        )

    sitekey = await _extract_smartcaptcha_sitekey(
        page,
    )
    if not sitekey:
        try:
            sitekey = await page.evaluate(r"""() => {
                // data-tilda-captchakey
                const el = document.querySelector(
                    '[data-tilda-captchakey]');
                if (el) return el.getAttribute(
                    'data-tilda-captchakey');
                // form attributes with captcha
                const forms = document.querySelectorAll(
                    'form[data-tilda-req],'
                    + 'form.t-form');
                for (const f of forms) {
                    for (const attr of f.attributes) {
                        if (/captcha/i.test(attr.name)
                            && attr.value.length > 8)
                            return attr.value;
                    }
                }
                // Tilda global
                try {
                    if (window.tildaForm
                        && window.tildaForm.captchaKey)
                        return window.tildaForm.captchaKey;
                } catch(e) {}
                // data-captcha-key
                const ck = document.querySelector(
                    '[data-captcha-key]');
                if (ck) {
                    const v = ck.getAttribute(
                        'data-captcha-key');
                    if (v && v.length > 5)
                        return v;
                }
                // SmartCaptcha script src
                for (const s of document.querySelectorAll(
                    'script[src]')) {
                    const src = s.src || '';
                    if (!/captcha/i.test(src))
                        continue;
                    const m = src.match(
                        /sitekey=([^&]+)/i);
                    if (m) return m[1];
                }
                // Inline script: sitekey
                for (const s of document.querySelectorAll(
                    'script:not([src])')) {
                    const t = s.textContent || '';
                    if (!/captcha/i.test(t))
                        continue;
                    const m = t.match(
                        /sitekey['":\s]+['"]([^'"]{10,})['"]/i);
                    if (m && !m[1].startsWith('6L'))
                        return m[1];
                    const m2 = t.match(
                        /captchaKey['":\s]+['"]([^'"]{10,})['"]/i);
                    if (m2 && !m2[1].startsWith('6L'))
                        return m2[1];
                }
                return null;
            }""")
        except Exception:
            pass

    if not sitekey:
        if log:
            log.log_captcha(
                "tilda_nc_fallback_no_sitekey",
            )
        return None

    if log:
        log.log_captcha(
            "tilda_nc_fallback_sitekey",
            sitekey=sitekey[:20],
        )

    token = await _solve_captcha(
        "yandex", sitekey,
        page_url, rucaptcha_key,
    )
    if not token:
        return "solve_failed"

    try:
        await page.evaluate(r"""t => {
            const inps = document.querySelectorAll(
                'input[name="smart-token"],'
                + 'input[name="smartCaptchaToken"],'
                + '[name*="captcha-token" i],'
                + '[name*="captcha" i][type="hidden"]');
            for (const i of inps) i.value = t;
            const forms = document.querySelectorAll(
                'form.t-form');
            for (const f of forms) {
                let inp = f.querySelector(
                    'input[name="smart-token"]');
                if (!inp) {
                    inp = document.createElement('input');
                    inp.type = 'hidden';
                    inp.name = 'smart-token';
                    f.appendChild(inp);
                }
                inp.value = t;
            }
            if (window.smartCaptcha) {
                try { window.smartCaptcha.execute(); }
                catch(e) {}
            }
        }""", token)
    except Exception:
        pass

    try:
        await page.evaluate(r"""() => {
            const btn = document.querySelector(
                'button[type="submit"].t-submit,'
                + '.t-form__submit button,'
                + 'button.t-submit');
            if (btn) btn.click();
        }""")
    except Exception:
        pass

    if log:
        log.log_captcha(
            "tilda_nc_fallback_injected",
            token=token[:30],
        )

    await asyncio.sleep(2)
    return "ok"


async def handle_post_submit_captcha(
    page, page_url, rucaptcha_key,
):
    """Обработка капчи, появляющейся ПОСЛЕ submit.
    Ждёт появления виджета SmartCaptcha/reCAPTCHA,
    math-квиза и т.д."""
    log = get_logger()

    for wait_round in range(8):
        await asyncio.sleep(2.5 if wait_round < 3 else 2)

        try:
            _ = await page.evaluate("() => 1")
        except Exception:
            if log:
                log.log_captcha(
                    "post_submit_page_lost",
                )
            return None

        math_res = await _detect_math_captcha(page)
        if math_res == "ok":
            return "ok"

        # SmartCaptcha: сначала кликаем чекбокс
        has_sc = await page.evaluate(r"""() => {
            const body = (
                document.body.innerText || ''
            ).toLowerCase();
            if (/i.m not a robot|not a robot|press to continue|smartcaptcha|не робот|поставьте галочку|check the box|let us know you.re human|нажмите.{0,5}чтобы продолжить|я не робот/i
                .test(body)) return true;
            for (const f of document.querySelectorAll(
                'iframe')) {
                const src = (f.src||'').toLowerCase();
                if (/smartcaptcha|captcha-cloud|captcha-api|captcha\.yandex|captcha\.ya\.net|tildaapi/
                    .test(src)) return true;
            }
            const sc = document.querySelector(
                '[class*="smartcaptcha" i],'
                + '[class*="smart-captcha" i],'
                + '#smartcaptcha,'
                + '[class*="CheckboxCaptcha" i],'
                + '.t-form__captcha-wrapper,'
                + '[class*="captchabox" i],'
                + '#tildaformcaptchabox,'
                + '#captchaIframeBox,'
                + '.t-form__captcha-error,'
                + '[class*="t-captcha" i],'
                + '[data-tilda-captchakey],'
                + '.t-popup[style*="display: block"],'
                + '.t-popup[style*="display:block"],'
                + '.t-popup.t-popup_show');
            if (sc) return true;
            // Tilda: SmartCaptcha render container
            if (document.querySelector(
                'div[id^="smartcaptcha-"],'
                + 'div[id^="smart-captcha-"],'
                + '[data-captcha-key]'))
                return true;
            // Tilda: SmartCaptcha script loaded
            if (document.querySelector(
                'script[src*="smartcaptcha" i],'
                + 'script[src*="captcha-api" i],'
                + 'script[src*="captcha.yandex" i]'))
                return true;
            return false;
        }""")

        if has_sc:
            if log:
                log.log_captcha(
                    "post_submit_smartcaptcha_detected",
                )
            clicked = await _try_click_smartcaptcha(
                page,
            )
            if clicked:
                await asyncio.sleep(2)
                # Проверяем: капча исчезла?
                # Проверяем и body, и iframe src
                still = await page.evaluate(r"""() => {
                    for (const f of document
                        .querySelectorAll('iframe')) {
                        const src = (
                            f.src||''
                        ).toLowerCase();
                        if (/smartcaptcha|captcha-cloud|captcha-api|captcha\.yandex|tildaapi/
                            .test(src)) {
                            const r = f
                                .getBoundingClientRect();
                            if (r.width > 50
                                && r.height > 30)
                                return true;
                        }
                    }
                    // Tilda captcha box
                    const tcb = document.querySelector(
                        '#tildaformcaptchabox');
                    if (tcb) {
                        try {
                            const st = getComputedStyle(tcb);
                            if (st.display !== 'none')
                                return true;
                        } catch(e) {}
                    }
                    const body = (
                        document.body.innerText||''
                    ).toLowerCase();
                    return /i.m not a robot|press to continue|smartcaptcha/i
                        .test(body);
                }""")
                # Дополнительно: есть ли ещё
                #   непрочеканный checkbox во фреймах
                if still:
                    sc_gone = False
                    for fr in page.frames:
                        if fr == page.main_frame:
                            continue
                        try:
                            checked = await fr.evaluate(
                                r"""() => {
                                const cb = document
                                    .querySelector(
                                    'input[type="checkbox"]');
                                return cb && cb.checked;
                            }""")
                            if checked:
                                sc_gone = True
                                break
                        except Exception:
                            continue
                    if sc_gone:
                        still = False
                if not still:
                    if log:
                        log.log_captcha(
                            "smartcaptcha_click_passed",
                        )
                    orig_b = page_url.split('#')[0] \
                        .split('?')[0].rstrip('/')
                    for _aw in range(3):
                        await asyncio.sleep(2)
                        try:
                            cur = page.url.split('#')[0] \
                                .split('?')[0].rstrip('/')
                            if cur != orig_b:
                                if log:
                                    log.log_captcha(
                                        "tilda_auto_resubmitted",
                                        redirect=page.url[:60],
                                    )
                                return "tilda_auto_submitted"
                        except Exception:
                            return "tilda_auto_submitted"
                        try:
                            has_s = await page.evaluate(
                                r"""() => {
                                const sbs = document
                                    .querySelectorAll(
                                    '.t-form__successbox');
                                for (const sb of sbs) {
                                    try {
                                        const st =
                                            getComputedStyle(sb);
                                        if (st.display !== 'none')
                                            return true;
                                    } catch(e) {}
                                }
                                const cb = document.querySelector(
                                    '#tildaformcaptchabox');
                                if (cb) {
                                    try {
                                        const st =
                                            getComputedStyle(cb);
                                        if (st.display === 'none')
                                            return true;
                                    } catch(e) {}
                                }
                                return false;
                            }""")
                            if has_s:
                                if log:
                                    log.log_captcha(
                                        "tilda_auto_resubmitted",
                                        signal="success_text",
                                    )
                                return "tilda_auto_submitted"
                        except Exception:
                            return "tilda_auto_submitted"
                        try:
                            auto_ok = await page.evaluate(
                                r"""() => {
                                const xhr =
                                    window.__fbXHR || [];
                                for (const e of xhr) {
                                    const b = (e.b || '');
                                    if (/"message"\s*:\s*"OK"/i
                                        .test(b)) return true;
                                    if (e.s >= 200
                                        && e.s < 300
                                        && /"ok"/i.test(b)
                                        && !/needcaptcha/i
                                            .test(b))
                                        return true;
                                }
                                return false;
                            }""")
                            if auto_ok:
                                if log:
                                    log.log_captcha(
                                        "tilda_auto_resubmitted",
                                        signal="xhr_ok",
                                    )
                                return "tilda_auto_submitted"
                        except Exception:
                            pass
                    return "ok"
                if log:
                    log.log_captcha(
                        "smartcaptcha_click_challenge",
                    )

            # Клик не помог — решаем через API
            if rucaptcha_key:
                sitekey = (
                    await _extract_smartcaptcha_sitekey(
                        page,
                    )
                )
                if sitekey:
                    if log:
                        log.log_captcha(
                            "post_submit_sitekey",
                            sitekey=sitekey[:20],
                        )
                    token = await _solve_captcha(
                        "yandex", sitekey,
                        page_url, rucaptcha_key,
                    )
                    if token:
                        ok = await _inject_captcha_token(
                            page, "yandex", token,
                        )
                        if ok:
                            try:
                                await page.evaluate(
                                    r"""t => {
                                    const inps = document
                                        .querySelectorAll(
                                        'input[name="smart-token"],'
                                        + '[name*="captcha" i]'
                                        + '[type="hidden"]');
                                    for (const i of inps)
                                        i.value = t;
                                    if (window.smartCaptcha)
                                        try { window.smartCaptcha
                                            .execute(); }
                                        catch(e) {}
                                }""", token)
                            except Exception:
                                pass
                            return "ok"
                        return "inject_failed"
                    return "solve_failed"
                else:
                    if log:
                        log.log_captcha(
                            "post_submit_no_sitekey",
                        )

            # Не возвращаем None сразу — даём
            # следующему раунду шанс (iframe может
            # ещё загружаться)
            continue

        # Не SmartCaptcha: ищем reCAPTCHA/hCaptcha
        if rucaptcha_key:
            info = await _get_sitekey(page)
            if info and info.get("key"):
                ctype = info["type"]
                skey = info["key"]
                if log:
                    log.log_captcha(
                        "post_submit_found",
                        type=ctype,
                        sitekey=skey[:20],
                    )
                token = await _solve_captcha(
                    ctype, skey,
                    page_url, rucaptcha_key,
                    enterprise=info.get(
                        "enterprise", False
                    ),
                )
                if not token:
                    return "solve_failed"
                ok = await _inject_captcha_token(
                    page, ctype, token,
                )
                return "ok" if ok else "inject_failed"

        slider_res = await _detect_slider_captcha(
            page, rucaptcha_key,
        )
        if slider_res == "ok":
            return "ok"

        img_res = await _detect_image_captcha(
            page, rucaptcha_key,
        )
        if img_res == "ok":
            return "ok"

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
                + '#tildaformcaptchabox,'
                + 'iframe[src*="smartcaptcha" i],'
                + 'iframe[src*="captcha-cloud" i],'
                + 'iframe[src*="tildaapi" i]'
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

    if not info and has_captcha_hint:
        for _wait in range(3):
            await asyncio.sleep(1.5)
            info = await _get_sitekey(page)
            if info:
                break
        if not info:
            try:
                has_recaptcha_iframe = await page.evaluate(
                    r"""() => !!document.querySelector(
                        'iframe[src*="recaptcha"],'
                        + 'iframe[src*="hcaptcha"],'
                        + 'div.g-recaptcha,'
                        + 'div.h-captcha'
                    )""")
                if has_recaptcha_iframe:
                    await asyncio.sleep(2)
                    info = await _get_sitekey(page)
            except Exception:
                pass

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
