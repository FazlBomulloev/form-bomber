import asyncio
import re
import time
from typing import Optional
from urllib.parse import urljoin, urlparse

from config import (
    TRIGGER_BUTTON_SEL, WIDGET_BLACKLIST_RE,
)
from models import FormContext
from js_extractor import extract_form_json
from browser_utils import scroll_page_for_lazy
from logger import get_logger

TRIGGER_PRIORITY = [
    [
        "заказать звонок", "обратный звонок",
        "заказать обратный звонок",
        "перезвоните мне", "перезвоните",
        "жду звонка", "закажите звонок",
        "request a call", "callback",
        "позвоните мне",
    ],
    [
        "получить консультацию",
        "бесплатная консультация",
        "консультация специалиста",
        "консультация", "связаться с нами",
        "связаться", "свяжитесь с нами",
        "обратная связь",
    ],
    [
        "записаться на приём", "записаться на прием",
        "записаться", "запись на приём",
        "запись на прием", "запись онлайн",
        "онлайн-запись", "онлайн запись",
        "забронировать", "выбрать время",
    ],
    [
        "оставить заявку", "отправить заявку",
        "задать вопрос", "написать нам",
        "оставить заявк", "заявка",
        "получить", "узнать цену",
        "рассчитать стоимость",
        "узнать стоимость",
        "отправить сообщение",
    ],
]

_PHONE_WAIT_SELS = (
    'input[type="tel"],'
    'input[name*="phone" i],'
    'input[name*="tel" i],'
    'input[placeholder*="телефон" i],'
    'input[placeholder*="phone" i],'
    'input[placeholder*="+7" i],'
    'input[placeholder*="+9" i],'
    'input[placeholder*="(" i],'
    'input.t-input-phonemask,'
    'input[name="tildaspec-phone-part[]"],'
    'input[data-tel-input],'
    'input[inputmode="tel"],'
    'input[autocomplete="tel"]'
)

_WIDGET_RE = re.compile(
    WIDGET_BLACKLIST_RE, re.I,
)

_TRIGGER_KEYWORDS_FLAT = [
    kw for group in TRIGGER_PRIORITY for kw in group
]

async def _has_phone_visible(page) -> bool:
    try:
        return bool(await page.evaluate(
            r"""(sels) => {
            for (const el of
                document.querySelectorAll(sels)) {
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                if (r.width > 18 && r.height > 6
                    && st.display !== 'none'
                    && st.visibility !== 'hidden'
                    && st.opacity !== '0')
                    return true;
            }
            return false;
        }""", _PHONE_WAIT_SELS,
        ))
    except Exception:
        return False

async def _wait_form_after_trigger(
    page, timeout=12000,
) -> bool:
    try:
        await page.wait_for_selector(
            _PHONE_WAIT_SELS,
            timeout=timeout,
            state="visible",
        )
        return True
    except Exception:
        pass
    modal_sels = (
        '[role="dialog"],[aria-modal="true"],'
        '[class*="popup" i]:not(nav),'
        '[class*="modal" i]:not(nav),'
        '[class*="form" i][style*="display: block"],'
        '[class*="form" i][style*="opacity: 1"]'
    )
    try:
        await page.wait_for_selector(
            modal_sels, timeout=3000,
            state="visible",
        )
        await asyncio.sleep(0.8)
        if await _has_phone_visible(page):
            return True
    except Exception:
        pass
    try:
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                found = await frame.evaluate(
                    r"""(sels) => {
                    for (const el of
                        document.querySelectorAll(sels)) {
                        const r = el.getBoundingClientRect();
                        const st = getComputedStyle(el);
                        if (r.width > 18 && r.height > 6
                            && st.display !== 'none'
                            && st.visibility !== 'hidden'
                            && st.opacity !== '0')
                            return true;
                    }
                    return false;
                }""", _PHONE_WAIT_SELS,
                )
                if found:
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False

async def _is_widget_btn(el) -> bool:
    try:
        sig = await el.evaluate(
            r"""(el, keywords) => {
            const s = (
                (el.className||'') + ' '
                + (el.id||'') + ' '
                + (el.getAttribute('data-name')||'')
            ).toLowerCase();
            const aria = (
                (el.getAttribute('aria-label')||'')
                + ' '
                + (el.getAttribute('title')||'')
                + ' '
                + (el.getAttribute('data-tooltip')||'')
                + ' '
                + (el.getAttribute('data-text')||'')
            ).toLowerCase();
            const st = getComputedStyle(el);
            const fixed = (
                st.position === 'fixed'
                || st.position === 'sticky'
            );
            const r = el.getBoundingClientRect();
            const isRound = (
                r.width < 80 && r.height < 80
                && r.width > 20
                && Math.abs(r.width - r.height) < 15
            );
            let kwHit = false;
            if (aria) {
                for (const kw of keywords) {
                    if (kw && aria.indexOf(kw) !== -1) {
                        kwHit = true;
                        break;
                    }
                }
            }
            return JSON.stringify({
                sig, fixed, isRound, kwHit,
                w: r.width, h: r.height,
            });
        }""",
            _TRIGGER_KEYWORDS_FLAT,
        )
        import json
        info = json.loads(sig)
        if info.get("kwHit"):
            return False
        if _WIDGET_RE.search(info["sig"]):
            return True
        if info["fixed"] and info["isRound"]:
            return True
        return False
    except Exception:
        return False

async def _collect_trigger_buttons(page):
    log = get_logger()
    buttons = []
    seen_texts = set()
    all_els = await page.query_selector_all(
        TRIGGER_BUTTON_SEL
    )
    for el in all_els:
        try:
            if not await el.is_visible():
                continue
            if await _is_widget_btn(el):
                continue
            text = (
                await el.inner_text()
            ).lower().strip()
            if not text:
                for attr in (
                    'aria-label', 'title',
                    'data-tooltip', 'data-text',
                ):
                    val = await el.get_attribute(attr)
                    if val:
                        text = val.lower().strip()
                        if text:
                            break
            if not text:
                try:
                    span_text = await el.evaluate(
                        r"""el => {
                        const s = el.querySelector(
                            'span'
                        );
                        return s
                            ? (s.innerText || '')
                                .trim()
                            : '';
                    }""",
                    )
                    if span_text:
                        text = span_text.lower().strip()
                except Exception:
                    pass
            if not text or len(text) > 60:
                continue
            if text in seen_texts:
                continue
            priority = len(TRIGGER_PRIORITY)
            for idx, group in enumerate(
                TRIGGER_PRIORITY
            ):
                if any(kw in text for kw in group):
                    priority = idx
                    break
            if priority >= len(TRIGGER_PRIORITY):
                continue
            seen_texts.add(text)
            buttons.append((priority, text, el))
        except Exception:
            continue
    buttons.sort(key=lambda x: x[0])
    if log and buttons:
        labels = [
            f"П{b[0]}:{b[1][:25]}"
            for b in buttons[:6]
        ]
        log.step(
            "trigger_btns",
            f"найдено {len(buttons)}: "
            + ", ".join(labels),
        )
    return buttons

async def _aggressive_form_reveal(page):
    log = get_logger()
    if log:
        log.step(
            "aggressive",
            "принудительный поиск форм через JS",
        )

    revealed = await page.evaluate(r"""() => {
        let found = 0;
        const formFunctions = [];

        for (const form of
            document.querySelectorAll('form')) {
            const hasPhone = form.querySelector(
                'input[type="tel"],'
                + 'input[name*="phone" i],'
                + 'input[placeholder*="телефон" i],'
                + 'input[placeholder*="phone" i],'
                + 'input[inputmode="tel"]'
            );
            if (!hasPhone) continue;
            let node = form;
            while (node && node !== document.body) {
                try {
                    const st = getComputedStyle(node);
                    if (st.display === 'none')
                        node.style.setProperty(
                            'display','block','important');
                    if (st.visibility === 'hidden')
                        node.style.setProperty(
                            'visibility','visible',
                            'important');
                    if (parseFloat(st.opacity) < 0.1)
                        node.style.setProperty(
                            'opacity','1','important');
                    if (st.height === '0px'
                        || st.maxHeight === '0px')
                        node.style.setProperty(
                            'height','auto','important');
                    if (st.overflow === 'hidden') {
                        const r =
                            node.getBoundingClientRect();
                        if (r.height < 10)
                            node.style.setProperty(
                                'overflow','visible',
                                'important');
                    }
                } catch(e) {}
                node = node.parentElement;
            }
            found++;
        }

        const pats = [
            /open.?form/i, /show.?form/i,
            /show.?popup/i, /open.?popup/i,
            /show.?modal/i, /open.?modal/i,
            /callback/i, /show.?callback/i,
            /open.?callback/i,
        ];
        for (const key of Object.keys(window)) {
            if (typeof window[key] !== 'function')
                continue;
            for (const p of pats) {
                if (p.test(key)) {
                    formFunctions.push(key);
                    break;
                }
            }
        }

        const tPops = document.querySelectorAll(
            '[class*="t-popup"][data-tooltip-hook],'
            + '.t-popup'
        );
        for (const popup of tPops) {
            try {
                popup.classList.add('t-popup_show');
                popup.style.setProperty(
                    'display','block','important');
                popup.style.setProperty(
                    'opacity','1','important');
                popup.style.setProperty(
                    'visibility','visible','important');
                found++;
            } catch(e) {}
        }

        const b24 = document.querySelectorAll(
            '[class*="b24-form" i],'
            + '[class*="bx-core-form" i]'
        );
        for (const el of b24) {
            try {
                el.style.setProperty(
                    'display','block','important');
                el.style.setProperty(
                    'visibility','visible','important');
                el.style.setProperty(
                    'opacity','1','important');
                found++;
            } catch(e) {}
        }

        const btnTexts = [
            'записаться','запись','консультац',
            'заказать звонок','обратный звонок',
            'связаться','заявк',
        ];
        const btns = document.querySelectorAll(
            'button, a[href^="#"], [role="button"],'
            + '[data-action], [onclick]'
        );
        let dispatched = 0;
        for (const btn of btns) {
            if (dispatched >= 3) break;
            const t = (btn.innerText||'')
                .toLowerCase().trim();
            if (t.length > 40) continue;
            const hit = btnTexts.some(
                kw => t.includes(kw));
            if (!hit) continue;
            try {
                btn.dispatchEvent(new MouseEvent(
                    'click', {bubbles:true}));
                dispatched++;
            } catch(e) {}
        }

        for (const ov of document.querySelectorAll(
            '[class*="overlay" i],'
            + '[class*="backdrop" i]'
        )) {
            try {
                const st = getComputedStyle(ov);
                if (st.position !== 'fixed'
                    && st.position !== 'absolute')
                    continue;
                const r = ov.getBoundingClientRect();
                if (r.width < innerWidth * 0.5)
                    continue;
                if (ov.querySelector(
                    'form,input[type="tel"]'))
                    continue;
                const sig = ((ov.className||'')
                    + ' ' + (ov.id||'')).toLowerCase();
                if (/captcha|recaptcha|smartcaptcha/
                    .test(sig)) continue;
                ov.style.setProperty(
                    'display','none','important');
            } catch(e) {}
        }

        return {found, formFunctions, dispatched};
    }""")

    if revealed and revealed.get('found', 0) > 0:
        if log:
            log.ok(
                f"раскрыто {revealed['found']} "
                f"скрытых форм"
            )
        await asyncio.sleep(0.8)
        form_json = await extract_form_json(page)
        if form_json and form_json.get("fields"):
            has_phone = any(
                f.get("role") == "phone"
                for f in form_json["fields"]
            )
            if has_phone:
                if log:
                    log.ok(
                        f"форма после раскрытия: "
                        f"{len(form_json['fields'])}"
                        f" полей"
                    )
                return form_json

    if revealed and revealed.get('dispatched', 0) > 0:
        await asyncio.sleep(1.0)
        form_json = await extract_form_json(page)
        if form_json and form_json.get("fields"):
            has_phone = any(
                f.get("role") == "phone"
                for f in form_json["fields"]
            )
            if has_phone:
                if log:
                    log.ok(
                        f"форма после dispatch: "
                        f"{len(form_json['fields'])}"
                        f" полей"
                    )
                return form_json

    if revealed and revealed.get('formFunctions'):
        for fn_name in (
            revealed['formFunctions'][:3]
        ):
            if not re.match(
                r'^[a-zA-Z_$][\w$]*$', fn_name
            ):
                continue
            try:
                if log:
                    log.step(
                        "js_call",
                        f"window.{fn_name}()",
                    )
                await page.evaluate(
                    f"() => {{ try {{ "
                    f"window.{fn_name}(); "
                    f"}} catch(e) {{}} }}"
                )
                await asyncio.sleep(1.5)
                form_json = (
                    await extract_form_json(page)
                )
                if (
                    form_json
                    and form_json.get("fields")
                ):
                    has_phone = any(
                        f.get("role") == "phone"
                        for f in form_json["fields"]
                    )
                    if has_phone:
                        if log:
                            log.ok(
                                f"форма после "
                                f"{fn_name}(): "
                                f"{len(form_json['fields'])}"
                                f" полей"
                            )
                        return form_json
            except Exception:
                continue

    return None

async def _find_in_iframes(page):
    log = get_logger()
    targets = [
        f for f in page.frames
        if f != page.main_frame
    ]
    if not targets:
        return None, None

    async def _scan(frame):
        try:
            data = await asyncio.wait_for(
                extract_form_json(frame), timeout=4,
            )
        except Exception:
            return frame, None
        if not data or not data.get("fields"):
            return frame, None
        if not any(
            f.get("role") == "phone"
            for f in data["fields"]
        ):
            return frame, None
        return frame, data

    tasks = [
        asyncio.create_task(_scan(fr))
        for fr in targets
    ]
    try:
        for done in asyncio.as_completed(tasks):
            frame, data = await done
            if not data:
                continue
            if log:
                log.ok(
                    f"форма в iframe: "
                    f"{len(data['fields'])} полей "
                    f"({frame.url[:60]})"
                )
            return data, frame
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
    return None, None

async def _mutation_observer_retry(page):
    log = get_logger()
    try:
        await page.evaluate(r"""() => {
            window.__fbFormAppeared = false;
            if (window.__fbFormMO) {
                try { window.__fbFormMO.disconnect(); }
                catch(e) {}
            }
            const mo = new MutationObserver(() => {
                if (window.__fbFormAppeared) return;
                const phoneEl = document.querySelector(
                    'input[type="tel"],'
                    + 'input[name*="phone" i],'
                    + 'input[placeholder*="телефон" i],'
                    + 'input[inputmode="tel"]'
                );
                if (phoneEl) {
                    try {
                        const r = phoneEl.getBoundingClientRect();
                        if (r.width > 5 && r.height > 5)
                            window.__fbFormAppeared = true;
                    } catch(e) {}
                }
            });
            mo.observe(document.body, {
                childList: true, subtree: true,
                attributes: true,
                attributeFilter: ['style','class'],
            });
            window.__fbFormMO = mo;
        }""")
    except Exception:
        return None

    try:
        await page.evaluate(r"""() => {
            window.scrollTo(0, document.body.scrollHeight / 2);
            window.dispatchEvent(new Event('scroll'));
            const evt = new MouseEvent('mouseenter', {bubbles: true});
            document.body.dispatchEvent(evt);
        }""")
    except Exception:
        pass

    appeared_flag = False
    for _ in range(15):
        await asyncio.sleep(0.2)
        try:
            appeared = await page.evaluate(
                "() => window.__fbFormAppeared",
            )
            if appeared:
                appeared_flag = True
                if log:
                    log.ok(
                        "MutationObserver: "
                        "phone field появился",
                    )
                break
        except Exception:
            break

    try:
        await page.evaluate(r"""() => {
            try { window.__fbFormMO?.disconnect(); }
            catch(e) {}
            window.__fbFormMO = null;
        }""")
    except Exception:
        pass

    if appeared_flag:
        await asyncio.sleep(0.8)

    data = await extract_form_json(page)
    if (not data or not data.get("fields")) and appeared_flag:
        await asyncio.sleep(0.7)
        data = await extract_form_json(page)
    return data

_SEARCH_SUBMIT_RE = re.compile(
    r"найти|искать|search|поиск", re.I,
)
_NEWSLETTER_SUBMIT_RE = re.compile(
    r"подписа|subscribe|рассылк|newsletter", re.I,
)
_LEAD_SUBMIT_RE = re.compile(
    r"заказать звонок|перезвон|записаться|\bзапис|"
    r"оставить заявк|\bзаявк|консультац|"
    r"отправить сообщени|связаться|получить",
    re.I,
)

def _score_form_type(
    form_json: dict, submit_text: str = "",
) -> dict:
    fields = form_json.get("fields") or []
    st = (submit_text or "").lower()

    visible = [f for f in fields if f.get("visible")]
    roles = [f.get("role") for f in fields]
    types = [
        (f.get("type") or "").lower() for f in fields
    ]

    has_phone = "phone" in roles
    has_email = "email" in roles
    has_password = any(t == "password" for t in types)
    has_search_input = any(t == "search" for t in types)
    has_textarea_named = any(
        f.get("tag") == "textarea" and f.get("name")
        for f in fields
    )

    score = 0
    reasons = []
    ftype = "contact"

    if has_phone:
        score += 30
        reasons.append("+phone")
    if _LEAD_SUBMIT_RE.search(st):
        score += 20
        reasons.append("+lead_btn")
    if has_textarea_named:
        score += 8
        reasons.append("+textarea")

    if has_password:
        score -= 60
        reasons.append("-password")
        ftype = "login"
    single_search = (
        len(visible) <= 1
        and bool(_SEARCH_SUBMIT_RE.search(st))
        and not has_phone
    )
    if has_search_input or single_search:
        score -= 50
        reasons.append("-search")
        if ftype == "contact":
            ftype = "search"
    newsletter = (
        has_email and not has_phone
        and len(visible) <= 2
        and (
            bool(_NEWSLETTER_SUBMIT_RE.search(st))
            or not _LEAD_SUBMIT_RE.search(st)
        )
    )
    if newsletter:
        score -= 40
        reasons.append("-newsletter")
        if ftype == "contact":
            ftype = "newsletter"

    if ftype == "contact" and has_phone and score > 0:
        ftype = "lead"

    return {
        "type": ftype, "score": score,
        "reasons": reasons,
    }

async def _get_submit_text(ctx, form_json: dict) -> str:
    sel = form_json.get("submit_selector")
    if not sel or ctx is None:
        return ""
    try:
        el = await ctx.query_selector(sel)
        if not el:
            return ""
        txt = (await el.inner_text()) or ""
        if not txt.strip():
            txt = (
                await el.get_attribute("value")
            ) or ""
        return txt.strip()[:80]
    except Exception:
        return ""

async def _accept_form(ctx, form_json: dict, log) -> bool:
    try:
        submit_text = await _get_submit_text(
            ctx, form_json,
        )
    except Exception:
        submit_text = ""
    info = _score_form_type(form_json, submit_text)
    if log:
        log.step(
            "form_type",
            f"тип={info['type']} score={info['score']} "
            f"[{','.join(info['reasons'])}]",
        )
    if info["score"] <= -30:
        if log:
            log.warn(
                f"форма отклонена по типу: "
                f"{info['type']} (score={info['score']})"
            )
        return False
    return True

_CONTACT_PATHS = [
    "/kontakty", "/contacts", "/contact", "/contact-us",
    "/zapis", "/zayavka", "/online-zapis",
    "/onlajn-zapis", "/about/contacts",
    "/obratnaya-svyaz", "/feedback",
]

_CONTACT_LINK_RE = re.compile(
    r"контакт|запис|заявк|обратн|"
    r"contact|callback|feedback",
    re.I,
)

async def _collect_contact_links(page):
    try:
        hrefs = await page.evaluate(
            r"""(pattern) => {
            const re = new RegExp(pattern, 'i');
            const out = [];
            const seen = new Set();
            for (const a of
                document.querySelectorAll('a[href]')) {
                const href = a.getAttribute('href') || '';
                if (!href) continue;
                const low = href.toLowerCase();
                if (low.startsWith('#')
                    || low.startsWith('tel:')
                    || low.startsWith('mailto:')
                    || low.startsWith('javascript:'))
                    continue;
                const text = (a.innerText || '').trim();
                if (!re.test(href) && !re.test(text))
                    continue;
                let abs;
                try {
                    abs = new URL(href, location.href).href;
                } catch(e) { continue; }
                if (seen.has(abs)) continue;
                seen.add(abs);
                out.push(abs);
                if (out.length >= 8) break;
            }
            return out;
        }""",
            _CONTACT_LINK_RE.pattern,
        )
        return hrefs or []
    except Exception:
        return []

async def _find_contact_page(page, log):
    try:
        cur = page.url
    except Exception:
        return None, None
    parsed = urlparse(cur)
    if not parsed.scheme or not parsed.netloc:
        return None, None
    origin = f"{parsed.scheme}://{parsed.netloc}"
    host = parsed.netloc.lower()
    cur_norm = cur.split("#")[0].rstrip("/")

    candidates = []
    seen = set()

    def _add(u):
        if not u:
            return
        try:
            p = urlparse(u)
        except Exception:
            return
        if p.netloc and p.netloc.lower() != host:
            return
        norm = u.split("#")[0].rstrip("/")
        if not norm or norm == cur_norm:
            return
        if norm in seen:
            return
        seen.add(norm)
        candidates.append(u)

    for link in await _collect_contact_links(page):
        _add(link)
    for path in _CONTACT_PATHS:
        _add(urljoin(origin + "/", path.lstrip("/")))

    if not candidates:
        return None, None

    if log:
        log.step(
            "contact_search",
            f"форма не найдена, пробуем контакт-страницы "
            f"({len(candidates)} канд.)",
        )

    deadline = time.monotonic() + 15.0
    max_nav = 4
    nav = 0
    for url in candidates:
        if nav >= max_nav or time.monotonic() >= deadline:
            break
        nav += 1
        try:
            await page.goto(
                url, wait_until="domcontentloaded",
                timeout=8000,
            )
        except Exception:
            continue
        await asyncio.sleep(0.6)
        if log:
            log.step("contact_nav", f"перешли: {url[:70]}")
        remaining = deadline - time.monotonic()
        if remaining <= 1.0:
            break
        try:
            form_json, ctx = await asyncio.wait_for(
                extract_forms(
                    page, allow_contact_search=False,
                ),
                timeout=max(3.0, remaining),
            )
        except Exception:
            form_json, ctx = None, None
        if form_json:
            if log:
                log.ok(
                    f"форма на контакт-странице: "
                    f"{url[:70]}"
                )
            return form_json, ctx
    return None, None

async def extract_forms(
    page, allow_contact_search: bool = True,
) -> tuple:
    log = get_logger()

    await scroll_page_for_lazy(page)

    if log:
        log.step("extract", "ищем форму в DOM")

    main_task = asyncio.create_task(
        extract_form_json(page),
    )
    iframe_task = asyncio.create_task(
        _find_in_iframes(page),
    )

    form_json = None
    try:
        form_json = await main_task
    except Exception:
        form_json = None

    has_phone_main = False
    source_main = ""
    if form_json and form_json.get("fields"):
        has_phone_main = any(
            f.get("role") == "phone"
            for f in form_json["fields"]
        )
        source_main = form_json.get("source", "form")

    if has_phone_main and source_main not in (
        "hidden_form",
    ):
        if await _accept_form(page, form_json, log):
            iframe_task.cancel()
            if log:
                log.ok(
                    f"форма в DOM: "
                    f"{len(form_json['fields'])} полей"
                )
            return form_json, FormContext(
                html="", source="form",
            )
        form_json = None
        has_phone_main = False

    iframe_form, iframe_frame = None, None
    try:
        iframe_form, iframe_frame = await iframe_task
    except Exception:
        iframe_form, iframe_frame = None, None

    if iframe_form and await _accept_form(
        iframe_frame, iframe_form, log,
    ):
        return iframe_form, FormContext(
            html="", source="iframe",
            frame=iframe_frame,
        )

    hidden_backup = (
        form_json if has_phone_main else None
    )

    if log:
        log.step(
            "trigger",
            "форма не в DOM, ищем кнопки",
        )
    buttons = await _collect_trigger_buttons(page)

    trigger_tries = 0
    for priority, text, el in buttons:
        if trigger_tries >= 10:
            break
        trigger_tries += 1
        try:
            if log:
                log.step(
                    "trigger_click",
                    f"П{priority}: «{text[:30]}»",
                )
            pages_before = len(
                page.context.pages
            )
            await el.click(timeout=5000)
            await asyncio.sleep(0.5)

            new_tab = None
            if len(page.context.pages) > pages_before:
                new_tab = page.context.pages[-1]
                if log:
                    log.step(
                        "new_tab",
                        f"кнопка открыла новую вкладку",
                    )
                try:
                    await new_tab.wait_for_load_state(
                        "domcontentloaded",
                        timeout=10000,
                    )
                except Exception:
                    pass

            search_page = new_tab or page

            appeared = (
                await _wait_form_after_trigger(
                    search_page, timeout=12000,
                )
            )
            accepted = False
            if appeared:
                for _retry in range(2):
                    await asyncio.sleep(
                        0.5 if _retry == 0 else 1.5
                    )
                    form_json = (
                        await extract_form_json(
                            search_page,
                        )
                    )
                    if (
                        form_json
                        and form_json.get("fields")
                    ):
                        has_phone = any(
                            f.get("role") == "phone"
                            for f in form_json["fields"]
                        )
                        if (
                            has_phone
                            and await _accept_form(
                                search_page, form_json, log,
                            )
                        ):
                            if log:
                                log.ok(
                                    f"форма после "
                                    f"«{text[:25]}»: "
                                    f"{len(form_json['fields'])}"
                                    f" полей"
                                )
                            ctx_src = "trigger"
                            frame = None
                            if new_tab:
                                ctx_src = "trigger_new_tab"
                                frame = new_tab
                            return form_json, FormContext(
                                html="",
                                source=ctx_src,
                                trigger_text=text[:50],
                                frame=frame,
                            )
                    if _retry == 0 and not (
                        form_json and form_json.get("fields")
                    ):
                        continue
                    break

            if new_tab:
                try:
                    await new_tab.close()
                except Exception:
                    pass
            elif not appeared:
                try:
                    await page.keyboard.press(
                        'Escape',
                    )
                    await asyncio.sleep(0.4)
                except Exception:
                    pass
        except Exception:
            continue

    agg_form = await _aggressive_form_reveal(page)
    if agg_form and await _accept_form(
        page, agg_form, log,
    ):
        if log:
            log.ok(
                f"форма через aggressive: "
                f"{len(agg_form.get('fields', []))}"
                f" полей"
            )
        return agg_form, FormContext(
            html="", source="aggressive_reveal",
        )

    if hidden_backup and await _accept_form(
        page, hidden_backup, log,
    ):
        if log:
            log.warn(
                "кнопки не помогли, используем "
                "скрытую форму из DOM"
            )
        return hidden_backup, FormContext(
            html="", source="hidden_form",
        )

    iframe_form2, frame2 = await _find_in_iframes(page)
    if iframe_form2 and await _accept_form(
        frame2, iframe_form2, log,
    ):
        return iframe_form2, FormContext(
            html="", source="iframe",
            frame=frame2,
        )

    mo_form = await _mutation_observer_retry(page)
    if mo_form and mo_form.get("fields"):
        has_phone = any(
            f.get("role") == "phone"
            for f in mo_form["fields"]
        )
        if has_phone and await _accept_form(
            page, mo_form, log,
        ):
            if log:
                log.ok("форма после MutationObserver")
            return mo_form, FormContext(
                html="", source="mutation_observer",
            )

    if allow_contact_search:
        cp_form, cp_ctx = await _find_contact_page(page, log)
        if cp_form:
            return cp_form, cp_ctx

    if log:
        log.warn("форма не найдена ни в DOM, "
                 "ни по кнопкам, ни в iframe")
    return None, FormContext(html="", source="none")

def build_smart_plan(form_json: dict) -> dict:
    if not form_json or not form_json.get("fields"):
        return {
            "form_found": False, "actions": [],
            "notes": "Эвристика: форма не найдена",
        }

    fields = form_json["fields"]
    form_sel = form_json.get("form_selector")
    submit_sel = form_json.get("submit_selector")

    actions = []
    step = 1

    role_value_map = {
        "phone": "{phone}",
        "name": "{name}",
        "firstname": "{firstname}",
        "lastname": "{lastname}",
        "patronymic": "{patronymic}",
        "email": "{email}",
        "comment": "{comment}",
        "date": "{date}",
    }
    fill_order = [
        "name", "firstname", "lastname",
        "patronymic", "phone", "email",
        "comment", "date",
    ]

    seen_roles = set()
    has_phone = False

    for role in fill_order:
        sorted_fields = sorted(
            fields,
            key=lambda f: f.get("priority", 1),
        )
        for f in sorted_fields:
            if f["role"] != role:
                continue
            if role in seen_roles:
                continue
            if not f.get("selector"):
                continue
            value = role_value_map.get(role, "")
            if not value:
                continue
            actions.append({
                "step": step, "action": "fill",
                "field": role,
                "selector": f["selector"],
                "value": value,
            })
            step += 1
            seen_roles.add(role)
            if role == "phone":
                has_phone = True

    for f in fields:
        if f["role"] != "checkbox_consent":
            continue
        if not f.get("selector"):
            continue
        actions.append({
            "step": step, "action": "click",
            "field": "checkbox",
            "selector": f["selector"],
        })
        step += 1

    for f in fields:
        if f["role"] != "dropdown":
            continue
        if not f.get("selector"):
            continue
        actions.append({
            "step": step,
            "action": "select_first",
            "field": "dropdown",
            "selector": f["selector"],
            "type": "native",
        })
        step += 1

    radio_names = set()
    for f in fields:
        if f["role"] != "radio":
            continue
        if not f.get("selector"):
            continue
        rname = f.get("name", "")
        if rname in radio_names:
            continue
        radio_names.add(rname)
        actions.append({
            "step": step, "action": "click",
            "field": "radio",
            "selector": f["selector"],
        })
        step += 1

    if submit_sel:
        actions.append({
            "step": step, "action": "submit",
            "field": "submit",
            "selector": submit_sel,
        })

    has_captcha = any(
        f.get("name", "").lower() in (
            "g-recaptcha-response",
            "h-captcha-response",
            "smart-token",
            "cf-turnstile-response",
        )
        for f in fields
    )

    notes = "Эвристика: plan ok"
    if not has_phone:
        notes = (
            "Эвристика: телефон не найден в форме"
        )

    return {
        "form_found": True,
        "form_selector": form_sel,
        "actions": actions,
        "has_captcha": has_captcha,
        "captcha_type": None,
        "cookie_selector": None,
        "success_texts": [
            "спасибо", "заявка принята",
            "перезвоним", "отправлено",
        ],
        "error_texts": [
            "ошибка", "заполните",
            "некорректн", "обязательное поле",
        ],
        "notes": notes,
    }

async def resolve_form_el(
    page, form_selector: Optional[str],
):
    if not form_selector:
        return None
    fe = None
    try:
        fe = await page.query_selector(form_selector)
    except Exception:
        fe = None

    async def _has_lead(f):
        if not f:
            return False
        try:
            return bool(await f.evaluate(r"""form => {
                if (!form || !form.querySelector)
                    return false;
                return !!form.querySelector(
                    'input[type="tel"],'
                    + 'input[type="text"],'
                    + 'input[type="email"],'
                    + 'textarea,'
                    + 'input[name*="phone" i]'
                );
            }"""))
        except Exception:
            return False

    if fe and await _has_lead(fe):
        return fe

    for alt in (
        'form:has(input[type="tel"])',
        '[role="dialog"] form',
        '[aria-modal="true"] form',
        '[class*="popup" i] form',
        '[class*="modal" i] form',
    ):
        try:
            cand = await page.query_selector(alt)
            if cand and await _has_lead(cand):
                return cand
        except Exception:
            continue
    return fe