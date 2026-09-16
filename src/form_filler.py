import asyncio
import re
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse

from config import PHONE_FALLBACKS
from logger import get_logger
from browser_utils import (
    find_el, react_patch_input, smart_click,
    step_shot, dismiss_popups,
)
from form_finder import resolve_form_el

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
    log = get_logger()
    try:
        changed = await page.evaluate(r"""el => {
            const form = el.closest('form')
                || el.closest('[class*="form" i]')
                || el.parentElement?.parentElement
                    ?.parentElement;
            if (!form) return false;

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

            const pm = el.closest(
                '.t-input-phonemask__wrap'
            ) || el.closest('.t-input-block');
            if (pm) {
                const curCode =
                    el.getAttribute(
                        'data-phonemask-code'
                    )
                    || pm.getAttribute(
                        'data-phonemask-code'
                    )
                    || (pm.querySelector(
                        '[data-phonemask-code]'
                    ) || {}).getAttribute?.(
                        'data-phonemask-code'
                    )
                    || '';
                if (curCode === '+7')
                    return 'tilda_api:already_+7';
                if (typeof
                    t_form_phonemask__handleUpdateCountry
                        === 'function') {
                    try {
                        t_form_phonemask__handleUpdateCountry(
                            pm, {
                                code: '+7',
                                mask: '+7(000) 000-00-00',
                                iso: 'ru',
                                silent: false,
                                noFocus: false
                            }
                        );
                        el.value = '';
                        return 'tilda_api:+7';
                    } catch (e) { /* fallthrough */ }
                }
                const flagEl = pm.querySelector(
                    '.t-input-phonemask__select-flag,'
                    + '.t-input-phonemask__flag,'
                    + '[class*="phonemask__flag"],'
                    + '[class*="phonemask__select"]'
                );
                if (flagEl) {
                    flagEl.click();
                    return 'tilda_flag_clicked';
                }
            }

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

        if changed == 'tilda_flag_clicked':
            await asyncio.sleep(0.5)
            ru_item = await page.query_selector(
                '.t-input-phonemask__country-item'
                '[data-phonemask-code="+7"],'
                '.t-input-phonemask__country-item'
                '[data-code="+7"],'
                '[data-phonemask-code="+7"],'
                '[data-code="7"],'
                '[data-country-code="ru"]'
            )
            if ru_item:
                await ru_item.click()
                await asyncio.sleep(0.3)
            else:
                try:
                    items = await page.query_selector_all(
                        '.t-input-phonemask__country-item,'
                        '[class*="phonemask__country"]'
                    )
                    for item in items:
                        txt = await page.evaluate(
                            "el => el.textContent||''",
                            item,
                        )
                        if '+7' in txt or 'Россия' in txt \
                                or 'Russia' in txt:
                            await item.click()
                            break
                except Exception:
                    pass
                await asyncio.sleep(0.3)

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
        ok = digits[-10:] == phone_short
        if not ok and log:
            log.warn(
                f"phone(tilda) mismatch: got "
                f"{digits[-10:]!r} != {phone_short!r}"
            )

        if not ok:
            switched = await page.evaluate(r"""el => {
                const pm = el.closest(
                    '.t-input-phonemask__wrap'
                ) || el.closest('.t-input-block');
                if (!pm) return false;
                const flag = pm.querySelector(
                    '.t-input-phonemask__select-flag,'
                    + '[class*="phonemask__flag"],'
                    + '[class*="phonemask__select"]'
                );
                if (flag) { flag.click(); return true; }
                return false;
            }""", el)
            if switched:
                await asyncio.sleep(0.5)
                ru_item = await page.query_selector(
                    '[data-phonemask-code="+7"],'
                    '[data-code="+7"],'
                    '[data-country-code="ru"]'
                )
                if not ru_item:
                    items = await page.query_selector_all(
                        '.t-input-phonemask__country-item,'
                        '[class*="phonemask__country"]'
                    )
                    for item in items:
                        txt = await page.evaluate(
                            "el => el.textContent||''",
                            item,
                        )
                        if '+7' in txt or 'Russia' in txt:
                            ru_item = item
                            break
                if ru_item:
                    try:
                        await ru_item.click(
                            timeout=3000
                        )
                    except Exception:
                        pass
                    else:
                        await asyncio.sleep(0.5)
                        try:
                            await el.click(timeout=1000)
                        except Exception:
                            pass
                        await asyncio.sleep(0.2)
                        try:
                            await kb.press("Control+a")
                            await asyncio.sleep(0.05)
                            await kb.press("Backspace")
                            await asyncio.sleep(0.1)
                            await kb.type(
                                phone_short, delay=50
                            )
                        except Exception:
                            await _slow_type(
                                page, el,
                                phone_short, 50
                            )
                        await asyncio.sleep(0.3)
                        final = await page.evaluate(
                            "el => el.value || ''", el
                        ) or ""
                        digits = re.sub(
                            r'[^\d]', '', final
                        )
                        ok = digits[-10:] == phone_short

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
    mask_hint = False
    try:
        mask_hint = bool(await page.evaluate(r"""el => {
            try {
                const ph = (el.placeholder || '');
                if (/[_]{2,}|\(\s*_|\(\s*\d{3}/.test(ph))
                    return true;
                const cls = (
                    el.className || ''
                ).toLowerCase();
                if (/mask|phonemask|iti__|intl-tel/
                    .test(cls))
                    return true;
                for (const a of [
                    'data-mask',
                    'data-phonemask-code',
                    'data-tel-input',
                    'data-inputmask',
                ]) if (el.getAttribute(a)) return true;
                return false;
            } catch (e) { return false; }
        }""", el))
    except Exception:
        mask_hint = False
    masked = has_mask or has_foreign_code or mask_hint
    retry_val = phone_short if masked else phone7

    if masked:
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
        await _slow_type(page, el, retry_val, 70)
        await asyncio.sleep(0.2)
        final = await page.evaluate(
            "el => el.value || ''", el,
        ) or ""
        digits = re.sub(r'[^\d]', '', final)

    if len(digits) < 10:
        try:
            await react_patch_input(
                page, el, retry_val,
            )
            await asyncio.sleep(0.2)
            final = await page.evaluate(
                "el => el.value || ''", el,
            ) or ""
            digits = re.sub(r'[^\d]', '', final)
        except Exception:
            pass

    if len(digits) < 10:
        try:
            await _input_event_phone(
                page, el, phone_short,
            )
            await asyncio.sleep(0.3)
            final = await page.evaluate(
                "el => el.value || ''", el,
            ) or ""
            digits = re.sub(r'[^\d]', '', final)
        except Exception:
            pass

    got = re.sub(r'\D', '', final)
    ok = got[-10:] == phone_short
    if not ok and log:
        log.warn(
            f"phone value mismatch: got "
            f"{got[-10:]!r} != {phone_short!r} "
            f"(raw {final[:30]!r})"
        )

    retries = 0
    while not ok and retries < 2:
        retries += 1
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
        try:
            await el.click(timeout=1000)
        except Exception:
            try:
                await page.evaluate(
                    "el => el.focus()", el,
                )
            except Exception:
                pass
        await asyncio.sleep(0.18)
        await _slow_type(
            page, el, phone_short, 80,
        )
        await asyncio.sleep(0.3)
        final = await page.evaluate(
            "el => el.value || ''", el,
        ) or ""
        got = re.sub(r'\D', '', final)
        ok = got[-10:] == phone_short
        if not ok and log:
            log.warn(
                f"phone retry #{retries} mismatch: "
                f"got {got[-10:]!r} != {phone_short!r}"
            )

    if log:
        log.log_action(
            "phone", sel, final[:30],
            success=ok,
            error="" if ok
            else f"value mismatch got={got[-10:]}",
        )
    return ok

async def _input_event_phone(page, el, digits):
    await page.evaluate(r"""([el, digits]) => {
        try {
            const proto = HTMLInputElement.prototype;
            const desc = Object.getOwnPropertyDescriptor(
                proto, 'value');
            const setVal = (v) => {
                if (desc && desc.set) desc.set.call(el, v);
                else el.value = v;
            };
            el.focus();
            setVal('');
            el.dispatchEvent(new Event('input',
                {bubbles: true}));
            for (const ch of digits) {
                try {
                    el.dispatchEvent(new InputEvent(
                        'beforeinput',
                        {inputType: 'insertText',
                         data: ch, bubbles: true,
                         cancelable: true}));
                } catch (_) {}
                setVal((el.value || '') + ch);
                try {
                    el.dispatchEvent(new InputEvent(
                        'input',
                        {inputType: 'insertText',
                         data: ch, bubbles: true}));
                } catch (_) {
                    el.dispatchEvent(new Event('input',
                        {bubbles: true}));
                }
            }
            el.dispatchEvent(new Event('change',
                {bubbles: true}));
            el.dispatchEvent(new Event('blur',
                {bubbles: true}));
        } catch (e) {}
    }""", [el, digits])

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

async def collect_form_fields(page, form_el=None):
    log = get_logger()
    try:
        data = await page.evaluate(r"""root => {
            const form = root
                || document.querySelector('form')
                || document;
            const fields = {};
            const hidden = {};
            const els = form.querySelectorAll(
                'input, select, textarea');
            for (const el of els) {
                const name = el.name;
                if (!name) continue;
                if (el.disabled) continue;
                const tag = el.tagName.toLowerCase();
                const tp = (el.type || '').toLowerCase();
                if (tag === 'input'
                    && (tp === 'checkbox'
                        || tp === 'radio')) {
                    if (!el.checked) continue;
                    fields[name] = el.value || 'on';
                    continue;
                }
                if (tag === 'select') {
                    if (el.multiple) {
                        const vals = Array.from(
                            el.selectedOptions)
                            .map(o => o.value);
                        if (vals.length)
                            fields[name] = vals.join(',');
                    } else if (el.value) {
                        fields[name] = el.value;
                    }
                    continue;
                }
                if (tp === 'submit' || tp === 'button'
                    || tp === 'file' || tp === 'image')
                    continue;
                const val = el.value || '';
                fields[name] = val;
                if (tp === 'hidden')
                    hidden[name] = val;
            }
            return {fields, hidden};
        }""", form_el)
    except Exception:
        return {"fields": {}, "hidden": {}, "has_csrf": False}

    fields = (data or {}).get("fields", {}) or {}
    hidden = (data or {}).get("hidden", {}) or {}
    csrf_re = re.compile(
        r"csrf|_?token|nonce|authenticity", re.I
    )
    csrf_names = [
        n for n in hidden if csrf_re.search(n)
    ]
    has_csrf = bool(csrf_names)
    if log:
        if has_csrf:
            log.step(
                "form_fields",
                f"hidden={len(hidden)}, "
                f"csrf/token поля: "
                f"{', '.join(csrf_names)}",
            )
        elif hidden:
            log.step(
                "form_fields",
                f"hidden={len(hidden)} "
                "(csrf/token не найден)",
            )
    return {
        "fields": fields,
        "hidden": hidden,
        "has_csrf": has_csrf,
    }

async def _looks_like_phone_field(page, el) -> bool:
    if el is None:
        return False
    try:
        return await page.evaluate(r"""el => {
            try {
                if (!el || el.tagName !== 'INPUT')
                    return false;
                const t = (el.type || '').toLowerCase();
                if (t === 'tel') return true;
                const im = (
                    el.getAttribute('inputmode') || ''
                ).toLowerCase();
                if (im === 'tel' || im === 'numeric')
                    return true;
                const haystack = [
                    el.name, el.id,
                    el.placeholder,
                    el.getAttribute('aria-label'),
                    el.getAttribute('data-field'),
                    el.getAttribute('data-name'),
                    el.getAttribute('autocomplete'),
                    el.className,
                ].filter(Boolean).join(' ').toLowerCase();
                if (/phone|телеф|моб|\btel\b/.test(haystack))
                    return true;
                const sample = (
                    (el.value || '')
                    + ' '
                    + (el.placeholder || '')
                );
                if (/\+\s*7|\(\s*\d{3}|_{3,}/.test(sample))
                    return true;
                return false;
            } catch (e) { return false; }
        }""", el)
    except Exception:
        return False

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

async def _fill_required_empty(
    page, form_el, phone,
    firstname, lastname, patronymic,
    email, comment,
    honeypots=None,
):
    log = get_logger()
    honeypot_set = set(honeypots or [])
    try:
        reqs = await page.evaluate(r"""root => {
            const scope = root || document;
            const out = [];
            const isShown = (el) => {
                try {
                    const st = getComputedStyle(el);
                    if (st.display === 'none'
                        || st.visibility === 'hidden')
                        return false;
                    const r =
                        el.getBoundingClientRect();
                    return r.width > 6 && r.height > 6;
                } catch(e) { return false; }
            };
            for (const el of scope.querySelectorAll(
                'input:not([type="hidden"])'
                + ':not([type="submit"])'
                + ':not([type="button"])'
                + ':not([type="checkbox"])'
                + ':not([type="radio"]),'
                + 'textarea'
            )) {
                if (!isShown(el)) continue;
                const req = el.required
                    || el.getAttribute(
                        'aria-required') === 'true';
                let invalid = false;
                try {
                    invalid = el.matches(':invalid');
                } catch(e) {}
                if (!req && !invalid) continue;
                if ((el.value || '').trim()) continue;
                const tp = (el.type || '')
                    .toLowerCase();
                const nm = (el.name || '')
                    .toLowerCase();
                const ph = (el.placeholder || '')
                    .toLowerCase();
                const tag = el.tagName.toLowerCase();
                let sel = null;
                if (el.id) sel = '#' + el.id;
                else if (el.name)
                    sel = tag
                        + '[name="'+el.name+'"]';
                else if (el.placeholder)
                    sel = tag
                        + '[placeholder="'
                        + el.placeholder + '"]';
                if (!sel) continue;
                let role = 'name';
                if (tp === 'tel'
                    || /phone|tel|телефон/.test(
                        nm + ' ' + ph))
                    role = 'phone';
                else if (tp === 'email'
                    || /email|почт/.test(nm + ' ' + ph))
                    role = 'email';
                else if (tag === 'textarea'
                    || /comment|сообщ|вопрос/.test(
                        nm + ' ' + ph))
                    role = 'comment';
                out.push({sel, role});
            }
            return out;
        }""", form_el)
    except Exception:
        return 0

    if honeypot_set:
        reqs = [
            f for f in (reqs or [])
            if f.get("sel") not in honeypot_set
        ]

    fixed = 0
    for f in (reqs or []):
        sel = f["sel"]
        role = f["role"]
        try:
            if role == "phone":
                ok = await smart_phone_fill(
                    page, sel, phone, form_el,
                )
            elif role == "email":
                ok = await _fill_field(
                    page, sel, email, "email",
                )
            elif role == "comment":
                ok = await _fill_field(
                    page, sel, comment, "comment",
                )
            else:
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
        except Exception:
            continue
    if log and fixed:
        log.ok(
            f"дозаполнено обязательных пустых "
            f"(пред-submit): {fixed}"
        )
    return fixed

async def execute_action_plan(
    page, actions, phone,
    firstname, lastname, patronymic,
    email, comment,
    form_selector=None, step_dir=None,
    honeypots=None,
    csrf_token=None,
):
    log = get_logger()
    form_el = await resolve_form_el(
        page, form_selector
    )
    filled = []
    phone_ok = False
    submit_sel = None

    honeypot_set = set(honeypots or [])
    csrf_sel = None
    if csrf_token and isinstance(csrf_token, dict):
        csrf_sel = csrf_token.get("selector")
    if csrf_sel:
        honeypot_set.add(csrf_sel)

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

        if sel in honeypot_set:
            if log:
                log.warn(
                    f"пропуск honeypot/csrf: {sel}"
                )
            continue

        if action == "fill":
            if field == "phone":
                target_el = await find_el(page, sel)
                phone_target_ok = (
                    target_el is not None
                    and await _looks_like_phone_field(
                        page, target_el,
                    )
                )
                if not phone_target_ok:
                    if log:
                        log.warn(
                            f"phone target не подходит "
                            f"({sel!r}), фолбэк на эвристику"
                        )
                    ok = await _heuristic_fill_phone(
                        page, phone, form_el,
                    )
                else:
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

    req_fixed = await _fill_required_empty(
        page, form_el, phone,
        firstname, lastname, patronymic,
        email, comment,
        honeypots=list(honeypot_set),
    )
    if req_fixed:
        filled.append(f"обязательные ×{req_fixed}")

    await step_shot(
        page, "before_submit", step_dir,
        form_el=form_el,
    )

    return {
        "filled": filled,
        "phone_ok": phone_ok,
        "submit_sel": submit_sel,
        "form_el": form_el,
        "honeypots": list(honeypot_set),
    }

async def do_submit(page, submit_sel, form_el=None):
    log = get_logger()
    if form_el:
        try:
            await collect_form_fields(page, form_el)
        except Exception:
            pass
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

    _fallback_sels = [
        'button[type="submit"]',
        'input[type="submit"]',
        'button:not([type])',
    ]
    if form_el:
        for sel in _fallback_sels:
            try:
                btn = await form_el.query_selector(sel)
                if btn:
                    await smart_click(
                        page, btn, aggressive=True,
                    )
                    if log:
                        log.log_action("submit", f"form>{sel}")
                    await asyncio.sleep(3.5)
                    return True
            except Exception:
                continue

    for sel in _fallback_sels:
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
    honeypots=None,
):
    log = get_logger()
    honeypot_set = set(honeypots or [])
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
                try {
                    const r = el.getBoundingClientRect();
                    const st = getComputedStyle(el);
                    if (r.left < -1000 || r.top < -1000)
                        continue;
                    if (st.position === 'absolute'
                        && parseFloat(st.left) < -500)
                        continue;
                    if (el.getAttribute('tabindex') === '-1'
                        && (el.name || '').match(
                            /website|url|fax|address2|honeypot|hp_/i))
                        continue;
                    const hsig = (
                        (el.name||'') + ' '
                        + (el.id||'') + ' '
                        + (el.className||'')
                    ).toLowerCase();
                    if (/honey|hp_|trap|gotcha|fakefield/
                        .test(hsig))
                        continue;
                } catch(e) {}
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
                    || tp==='datetime-local'
                    || /дата|date/.test(nm+' '+ph))
                    role = 'date';
                else if (tp==='number')
                    role = 'number';
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

    if honeypot_set:
        empties = [
            f for f in (empties or [])
            if f.get("sel") not in honeypot_set
        ]

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
            elif role == "number":
                ok = await _fill_number_default(
                    page, sel,
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

    try:
        radio_fixed = await _select_empty_radio_groups(
            page, form_el, honeypot_set,
        )
        fixed += radio_fixed
    except Exception:
        pass

    if log and fixed:
        log.ok(
            f"дозаполнено {fixed} пустых полей (все)"
        )
    return fixed

async def _fill_number_default(page, sel):
    el = await find_el(page, sel)
    if not el:
        return False
    try:
        return bool(await page.evaluate(r"""el => {
            if (!el || el.tagName !== 'INPUT')
                return false;
            if ((el.value || '').trim()) return false;
            let v = 2;
            const mn = parseFloat(el.min);
            if (!isNaN(mn)) v = mn > 0 ? mn : (mn === 0 ? 1 : v);
            const mx = parseFloat(el.max);
            if (!isNaN(mx) && v > mx) v = mx;
            const proto = HTMLInputElement.prototype;
            const desc = Object.getOwnPropertyDescriptor(
                proto, 'value');
            const val = String(v);
            if (desc && desc.set) desc.set.call(el, val);
            else el.value = val;
            el.dispatchEvent(
                new Event('input', {bubbles: true}));
            el.dispatchEvent(
                new Event('change', {bubbles: true}));
            return true;
        }""", el))
    except Exception:
        return False

async def _select_empty_radio_groups(
    page, form_el=None, honeypots=None,
):
    honeypot_list = list(honeypots or [])
    try:
        return int(await page.evaluate(r"""(args) => {
            const scope = args.form || document;
            const skip = new Set(args.honeypots || []);
            const isShown = (el) => {
                try {
                    const st = getComputedStyle(el);
                    if (st.display === 'none'
                        || st.visibility === 'hidden')
                        return false;
                    const r = el.getBoundingClientRect();
                    if (r.left < -1000 || r.top < -1000)
                        return false;
                    return true;
                } catch(e) { return false; }
            };
            const selOf = (el) => {
                if (el.id) return '#' + el.id;
                if (el.name) return 'input[name="'
                    + el.name + '"]';
                return null;
            };
            const groups = {};
            const radios = scope.querySelectorAll(
                'input[type="radio"]');
            for (const el of radios) {
                const nm = el.name;
                if (!nm) continue;
                (groups[nm] = groups[nm] || []).push(el);
            }
            let n = 0;
            for (const nm in groups) {
                const list = groups[nm];
                if (list.some(r => r.checked)) continue;
                let chosen = null;
                for (const r of list) {
                    if (r.disabled) continue;
                    if (!isShown(r)) continue;
                    const s = selOf(r);
                    if (s && skip.has(s)) continue;
                    chosen = r;
                    break;
                }
                if (!chosen) continue;
                chosen.checked = true;
                chosen.dispatchEvent(
                    new Event('input', {bubbles: true}));
                chosen.dispatchEvent(
                    new Event('change', {bubbles: true}));
                n++;
            }
            return n;
        }""", {
            "form": form_el,
            "honeypots": honeypot_list,
        }))
    except Exception:
        return 0

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

_ANALYTICS_HOSTS = (
    "google-analytics", "googletagmanager",
    "mc.yandex", "yandex.ru/watch", "mixpanel",
    "facebook.com/tr", "doubleclick", "/collect",
    "hotjar", "criteo", "vk.com/rtrg",
    "top-fwz1.mail.ru", "/analytics", "/gtm",
)

def _norm_host(url):
    try:
        h = urlparse(url).netloc.lower()
        if h.startswith("www."):
            h = h[4:]
        return h
    except Exception:
        return ""

def _make_submit_predicate(origin_host):
    def pred(resp):
        try:
            req = resp.request
            if req.method not in (
                "POST", "PUT", "PATCH",
            ):
                return False
            url = resp.url
            host = _norm_host(url)
            if origin_host and host:
                same = (
                    host == origin_host
                    or host.endswith("." + origin_host)
                    or origin_host.endswith("." + host)
                )
                if not same:
                    return False
            low = url.lower()
            if any(a in low for a in _ANALYTICS_HOSTS):
                return False
            return True
        except Exception:
            return False
    return pred

async def _click_form_submit(page, form_el):
    if not form_el:
        return None
    prio = [
        'button[type="submit"]',
        'input[type="submit"]',
        '.sbut',
        '.t-submit',
        '[class*="submit" i]',
        '[class*="feedback__btn" i]',
        '[class*="btn-submit" i]',
        '[class*="form__submit" i]',
        '[class*="form-submit" i]',
        'button:not([type="button"]):not([type="reset"])',
        'button',
    ]
    for sel in prio:
        try:
            btn = await form_el.query_selector(sel)
            if btn and await btn.is_visible():
                await smart_click(
                    page, btn, aggressive=True,
                )
                return sel
        except Exception:
            continue

    try:
        clicked = await page.evaluate(
            r"""(form) => {
                if (!form) return null;
                const KEYS = [
                    'отправ','запис','заказ','получ',
                    'запрос','связат','перезвон',
                    'заплан','оставить','запросить',
                    'submit','send',
                ];
                const parents = [form];
                let cur = form.parentElement;
                for (let i=0; i<5 && cur; i++) {
                    parents.push(cur);
                    cur = cur.parentElement;
                }
                for (const scope of parents) {
                    const btns = scope.querySelectorAll(
                        'button, a.btn, div.btn, '
                        + 'span.btn, [role="button"], '
                        + '[class*="btn" i]'
                    );
                    for (const b of btns) {
                        const st = getComputedStyle(b);
                        if (st.display === 'none'
                            || st.visibility === 'hidden'
                            || st.opacity === '0') continue;
                        const r = b.getBoundingClientRect();
                        if (r.width < 30 || r.height < 15)
                            continue;
                        const t = (
                            b.innerText || b.value || ''
                        ).trim().toLowerCase();
                        if (t.length > 60) continue;
                        if (b.type === 'reset'
                            || b.type === 'button') {
                            const cls = (
                                b.className || ''
                            ).toString().toLowerCase();
                            if (!/submit|btn-submit|feedback__btn/.test(cls))
                                continue;
                        }
                        if (KEYS.some(k => t.includes(k))) {
                            try { b.click(); }
                            catch(e) {}
                            try {
                                b.dispatchEvent(
                                    new MouseEvent('click',
                                    {bubbles:true,
                                     cancelable:true,
                                     view:window})
                                );
                            } catch(e) {}
                            return (b.className || '')
                                .toString()
                                .split(' ').filter(Boolean)
                                .slice(0,2).join('.')
                                || b.tagName.toLowerCase();
                        }
                    }
                }
                return null;
            }""",
            form_el,
        )
        if clicked:
            return f"text_scan:{clicked}"
    except Exception:
        pass
    return None

async def _escalate_submit(page, form_el, pred):
    log = get_logger()
    resp_task = None
    try:
        resp_task = asyncio.ensure_future(
            page.wait_for_response(pred, timeout=20000)
        )
        await asyncio.sleep(0)
    except Exception:
        resp_task = None

    used = []

    clicked_sel = await _click_form_submit(page, form_el)
    if clicked_sel:
        used.append(f"btn:{clicked_sel}")
    await asyncio.sleep(0.8)

    done = resp_task is not None and resp_task.done()
    if not done and form_el:
        try:
            await page.evaluate(
                r"f => { try { f.requestSubmit"
                r" && f.requestSubmit(); }"
                r" catch(e) {} }",
                form_el,
            )
            used.append("requestSubmit")
        except Exception:
            pass
        await asyncio.sleep(0.8)
        done = resp_task is not None and resp_task.done()

    if not done and form_el:
        try:
            await page.evaluate(
                r"f => { try { f.dispatchEvent("
                r"new Event('submit', {bubbles:true,"
                r"cancelable:true})); } catch(e) {} }",
                form_el,
            )
            used.append("dispatch_submit")
        except Exception:
            pass
        await asyncio.sleep(0.8)
        done = resp_task is not None and resp_task.done()

    if not done:
        for s in PHONE_FALLBACKS:
            try:
                scope = form_el or page
                ph_el = await scope.query_selector(s)
                if ph_el and await ph_el.is_visible():
                    await ph_el.press("Enter")
                    used.append("enter_phone")
                    break
            except Exception:
                continue
        await asyncio.sleep(0.5)

    resp = None
    if resp_task is not None:
        if resp_task.done():
            try:
                resp = resp_task.result()
            except Exception:
                resp = None
        else:
            try:
                resp = await asyncio.wait_for(
                    asyncio.shield(resp_task),
                    timeout=1.5,
                )
            except Exception:
                resp = None
            if not resp_task.done():
                resp_task.cancel()

    if log and used:
        log.step(
            "escalate_submit",
            "методы: " + ", ".join(used)
            + (
                " | форменный POST пойман"
                if resp else " | POST не пойман"
            ),
        )
    return resp

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
        PlaywrightNetworkListener,
    )
    log = get_logger()
    shot_page = page_for_shot or page

    net_listener = PlaywrightNetworkListener(phone)
    net_listener.start(page)
    await setup_xhr_listener(page)

    pre_text = await capture_pre_submit_text(
        page, form_el,
    )
    pre_url = page.url
    prev_err_match = None
    origin_host = _norm_host(pre_url)
    submit_pred = _make_submit_predicate(origin_host)

    for attempt in range(1, max_submits + 1):
        if log:
            log.step(
                "submit",
                f"попытка {attempt}/{max_submits}",
            )
        net_listener.clear()
        await setup_xhr_listener(page)

        resp_task = None
        try:
            resp_task = asyncio.ensure_future(
                page.wait_for_response(
                    submit_pred, timeout=12000,
                )
            )
            await asyncio.sleep(0)
        except Exception:
            resp_task = None

        submitted = await do_submit(
            page, submit_sel, form_el,
        )
        if not submitted:
            if resp_task is not None \
                    and not resp_task.done():
                resp_task.cancel()
            return {"state": "submit_failed"}

        form_post_resp = None
        if resp_task is not None:
            if resp_task.done():
                try:
                    form_post_resp = resp_task.result()
                except Exception:
                    form_post_resp = None
            else:
                try:
                    form_post_resp = await asyncio.wait_for(
                        asyncio.shield(resp_task),
                        timeout=1.5,
                    )
                except Exception:
                    form_post_resp = None
                if not resp_task.done():
                    resp_task.cancel()
        post_status = None
        if form_post_resp is not None:
            try:
                post_status = form_post_resp.status
            except Exception:
                post_status = None

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
            net_listener=net_listener,
        )
        state = dom.get("state", "unchanged")

        if state in ("unchanged", "likely_failed"):
            poll_deadline = time.monotonic() + 25
            while time.monotonic() < poll_deadline:
                await asyncio.sleep(0.7)
                post_url2 = page.url
                url_changed2 = (
                    pre_url.rstrip("/")
                    != post_url2.rstrip("/")
                )
                dom2 = await detect_submission_result(
                    page, form_el, pre_text,
                    url_changed=url_changed2,
                    net_listener=net_listener,
                )
                s2 = dom2.get("state", "unchanged")
                if s2 not in (
                    "unchanged", "likely_failed",
                ):
                    dom = dom2
                    state = s2
                    break

        net_post_ok = (
            post_status is not None
            and 200 <= post_status < 400
        )
        if net_post_ok and state in (
            "unchanged", "likely_failed",
        ):
            if log:
                log.ok(
                    f"форменный POST {post_status} "
                    f"пойман → likely_success"
                )
            state = "likely_success"
            dom["state"] = "likely_success"
            dom.setdefault(
                "match", f"form POST {post_status}",
            )

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

        if (
            form_post_resp is None
            and state == "unchanged"
        ):
            esc_resp = await _escalate_submit(
                page, form_el, submit_pred,
            )
            if esc_resp is not None:
                await asyncio.sleep(2.0)
                try:
                    dom_e = (
                        await detect_submission_result(
                            page, form_el, pre_text,
                            url_changed=(
                                pre_url.rstrip("/")
                                != page.url.rstrip("/")
                            ),
                            net_listener=net_listener,
                        )
                    )
                except Exception:
                    dom_e = {"state": "unchanged"}
                se = dom_e.get("state", "unchanged")
                try:
                    est = esc_resp.status
                except Exception:
                    est = None
                if se in ("success", "likely_success") or (
                    est is not None
                    and 200 <= est < 400
                    and se in ("unchanged", "likely_failed")
                ):
                    dom_e["state"] = "success"
                    dom_e.setdefault(
                        "match",
                        f"escalated POST {est}",
                    )
                    return dom_e
                if se not in (
                    "unchanged", "likely_failed",
                ):
                    dom = dom_e
                    state = se

            late_deadline = time.monotonic() + 15
            while time.monotonic() < late_deadline:
                await asyncio.sleep(0.9)
                try:
                    dom_late = (
                        await detect_submission_result(
                            page, form_el, pre_text,
                            url_changed=(
                                pre_url.rstrip("/")
                                != page.url.rstrip("/")
                            ),
                            net_listener=net_listener,
                        )
                    )
                except Exception:
                    continue
                s_late = dom_late.get("state", "unchanged")
                if s_late in (
                    "success", "likely_success",
                ):
                    dom_late["state"] = "success"
                    if log:
                        log.ok(
                            "late POST пойман → success"
                        )
                    return dom_late
                if s_late not in (
                    "unchanged", "likely_failed",
                ):
                    dom = dom_late
                    state = s_late
                    break

        cur_match_for_cap = (
            dom.get("match", "") or ""
        ).strip()
        skip_captcha_loop = (
            state == "error"
            and prev_err_match is not None
            and cur_match_for_cap == prev_err_match
        )
        if state in (
            "unchanged", "error",
            "captcha_required",
        ) and not skip_captcha_loop:
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
                net_listener.clear()
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
                            net_listener=net_listener,
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
                                net_listener=(
                                    net_listener
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
            cur_match = (dom.get("match", "") or "").strip()
            if (
                prev_err_match is not None
                and cur_match == prev_err_match
            ):
                if log:
                    log.warn(
                        f"submit #{attempt}: та же ошибка "
                        f"({cur_match[:60]!r}), "
                        f"повтор бесполезен"
                    )
                return dom
            prev_err_match = cur_match
            if log:
                log.warn(
                    f"submit #{attempt}: {state} "
                    f"({cur_match}), "
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
