import asyncio
import re
from typing import Optional

from config import (
    MODAL_KEYWORDS, TRIGGER_BUTTON_SEL,
    WIDGET_BLACKLIST_RE, BITRIX_FORM_TRIGGER_SEL,
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
    page, timeout=8000,
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
            modal_sels, timeout=2500,
            state="visible",
        )
        await asyncio.sleep(0.8)
        return await _has_phone_visible(page)
    except Exception:
        return False


async def _is_widget_btn(el) -> bool:
    try:
        sig = await el.evaluate(r"""el => {
            const s = (
                (el.className||'') + ' '
                + (el.id||'') + ' '
                + (el.getAttribute('data-name')||'')
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
            return JSON.stringify({
                sig, fixed, isRound,
                w: r.width, h: r.height,
            });
        }""")
        import json
        info = json.loads(sig)
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
    """Принудительный поиск: раскрытие скрытых форм,
    вызов CMS-функций, удаление overlay-блокеров."""
    log = get_logger()
    if log:
        log.step(
            "aggressive",
            "принудительный поиск форм через JS",
        )

    revealed = await page.evaluate(r"""() => {
        let found = 0;
        const formFunctions = [];

        // 1. Force-show hidden <form> с полем телефона
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

        // 2. Глобальные JS-функции открытия форм
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

        // 3. Tilda-попапы
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

        // 4. Bitrix24 form containers
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

        // 5. dispatchEvent на кнопки-триггеры
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

        // 6. Удаление overlay-блокеров (не-капча)
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


async def extract_forms(page) -> tuple:
    log = get_logger()

    await scroll_page_for_lazy(page)

    # ── Шаг 1: DOM ──────────────────────────
    if log:
        log.step("extract", "ищем форму в DOM")
    form_json = await extract_form_json(page)
    if form_json and form_json.get("fields"):
        has_phone = any(
            f.get("role") == "phone"
            for f in form_json["fields"]
        )
        source = form_json.get("source", "form")
        if has_phone and source not in (
            "hidden_form",
        ):
            if log:
                log.ok(
                    f"форма в DOM: "
                    f"{len(form_json['fields'])} полей"
                )
            return form_json, FormContext(
                html="", source="form",
            )
        if has_phone:
            hidden_backup = form_json
        else:
            hidden_backup = None
    else:
        hidden_backup = None

    # ── Шаг 2: кнопки по приоритету ──────────
    if log:
        log.step(
            "trigger",
            "форма не в DOM, ищем кнопки",
        )
    buttons = await _collect_trigger_buttons(page)

    trigger_tries = 0
    for priority, text, el in buttons:
        if trigger_tries >= 5:
            break
        trigger_tries += 1
        try:
            if log:
                log.step(
                    "trigger_click",
                    f"П{priority}: «{text[:30]}»",
                )
            await el.click(timeout=5000)
            appeared = (
                await _wait_form_after_trigger(
                    page, timeout=8000,
                )
            )
            if appeared:
                await asyncio.sleep(0.5)
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
                                f"«{text[:25]}»: "
                                f"{len(form_json['fields'])}"
                                f" полей"
                            )
                        return form_json, FormContext(
                            html="",
                            source="trigger",
                            trigger_text=text[:50],
                        )
            try:
                await page.keyboard.press('Escape')
                await asyncio.sleep(0.4)
            except Exception:
                pass
        except Exception:
            continue

    # ── Шаг 2.5: агрессивное JS-раскрытие ──
    agg_form = await _aggressive_form_reveal(page)
    if agg_form:
        if log:
            log.ok(
                f"форма через aggressive: "
                f"{len(agg_form.get('fields', []))}"
                f" полей"
            )
        return agg_form, FormContext(
            html="", source="aggressive_reveal",
        )

    # ── Шаг 3: скрытая форма как fallback ──
    if hidden_backup:
        if log:
            log.warn(
                "кнопки не помогли, используем "
                "скрытую форму из DOM"
            )
        return hidden_backup, FormContext(
            html="", source="hidden_form",
        )

    # ── Шаг 4: поиск в iframe ──────────────
    if log:
        log.step("iframe", "ищем форму в iframe")
    try:
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                iframe_form = await extract_form_json(
                    frame
                )
                if (
                    iframe_form
                    and iframe_form.get("fields")
                ):
                    has_phone = any(
                        f.get("role") == "phone"
                        for f in iframe_form["fields"]
                    )
                    if has_phone:
                        if log:
                            log.ok(
                                f"форма в iframe: "
                                f"{len(iframe_form['fields'])}"
                                f" полей"
                            )
                        return iframe_form, FormContext(
                            html="", source="iframe",
                            frame=frame,
                        )
            except Exception:
                continue
    except Exception:
        pass

    # ── Шаг 5: не нашли ─────────────────────
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


async def reopen_form(
    page, ctx: FormContext, phone_sel: str,
) -> bool:
    if await _has_phone_visible(page):
        return True

    if ctx.trigger_text:
        buttons = (
            await _collect_trigger_buttons(page)
        )
        trigger_low = ctx.trigger_text.lower()[:20]
        for _, text, el in buttons:
            if trigger_low in text:
                try:
                    await el.click()
                    ok = (
                        await _wait_form_after_trigger(
                            page, timeout=5000,
                        )
                    )
                    if ok:
                        return True
                except Exception:
                    pass

    if ctx.source == "tilda_popup" and ctx.trigger_href:
        target_low = ctx.trigger_href.lower()
        for link in await page.query_selector_all(
            'a[href^="#popup:"],'
            'a[href^="#Popup:"]'
        ):
            href = (
                await link.get_attribute('href') or ''
            ).lower()
            if href != target_low:
                continue
            if not await link.is_visible():
                continue
            try:
                await link.click()
                await asyncio.sleep(3.5)
                if await _has_phone_visible(page):
                    return True
            except Exception:
                pass

    fallback_sel = (
        'button,a[href^="#popup:"],'
        '[data-action="modal"],'
        '[data-toggle="modal"],'
        '[data-modal],[data-popup],'
        f'{BITRIX_FORM_TRIGGER_SEL}'
    )
    for el in await page.query_selector_all(
        fallback_sel
    ):
        try:
            if not await el.is_visible():
                continue
            text = (await el.inner_text()).lower()
            href = (
                await el.get_attribute('href') or ''
            ).lower()
            is_modal = (
                any(
                    kw in text
                    for kw in MODAL_KEYWORDS
                )
                or '#popup:' in href
            )
            if not is_modal:
                continue
            await el.click()
            ok = await _wait_form_after_trigger(
                page, timeout=5000,
            )
            if ok:
                return True
            await page.keyboard.press('Escape')
            await asyncio.sleep(0.3)
        except Exception:
            continue

    return False


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