import asyncio
import re
from datetime import datetime, timedelta

from config import PHONE_FALLBACKS
from logger import get_logger
from browser_utils import (
    find_el, react_patch_input, smart_click,
    step_shot, dismiss_popups,
)
from form_finder import resolve_form_el


_phone_method_cache: dict = {}


def _next_workday():
    d = datetime.now() + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def _resolve_value(
    template: str, phone: str,
    firstname: str, lastname: str,
    patronymic: str,
    email: str, comment: str,
) -> str:
    fio = " ".join(
        p for p in [lastname, firstname, patronymic]
        if p
    )
    vmap = {
        "{phone}": phone,
        "{name}": firstname,
        "{fio}": fio,
        "{firstname}": firstname,
        "{lastname}": lastname,
        "{patronymic}": patronymic,
        "{email}": email,
        "{comment}": comment,
        "{date}": _next_workday(),
    }
    val = template
    for k, v in vmap.items():
        val = val.replace(k, v)
    return val


def _get_keyboard(ctx):
    if hasattr(ctx, 'keyboard'):
        return ctx.keyboard
    return ctx.page.keyboard


async def _slow_type(page, el, text, delay=60):
    try:
        await el.fill("")
    except Exception:
        pass
    kb = _get_keyboard(page)
    try:
        await kb.type(text, delay=delay)
    except Exception:
        for ch in text:
            try:
                await kb.press(ch)
            except Exception:
                pass
            await asyncio.sleep(delay / 1000)


async def _is_tilda(page):
    try:
        return await page.evaluate(r"""() => {
            return !!(
                document.querySelector('.t-form')
                || document.querySelector('.t-input')
                || document.querySelector(
                    '[class*="t-input" i]')
                || document.querySelector(
                    'link[href*="tilda"]')
                || document.querySelector(
                    'script[src*="tilda"]')
                || (window.t_onReady !== undefined)
            );
        }""")
    except Exception:
        return False


async def _tilda_fill(page, el, value):
    kb = _get_keyboard(page)
    try:
        await el.click(timeout=2000)
    except Exception:
        try:
            await page.evaluate(
                "el => el.focus()", el
            )
        except Exception:
            pass
    await asyncio.sleep(0.15)

    try:
        await kb.press("Control+a")
        await asyncio.sleep(0.05)
        await kb.press("Backspace")
        await asyncio.sleep(0.1)
    except Exception:
        pass

    try:
        await kb.type(value, delay=35)
    except Exception:
        await _slow_type(page, el, value, 40)

    await asyncio.sleep(0.2)

    try:
        await page.evaluate(r"""el => {
            el.dispatchEvent(
                new Event('change', {bubbles: true}));
            el.dispatchEvent(
                new Event('blur', {bubbles: true}));
        }""", el)
    except Exception:
        pass


async def _fill_field(page, sel, value, field_name):
    log = get_logger()
    el = await find_el(page, sel)
    if not el:
        if log:
            log.log_action(
                "fill", sel, value,
                success=False,
                error="элемент не найден",
            )
        return False

    try:
        await el.scroll_into_view_if_needed()
    except Exception:
        pass
    await asyncio.sleep(0.15)

    tilda = await _is_tilda(page)

    if tilda:
        await _tilda_fill(page, el, value)
        actual = ""
        try:
            actual = await page.evaluate(
                "el => el.value || ''", el
            ) or ""
        except Exception:
            pass
        if log:
            log.log_action(
                "fill(tilda)", sel, value[:30],
                success=bool(actual.strip()),
            )
        return bool(actual.strip()) or True

    try:
        await el.click(timeout=2000)
    except Exception:
        try:
            await page.evaluate(
                "el => el.focus()", el
            )
        except Exception:
            pass

    try:
        await el.fill(value)
        await asyncio.sleep(0.2)
        await react_patch_input(page, el, value)
    except Exception:
        try:
            await react_patch_input(page, el, value)
        except Exception as e2:
            if log:
                log.log_action(
                    "fill", sel, value,
                    success=False,
                    error=str(e2)[:120],
                )
            return False

    actual = ""
    try:
        actual = await page.evaluate(
            "el => el.value || ''", el
        ) or ""
    except Exception:
        pass

    if not actual.strip():
        if log:
            log.warn(
                f"fill verify empty, fallback: {sel}"
            )
        try:
            await _slow_type(page, el, value, 50)
            actual = await page.evaluate(
                "el => el.value || ''", el
            ) or ""
        except Exception:
            pass
        if not actual.strip():
            try:
                await page.evaluate(
                    "([el, v]) => {"
                    "el.setAttribute('value', v);"
                    "el.value = v;"
                    "el.dispatchEvent("
                    "new Event('input',{bubbles:true}));"
                    "}", [el, value],
                )
            except Exception:
                pass

    if log:
        log.log_action("fill", sel, value[:30])
    return True


async def _select_country_code_7(page, phone_el):
    """Находит select/dropdown кода страны
    рядом с полем телефона и выбирает +7."""
    log = get_logger()
    try:
        changed = await page.evaluate(r"""el => {
            const form = el.closest('form')
                || el.closest('[class*="form" i]')
                || el.parentElement?.parentElement
                    ?.parentElement;
            if (!form) return false;

            // 1. <select> с кодами стран
            const sels = form.querySelectorAll('select');
            for (const s of sels) {
                const opts = Array.from(s.options);
                const has7 = opts.some(o =>
                    /^\+?7$/.test(o.value.trim())
                    || /russia|россия|\+7/i.test(
                        o.text)
                );
                if (!has7) continue;
                const ru = opts.find(o =>
                    /^\+?7$/.test(o.value.trim())
                    || /russia|россия/i.test(o.text)
                    || o.value === '7'
                    || o.value === '+7'
                    || o.getAttribute(
                        'data-phonecode'
                    ) === '7'
                    || o.getAttribute(
                        'data-code'
                    ) === '7'
                );
                if (ru) {
                    s.value = ru.value;
                    s.dispatchEvent(new Event(
                        'change', {bubbles: true}
                    ));
                    return 'select:' + ru.value;
                }
            }

            // 2. Tilda phone mask — скрытый select
            //    или data-phonemask-code
            const tildaSel = form.querySelector(
                'select.t-sel-phonemask,'
                + 'select[class*="phonemask" i],'
                + 'select[class*="phone-code" i],'
                + 'select[class*="country-code" i],'
                + 'select[name*="code" i]'
            );
            if (tildaSel) {
                const opts = Array.from(
                    tildaSel.options
                );
                const ru = opts.find(o =>
                    o.value === '+7'
                    || o.value === '7'
                    || /\+7/.test(o.text)
                    || /russia|россия/i.test(o.text)
                );
                if (ru) {
                    tildaSel.value = ru.value;
                    tildaSel.dispatchEvent(
                        new Event('change',
                            {bubbles: true})
                    );
                    return 'tilda:' + ru.value;
                }
            }

            // 3. intl-tel-input — кликаем на
            //    флаг России
            const iti = form.querySelector(
                '.iti__flag-container,'
                + '.intl-tel-input .flag-container,'
                + '[class*="iti__flag" i],'
                + '[class*="phone-flag" i],'
                + '[class*="country-flag" i]'
            );
            if (iti) {
                const flagBtn = iti.querySelector(
                    '.iti__selected-flag,'
                    + '.selected-flag,'
                    + '[role="combobox"],'
                    + 'div[class*="flag"]'
                );
                if (flagBtn) {
                    flagBtn.click();
                    return 'iti_opened';
                }
            }

            return false;
        }""", phone_el)

        if changed == 'iti_opened':
            await asyncio.sleep(0.5)
            ru_item = await page.query_selector(
                '[data-country-code="ru"],'
                '.iti__country[data-dial-code="7"],'
                'li[data-dial-code="7"],'
                'li[data-country-code="ru"]'
            )
            if ru_item:
                await ru_item.click()
                await asyncio.sleep(0.3)
            else:
                try:
                    await page.keyboard.press(
                        'Escape'
                    )
                except Exception:
                    pass

        if changed and log:
            log.log_action(
                "country_code", str(changed)[:40],
                "+7", success=True,
            )

    except Exception as e:
        if log:
            log.log_action(
                "country_code", "", "",
                success=False,
                error=str(e)[:80],
            )


async def smart_phone_fill(
    page, sel, phone, form_el=None
):
    log = get_logger()
    raw = re.sub(r'[^\d]', '', phone)
    if len(raw) == 11 and raw.startswith('8'):
        raw = '7' + raw[1:]
    phone7 = (
        f"+7{raw[-10:]}"
        if len(raw) >= 10 else phone
    )
    phone_short = raw[-10:]

    el = await find_el(page, sel)
    if not el:
        for fb_sel in PHONE_FALLBACKS:
            el = await find_el(
                page, fb_sel, timeout=600,
            )
            if el:
                sel = fb_sel
                break
    if not el:
        if log:
            log.log_action(
                "phone", sel, phone,
                success=False,
                error="элемент не найден",
            )
        return False

    try:
        await el.scroll_into_view_if_needed()
    except Exception:
        pass

    await _select_country_code_7(page, el)

    tilda = await _is_tilda(page)

    try:
        await el.click(timeout=2000)
    except Exception:
        try:
            await page.evaluate(
                "el => el.focus()", el
            )
        except Exception:
            pass

    await asyncio.sleep(0.3)

    if tilda:
        kb = _get_keyboard(page)
        try:
            await kb.press("Control+a")
            await asyncio.sleep(0.05)
            await kb.press("Backspace")
            await asyncio.sleep(0.1)
        except Exception:
            pass
        try:
            await kb.type(phone_short, delay=50)
        except Exception:
            await _slow_type(page, el, phone_short, 50)
        await asyncio.sleep(0.3)
        final = await page.evaluate(
            "el => el.value || ''", el
        ) or ""
        digits = re.sub(r'[^\d]', '', final)
        ok = len(digits) >= 10
        if log:
            log.log_action(
                "phone(tilda)", sel, final[:30],
                success=ok,
            )
        return ok

    cur = await page.evaluate(
        "el => el.value || ''", el
    ) or ""
    has_mask = bool(re.search(
        r'[_\(\)\-\+\s]{3,}', cur
    ))
    has_foreign_code = bool(re.search(
        r'^\+(?!7)\d{1,3}', cur.strip()
    ))

    if has_mask or has_foreign_code:
        try:
            await page.evaluate(r"""el => {
                const proto =
                    HTMLInputElement.prototype;
                const desc =
                    Object.getOwnPropertyDescriptor(
                        proto, 'value'
                    );
                if (desc && desc.set)
                    desc.set.call(el, '');
                else el.value = '';
                el.dispatchEvent(new Event(
                    'input', {bubbles: true}
                ));
                el.dispatchEvent(new Event(
                    'change', {bubbles: true}
                ));
            }""", el)
        except Exception:
            try:
                await el.fill("")
            except Exception:
                pass

        await asyncio.sleep(0.15)

        try:
            await el.click(timeout=1000)
        except Exception:
            pass

        await asyncio.sleep(0.15)
        cur2 = await page.evaluate(
            "el => el.value || ''", el,
        ) or ""

        if re.search(r'^\+\d{1,3}', cur2.strip()):
            try:
                await el.press("Home")
                await asyncio.sleep(0.05)
                for _ in range(25):
                    await el.press("Delete")
                await asyncio.sleep(0.1)
            except Exception:
                pass

        await _slow_type(
            page, el, phone_short, 80,
        )
    else:
        kb = _get_keyboard(page)
        try:
            await kb.press("Control+a")
            await asyncio.sleep(0.05)
            await kb.press("Delete")
            await asyncio.sleep(0.1)
        except Exception:
            try:
                await el.fill("")
            except Exception:
                pass
        await asyncio.sleep(0.15)
        prefill = await page.evaluate(
            "el => el.value || ''", el
        ) or ""
        if re.search(r'[\+\d]', prefill.strip()):
            await _slow_type(
                page, el, phone_short, 70,
            )
        else:
            await _slow_type(
                page, el, phone7, 70,
            )

    await asyncio.sleep(0.3)
    final = await page.evaluate(
        "el => el.value || ''", el
    ) or ""
    digits = re.sub(r'[^\d]', '', final)

    if len(digits) < 10:
        try:
            await page.evaluate(r"""el => {
                const proto =
                    HTMLInputElement.prototype;
                const desc =
                    Object.getOwnPropertyDescriptor(
                        proto, 'value'
                    );
                if (desc && desc.set)
                    desc.set.call(el, '');
                else el.value = '';
                el.dispatchEvent(new Event(
                    'input', {bubbles: true}
                ));
            }""", el)
        except Exception:
            try:
                await el.fill("")
            except Exception:
                pass
        await asyncio.sleep(0.15)
        await _slow_type(page, el, phone7, 70)
        await asyncio.sleep(0.2)
        final = await page.evaluate(
            "el => el.value || ''", el,
        ) or ""
        digits = re.sub(r'[^\d]', '', final)

    if len(digits) < 10:
        try:
            await react_patch_input(
                page, el, phone7,
            )
            await asyncio.sleep(0.2)
            final = await page.evaluate(
                "el => el.value || ''", el,
            ) or ""
            digits = re.sub(r'[^\d]', '', final)
        except Exception:
            pass

    ok = len(digits) >= 10
    if ok:
        method_used = "fill"
        if has_mask or has_foreign_code:
            method_used = "slow_type"
        try:
            from models import domain_from_url
            _phone_method_cache[
                domain_from_url(
                    page.url
                )
            ] = method_used
        except Exception:
            pass
    if log:
        log.log_action(
            "phone", sel, final[:30],
            success=ok,
            error="" if ok
            else f"только {len(digits)} цифр",
        )
    return ok


async def _select_first(page, sel, sel_type="native"):
    log = get_logger()
    el = await find_el(page, sel)
    if not el:
        if log:
            log.log_action(
                "select_first", sel, "",
                success=False,
                error="элемент не найден",
            )
        return False
    try:
        await page.evaluate(r"""el => {
            if (el.tagName === 'SELECT') {
                const opts = Array.from(el.options);
                const real = opts.find(
                    o => o.value
                        && o.value !== ''
                        && !o.disabled
                );
                if (real) {
                    el.value = real.value;
                    el.dispatchEvent(
                        new Event('change',
                            {bubbles: true})
                    );
                }
            }
        }""", el)
        if log:
            log.log_action("select_first", sel)
        return True
    except Exception as e:
        if log:
            log.log_action(
                "select_first", sel, "",
                success=False,
                error=str(e)[:120],
            )
        return False


async def _check_all_consent_boxes(
    page, form_el=None
):
    log = get_logger()
    n = 0

    try:
        unchecked = await page.evaluate(r"""root => {
            let scope = root || document;
            let cbs = scope.querySelectorAll(
                'input[type="checkbox"]');
            if (root && cbs.length === 0) {
                let up = root;
                for (let i = 0; i < 3 && up.parentElement; i++) {
                    up = up.parentElement;
                    const found = up.querySelectorAll(
                        'input[type="checkbox"]');
                    if (found.length > 0) { scope = up; cbs = found; break; }
                }
            }
            const results = [];
            const onlyOne = cbs.length === 1;
            for (const cb of cbs) {
                if (cb.checked) continue;
                let lblText = '';
                try {
                    if (cb.id) {
                        const l = scope.querySelector(
                            'label[for="'+cb.id+'"]');
                        if (l) lblText = (l.innerText||'');
                    }
                    if (!lblText) {
                        const pl = cb.closest('label');
                        if (pl) lblText = (pl.innerText||'');
                    }
                    if (!lblText && cb.parentElement) {
                        lblText = (cb.parentElement.innerText||'');
                    }
                } catch(e) {}
                const sig = (
                    (cb.name||'') + ' '
                    + (cb.id||'') + ' '
                    + (cb.className||'') + ' '
                    + lblText
                ).toLowerCase();
                const isConsent = /consent|agree|policy|accept|соглас|персональн|обработк|конфиденц|privacy|gdpr/.test(sig);
                const isRequired = cb.required
                    || cb.getAttribute(
                        'aria-required') === 'true';
                if (!(isConsent || isRequired || onlyOne))
                    continue;
                let wrapperSel = null;
                if (cb.id) {
                    const lbl = scope.querySelector(
                        'label[for="'+cb.id+'"]');
                    if (lbl) {
                        if (lbl.id) wrapperSel = '#' + lbl.id;
                        else if (lbl.className) {
                            const cls = lbl.className.toString()
                                .split(' ').filter(Boolean)[0];
                            if (cls) wrapperSel =
                                'label.' + cls + '[for="'+cb.id+'"]';
                        }
                        if (!wrapperSel)
                            wrapperSel = 'label[for="'+cb.id+'"]';
                    }
                }
                if (!wrapperSel) {
                    const parent = cb.parentElement;
                    if (parent) {
                        const pCls = (parent.className||'')
                            .toString().toLowerCase();
                        if (/checkbox|policy|consent|agree/.test(pCls)) {
                            const cls = parent.className.toString()
                                .split(' ').filter(Boolean)[0];
                            if (cls) wrapperSel =
                                parent.tagName.toLowerCase()
                                + '.' + cls;
                        }
                        const label = cb.closest('label');
                        if (!wrapperSel && label) {
                            if (label.className) {
                                const cls = label.className.toString()
                                    .split(' ').filter(Boolean)[0];
                                if (cls) wrapperSel = 'label.' + cls;
                            } else {
                                wrapperSel = null;
                            }
                        }
                    }
                }
                let cbSel = null;
                if (cb.id) cbSel = '#' + cb.id;
                else if (cb.name) cbSel =
                    'input[type="checkbox"][name="'+cb.name+'"]';
                results.push({cbSel, wrapperSel});
            }
            return results;
        }""", form_el)
    except Exception:
        unchecked = []

    for item in (unchecked or []):
        clicked = False
        wrapper_sel = item.get("wrapperSel")
        cb_sel = item.get("cbSel")

        if wrapper_sel:
            try:
                wrapper = await page.query_selector(
                    wrapper_sel)
                if wrapper:
                    await wrapper.click(timeout=2000)
                    await asyncio.sleep(0.2)
                    clicked = True
                    n += 1
            except Exception:
                pass

        if not clicked and cb_sel:
            try:
                cb_el = await page.query_selector(cb_sel)
                if cb_el:
                    await cb_el.click(
                        timeout=2000, force=True)
                    await asyncio.sleep(0.2)
                    clicked = True
                    n += 1
            except Exception:
                pass

        if not clicked and cb_sel:
            try:
                await page.evaluate(r"""sel => {
                    const cb = document.querySelector(sel);
                    if (!cb) return;
                    cb.checked = true;
                    cb.dispatchEvent(
                        new Event('change', {bubbles:true}));
                    cb.dispatchEvent(
                        new Event('click', {bubbles:true}));
                }""", cb_sel)
                n += 1
            except Exception:
                pass

    try:
        n += await page.evaluate(r"""root => {
            const scope = root || document;
            let fixed = 0;
            for (const cb of scope.querySelectorAll(
                'input[type="checkbox"]')) {
                if (cb.checked) continue;
                let lblText = '';
                try {
                    if (cb.id) {
                        const l = scope.querySelector(
                            'label[for="'+cb.id+'"]');
                        if (l) lblText = (l.innerText||'');
                    }
                    if (!lblText) {
                        const pl = cb.closest('label');
                        if (pl) lblText = (pl.innerText||'');
                    }
                    if (!lblText && cb.parentElement)
                        lblText = (cb.parentElement.innerText||'');
                } catch(e) {}
                const sig = (
                    (cb.name||'') + ' ' + (cb.id||'')
                    + ' ' + (cb.className||'')
                    + ' ' + lblText
                ).toLowerCase();
                const isConsent = /consent|agree|policy|accept|соглас|персональн|обработк|конфиденц|privacy|gdpr/.test(sig);
                const isRequired = cb.required
                    || cb.getAttribute('aria-required') === 'true';
                const onlyOne = scope.querySelectorAll(
                    'input[type="checkbox"]').length === 1;
                if (isConsent || isRequired || onlyOne) {
                    try {
                        cb.checked = true;
                        cb.dispatchEvent(
                            new Event('change', {bubbles:true}));
                        cb.dispatchEvent(
                            new Event('click', {bubbles:true}));
                        fixed++;
                    } catch(e) {}
                }
            }
            const radioNames = new Set();
            for (const rd of scope.querySelectorAll(
                'input[type="radio"]')) {
                if (!rd.name) continue;
                if (radioNames.has(rd.name)) continue;
                const group = scope.querySelectorAll(
                    'input[type="radio"][name="'+rd.name+'"]');
                const anyChecked = Array.from(group)
                    .some(r => r.checked);
                if (anyChecked) {
                    radioNames.add(rd.name);
                    continue;
                }
                const isReq = Array.from(group).some(
                    r => r.required
                        || r.getAttribute('aria-required') === 'true');
                if (isReq) {
                    try {
                        group[0].checked = true;
                        group[0].dispatchEvent(
                            new Event('change', {bubbles:true}));
                        group[0].dispatchEvent(
                            new Event('click', {bubbles:true}));
                        fixed++;
                    } catch(e) {}
                }
                radioNames.add(rd.name);
            }
            return fixed;
        }""", form_el) or 0
    except Exception:
        pass

    if log and n:
        log.ok(f"чекбоксы согласия: {n}")
    return n


async def _prefill_date_fields(
    page, form_el=None
):
    try:
        d = datetime.now() + timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        date_val = d.strftime("%Y-%m-%d")

        return await page.evaluate(r"""(args) => {
            const root = args.form || document;
            const dateVal = args.dateVal;
            let n = 0;
            const isShown = (el) => {
                const st = getComputedStyle(el);
                if (st.display === 'none'
                    || st.visibility === 'hidden'
                    || st.opacity === '0')
                    return false;
                const r = el.getBoundingClientRect();
                return r.width > 4 && r.height > 4;
            };
            const dateSels = [
                'input[type="date"]',
                'input[type="datetime-local"]',
                'input[name*="date" i]',
                'input[placeholder*="дата" i]',
                'input[placeholder*="date" i]',
            ].join(',');
            for (const el of
                root.querySelectorAll(dateSels)) {
                if (!isShown(el)) continue;
                if ((el.value || '').trim()) continue;
                try {
                    const proto =
                        HTMLInputElement.prototype;
                    const desc =
                        Object.getOwnPropertyDescriptor(
                            proto, 'value'
                        );
                    if (desc && desc.set)
                        desc.set.call(el, dateVal);
                    else el.value = dateVal;
                    el.dispatchEvent(
                        new Event('input',
                            {bubbles: true})
                    );
                    el.dispatchEvent(
                        new Event('change',
                            {bubbles: true})
                    );
                    n++;
                } catch(e) {}
            }
            return n;
        }""", {"form": form_el, "dateVal": date_val})
    except Exception:
        return 0


async def _heuristic_fill_phone(
    page, phone, form_el=None
):
    scope = form_el or page
    for sel in PHONE_FALLBACKS:
        try:
            el = await scope.query_selector(sel)
            if el and await el.is_visible():
                return await smart_phone_fill(
                    page, sel, phone, form_el
                )
        except Exception:
            continue
    return False


async def execute_action_plan(
    page, actions, phone,
    firstname, lastname, patronymic,
    email, comment,
    form_selector=None, step_dir=None,
):
    log = get_logger()
    form_el = await resolve_form_el(
        page, form_selector
    )
    filled = []
    phone_ok = False
    submit_sel = None

    if form_el:
        for _ in range(10):
            has_inputs = await page.evaluate(
                r"""fe => {
                const els = fe.querySelectorAll(
                    'input:not([type="hidden"])'
                    + ':not([type="submit"]),'
                    + 'textarea');
                for (const el of els) {
                    try {
                        const st = getComputedStyle(el);
                        if (st.display !== 'none'
                            && st.visibility !== 'hidden')
                            return true;
                    } catch(e) {}
                }
                return false;
            }""", form_el)
            if has_inputs:
                break
            await asyncio.sleep(0.5)

    await dismiss_popups(page, form_el)

    for act in sorted(
        actions, key=lambda a: a.get("step", 0)
    ):
        action = act.get("action", "")
        sel = act.get("selector", "")
        field = act.get("field", "")
        value_tmpl = act.get("value", "")

        if action == "submit":
            submit_sel = sel
            continue

        if not sel:
            continue

        if action == "fill":
            if field == "phone":
                ok = await smart_phone_fill(
                    page, sel, phone, form_el
                )
                if ok:
                    phone_ok = True
                    filled.append("phone")
            elif field == "date":
                val = _next_workday()
                ok = await _fill_field(
                    page, sel, val, field
                )
                if ok:
                    filled.append("date")
            elif field == "name":
                need_fio = await _check_need_fio(
                    page, sel,
                )
                if need_fio:
                    val = " ".join(
                        p for p in [
                            lastname, firstname,
                            patronymic,
                        ] if p
                    )
                else:
                    val = _resolve_value(
                        value_tmpl, phone,
                        firstname, lastname,
                        patronymic,
                        email, comment,
                    )
                ok = await _fill_field(
                    page, sel, val, field,
                )
                if ok:
                    filled.append(
                        "name(fio)"
                        if need_fio else "name"
                    )
            else:
                val = _resolve_value(
                    value_tmpl, phone,
                    firstname, lastname,
                    patronymic,
                    email, comment,
                )
                ok = await _fill_field(
                    page, sel, val, field
                )
                if ok:
                    filled.append(field)
        elif action == "click":
            el = await find_el(page, sel)
            if el:
                await smart_click(page, el)
                if log:
                    log.log_action("click", sel)
                filled.append(field)
        elif action == "select_first":
            ok = await _select_first(
                page, sel,
                act.get("type", "native"),
            )
            if ok:
                filled.append(field)

        await asyncio.sleep(0.15)

    if not phone_ok:
        phone_ok = await _heuristic_fill_phone(
            page, phone, form_el
        )
        if phone_ok:
            filled.append("phone(fallback)")

    date_count = await _prefill_date_fields(
        page, form_el
    )
    if date_count:
        filled.append(f"дата ×{date_count} (авто)")

    await _check_all_consent_boxes(page, form_el)

    await step_shot(
        page, "before_submit", step_dir,
        form_el=form_el,
    )

    return {
        "filled": filled,
        "phone_ok": phone_ok,
        "submit_sel": submit_sel,
        "form_el": form_el,
    }


async def do_submit(page, submit_sel, form_el=None):
    log = get_logger()
    if form_el:
        try:
            action = await page.evaluate(
                "f => (f.action || '').toLowerCase()",
                form_el,
            )
            bad_actions = [
                "/search", "/login", "/register",
                "/subscribe", "/unsubscribe",
            ]
            for ba in bad_actions:
                if ba in action:
                    if log:
                        log.warn(
                            f"submit отменён: action "
                            f"ведёт на {ba}"
                        )
                    return False
        except Exception:
            pass
    if submit_sel:
        el = await find_el(page, submit_sel)
        if el:
            ok = await smart_click(
                page, el, aggressive=True,
            )
            if ok:
                if log:
                    log.log_action(
                        "submit", submit_sel,
                    )
                await asyncio.sleep(3.5)
                return True

    if form_el:
        for sel in [
            'button[type="submit"]',
            'input[type="submit"]',
            'button:not([type])',
        ]:
            try:
                btn = await form_el.query_selector(sel)
                if btn:
                    await smart_click(
                        page, btn, aggressive=True,
                    )
                    if log:
                        log.log_action(
                            "submit", f"form>{sel}",
                        )
                    await asyncio.sleep(3.5)
                    return True
            except Exception:
                continue

    for sel in [
        'button[type="submit"]',
        'input[type="submit"]',
        'button:not([type])',
    ]:
        el = await find_el(page, sel)
        if el:
            await smart_click(page, el, aggressive=True)
            if log:
                log.log_action("submit", sel)
            await asyncio.sleep(3.5)
            return True

    try:
        submit_btn = await page.evaluate(r"""() => {
            const texts = [
                'отправить','записаться','заказать',
                'submit','send','получить',
                'оставить заявку','заказать звонок',
            ];
            const all = document.querySelectorAll(
                'button, [role="button"], '
                + 'a.btn, div.btn, span.btn, '
                + '[class*="btn" i], [class*="submit" i]'
            );
            for (const el of all) {
                const st = getComputedStyle(el);
                if (st.display === 'none'
                    || st.visibility === 'hidden')
                    continue;
                const r = el.getBoundingClientRect();
                if (r.width < 20 || r.height < 15) continue;
                const t = (el.innerText || '').trim()
                    .toLowerCase();
                if (t.length > 40) continue;
                if (texts.some(s => t.includes(s))) {
                    if (el.id) return '#' + el.id;
                    if (el.className) {
                        const cls = el.className.toString()
                            .split(' ').filter(Boolean)[0];
                        if (cls) return el.tagName
                            .toLowerCase() + '.' + cls;
                    }
                    return null;
                }
            }
            return null;
        }""")
        if submit_btn:
            el = await find_el(page, submit_btn)
            if el:
                await smart_click(
                    page, el, aggressive=True,
                )
                if log:
                    log.log_action(
                        "submit", submit_btn,
                    )
                await asyncio.sleep(3.5)
                return True
    except Exception:
        pass

    if form_el:
        try:
            await page.evaluate(
                r"f => { try{f.requestSubmit();"
                r"}catch(e){f.submit();} }",
                form_el,
            )
            if log:
                log.log_action(
                    "submit", "requestSubmit()",
                )
            await asyncio.sleep(3.5)
            return True
        except Exception:
            pass

    if log:
        log.err("submit", "кнопка submit не найдена")
    return False


async def _check_need_fio(page, sel) -> bool:
    try:
        return bool(await page.evaluate(
            r"""sel => {
            const el = document.querySelector(sel);
            if (!el) return false;
            const ph = (
                el.placeholder || ''
            ).toLowerCase();
            const nm = (el.name || '').toLowerCase();
            const lbl = (() => {
                if (el.id) {
                    const l = document.querySelector(
                        'label[for="' + el.id + '"]'
                    );
                    if (l) return (
                        l.innerText || ''
                    ).toLowerCase();
                }
                const p = el.closest('label');
                if (p) return (
                    p.innerText || ''
                ).toLowerCase();
                return '';
            })();
            const sig = ph + ' ' + nm + ' ' + lbl;
            if (/фио|ф\.и\.о|фамилия.+имя|полное имя|full\s*name/.test(sig))
                return true;
            if (/фамилия|surname|last.?name/.test(sig))
                return true;
            return false;
        }""", sel))
    except Exception:
        return False


async def fill_all_empty_fields(
    page, phone,
    firstname, lastname, patronymic,
    email, comment,
    form_el=None,
):
    log = get_logger()
    try:
        empties = await page.evaluate(r"""root => {
            const scope = root || document;
            const result = [];
            const isShown = (el) => {
                try {
                    const st = getComputedStyle(el);
                    if (st.display === 'none'
                        || st.visibility === 'hidden')
                        return false;
                    const r =
                        el.getBoundingClientRect();
                    return r.width > 6
                        && r.height > 6;
                } catch(e) { return false; }
            };
            for (const el of scope.querySelectorAll(
                'input:not([type="hidden"])'
                + ':not([type="submit"])'
                + ':not([type="button"])'
                + ':not([type="checkbox"])'
                + ':not([type="radio"]),'
                + 'textarea, select'
            )) {
                if (!isShown(el)) continue;
                const val = (
                    el.value || ''
                ).trim();
                if (val) continue;
                const tp = (
                    el.type || ''
                ).toLowerCase();
                const nm = (
                    el.name || ''
                ).toLowerCase();
                const ph = (
                    el.placeholder || ''
                ).toLowerCase();
                const tag =
                    el.tagName.toLowerCase();
                let sel = null;
                if (el.id)
                    sel = '#' + el.id;
                else if (el.name)
                    sel = tag
                        + '[name="'+el.name+'"]';
                else if (el.placeholder)
                    sel = tag
                        + '[placeholder="'
                        + el.placeholder + '"]';
                if (!sel) continue;
                let role = 'unknown';
                if (tp==='tel'
                    || /phone|tel|телефон/.test(
                        nm+' '+ph))
                    role = 'phone';
                else if (tp==='email'
                    || /email|почт/.test(nm+' '+ph))
                    role = 'email';
                else if (/name|имя|фио/.test(
                    nm+' '+ph))
                    role = 'name';
                else if (/comment|сообщ|вопрос/.test(
                    nm+' '+ph))
                    role = 'comment';
                else if (tp==='date'
                    || /дата|date/.test(nm+' '+ph))
                    role = 'date';
                else if (tag === 'select')
                    role = 'dropdown';
                else if (tag === 'textarea')
                    role = 'comment';
                if (/captcha|capcha|код.с.картинк|verification.?code|security.?code|проверочн/i
                    .test(nm + ' ' + ph + ' '
                        + (el.id||'')
                        + ' ' + (el.className||'')))
                    role = 'captcha';
                result.push({sel, role, tp, nm, ph});
            }
            return result;
        }""", form_el)
    except Exception:
        return 0

    fixed = 0
    for f in (empties or []):
        sel = f["sel"]
        role = f["role"]
        try:
            if role == "captcha":
                continue
            if role == "phone":
                ok = await smart_phone_fill(
                    page, sel, phone, form_el,
                )
                if ok:
                    fixed += 1
            elif role == "email":
                ok = await _fill_field(
                    page, sel, email, "email",
                )
                if ok:
                    fixed += 1
            elif role == "name":
                need_fio = await _check_need_fio(
                    page, sel,
                )
                if need_fio:
                    val = " ".join(
                        p for p in [
                            lastname, firstname,
                            patronymic,
                        ] if p
                    )
                else:
                    val = firstname
                ok = await _fill_field(
                    page, sel, val, "name",
                )
                if ok:
                    fixed += 1
            elif role == "comment":
                ok = await _fill_field(
                    page, sel, comment, "comment",
                )
                if ok:
                    fixed += 1
            elif role == "date":
                ok = await _fill_field(
                    page, sel, _next_workday(),
                    "date",
                )
                if ok:
                    fixed += 1
            elif role == "dropdown":
                ok = await _select_first(page, sel)
                if ok:
                    fixed += 1
            else:
                ok = await _fill_field(
                    page, sel, firstname,
                    "unknown",
                )
                if ok:
                    fixed += 1
        except Exception:
            continue
    if log and fixed:
        log.ok(
            f"дозаполнено {fixed} пустых полей (все)"
        )
    return fixed


async def _fix_invalid_fields(
    page, hints, phone,
    firstname, lastname, patronymic,
    email, comment,
):
    log = get_logger()
    fixed = 0
    for h in hints:
        tp = h.get("type", "")
        name = h.get("name", "").lower()
        ph = h.get("ph", "").lower()
        msg = h.get("msg", "").lower()
        sig = f"{tp} {name} {ph}"

        sel = None
        if h.get("name"):
            tag = h.get("tag", "input")
            sel = f'{tag}[name="{h["name"]}"]'
        elif h.get("ph"):
            tag = h.get("tag", "input")
            sel = f'{tag}[placeholder="{h["ph"]}"]'
        if not sel:
            continue

        try:
            if tp == "tel" or "phone" in sig or "телефон" in sig:
                ok = await smart_phone_fill(
                    page, sel, phone,
                )
                if ok:
                    fixed += 1
            elif tp == "email" or "email" in sig or "почт" in sig:
                ok = await _fill_field(
                    page, sel, email, "email",
                )
                if ok:
                    fixed += 1
            elif "name" in sig or "имя" in sig or "фио" in sig:
                val = firstname
                if "фио" in sig or "фамили" in sig:
                    val = " ".join(
                        p for p in [
                            lastname, firstname,
                            patronymic,
                        ] if p
                    )
                ok = await _fill_field(
                    page, sel, val, "name",
                )
                if ok:
                    fixed += 1
            elif "checkbox" in tp:
                el = await find_el(page, sel)
                if el:
                    try:
                        await page.evaluate(
                            r"""el => {
                            el.checked = true;
                            el.dispatchEvent(
                                new Event('change',
                                    {bubbles: true}));
                            }""", el,
                        )
                        fixed += 1
                    except Exception:
                        pass
            elif (
                "обязательно" in msg
                or "required" in msg
                or "заполните" in msg
            ):
                ok = await _fill_field(
                    page, sel, firstname, "unknown",
                )
                if ok:
                    fixed += 1
        except Exception:
            continue
    if log and fixed:
        log.ok(
            f"исправлено {fixed} невалидных полей"
        )
    return fixed


async def submit_with_retry(
    page, submit_sel, form_el,
    phone, firstname, lastname,
    patronymic, email, comment,
    step_dir=None, max_submits=3,
    captcha_unresolved=False,
    page_for_shot=None,
    rucaptcha_key="",
):
    from result_detect import (
        capture_pre_submit_text,
        detect_submission_result,
        get_invalid_field_hint,
        setup_xhr_listener,
    )
    log = get_logger()
    shot_page = page_for_shot or page

    await setup_xhr_listener(page)

    pre_text = await capture_pre_submit_text(
        page, form_el,
    )
    pre_url = page.url

    for attempt in range(1, max_submits + 1):
        if log:
            log.step(
                "submit",
                f"попытка {attempt}/{max_submits}",
            )
        await setup_xhr_listener(page)
        submitted = await do_submit(
            page, submit_sel, form_el,
        )
        if not submitted:
            return {"state": "submit_failed"}

        await step_shot(
            shot_page,
            f"03_after_submit_{attempt}",
            step_dir,
            form_el=form_el,
        )

        post_url = page.url
        url_changed = (
            pre_url.rstrip("/")
            != post_url.rstrip("/")
        )
        dom = await detect_submission_result(
            page, form_el, pre_text,
            url_changed=url_changed,
        )
        state = dom.get("state", "unchanged")

        if state == "unchanged":
            await asyncio.sleep(2)
            post_url2 = page.url
            url_changed2 = (
                pre_url.rstrip("/")
                != post_url2.rstrip("/")
            )
            dom2 = await detect_submission_result(
                page, form_el, pre_text,
                url_changed=url_changed2,
            )
            s2 = dom2.get("state", "unchanged")
            if s2 != "unchanged":
                dom = dom2
                state = s2

        if state == "likely_success":
            state = "success"
            dom["state"] = "success"
        elif state == "likely_failed":
            state = "unchanged"
            dom["state"] = "unchanged"

        if captcha_unresolved and state == "success":
            dom["state"] = "captcha"
            dom["match"] = "captcha not solved, success blocked"
            state = "captcha"

        if state == "success":
            return dom

        # Проверка капчи после submit
        if state in (
            "unchanged", "error",
            "captcha_required",
        ) and attempt == 1:
            from captcha import (
                handle_captcha,
                handle_post_submit_captcha,
                _handle_tilda_needcaptcha,
            )
            post_captcha = None
            is_tilda_nc = (
                state == "captcha_required"
                and "needcaptcha" in (
                    dom.get("match", "")
                )
            )
            if is_tilda_nc:
                try:
                    post_captcha = (
                        await _handle_tilda_needcaptcha(
                            page, page.url,
                            rucaptcha_key,
                        )
                    )
                except Exception:
                    pass
            if not post_captcha:
                try:
                    post_captcha = (
                        await handle_post_submit_captcha(
                            page, page.url,
                            rucaptcha_key,
                        )
                    )
                except Exception:
                    pass
            if not post_captcha:
                try:
                    post_captcha = await handle_captcha(
                        page, page.url,
                        rucaptcha_key,
                        has_captcha_hint=True,
                    )
                except Exception:
                    pass
            if post_captcha == "tilda_auto_submitted":
                if log:
                    log.ok(
                        "капча решена, Tilda "
                        "авто-ресабмит: success"
                    )
                return {
                    "state": "success",
                    "match": "tilda_auto_resubmit",
                }
            if post_captcha == "ok":
                if log:
                    log.ok("капча после submit решена")
                await setup_xhr_listener(page)
                await do_submit(
                    page, submit_sel, form_el,
                )
                await asyncio.sleep(3.5)
                try:
                    dom2 = (
                        await detect_submission_result(
                            page, form_el, pre_text,
                            url_changed=(
                                pre_url.rstrip("/")
                                != page.url.rstrip("/")
                            ),
                        )
                    )
                except Exception:
                    dom2 = {
                        "state": "unchanged",
                        "match": "",
                    }
                s2 = dom2.get("state", "unchanged")
                if s2 in (
                    "success", "likely_success",
                ):
                    dom2["state"] = "success"
                    return dom2
                if s2 == "unchanged":
                    await asyncio.sleep(2)
                    try:
                        dom3 = (
                            await detect_submission_result(
                                page, form_el,
                                pre_text,
                                url_changed=(
                                    pre_url.rstrip("/")
                                    != page.url.rstrip(
                                        "/"
                                    )
                                ),
                            )
                        )
                    except Exception:
                        dom3 = {
                            "state": "unchanged",
                            "match": "",
                        }
                    s3 = dom3.get(
                        "state", "unchanged"
                    )
                    if s3 in (
                        "success", "likely_success",
                    ):
                        dom3["state"] = "success"
                        return dom3

        if state == "captcha_required":
            return dom

        if state == "error" and any(
            kw in (dom.get("match", "").lower())
            for kw in (
                "превысили", "лимит",
                "too many", "rate limit",
                "слишком много",
            )
        ):
            if log:
                log.warn(
                    f"rate limit: {dom.get('match','')}"
                    f", повтор бесполезен"
                )
            return dom

        if state in (
            "validation_error", "error",
        ) and attempt < max_submits:
            if log:
                log.warn(
                    f"submit #{attempt}: {state} "
                    f"({dom.get('match', '')}), "
                    f"пробуем исправить"
                )
            hints = await get_invalid_field_hint(
                page,
            )
            if log and hints:
                log.step(
                    "fix_validation",
                    f"невалидных: {len(hints)}, "
                    + ", ".join(
                        h.get("name") or h.get("ph")
                        or h.get("type", "?")
                        for h in hints[:3]
                    ),
                )
            await _check_all_consent_boxes(
                page, form_el,
            )
            fixed = await fill_all_empty_fields(
                page, phone,
                firstname, lastname, patronymic,
                email, comment, form_el,
            )
            fix_inv = await _fix_invalid_fields(
                page, hints or [],
                phone, firstname, lastname,
                patronymic, email, comment,
            )
            fixed += fix_inv
            await _prefill_date_fields(
                page, form_el,
            )
            if not fixed:
                if log:
                    log.warn(
                        "нечего исправлять, "
                        "повтор бесполезен"
                    )
                return dom
            await asyncio.sleep(0.5)
            continue

        return dom

    return dom
