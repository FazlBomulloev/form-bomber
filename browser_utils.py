import asyncio
from config import COOKIE_BTN_TEXTS


async def step_shot(
    page, name, step_dir, form_el=None,
):
    if step_dir is None:
        return
    try:
        from logger import get_logger
        path_str = str(step_dir / f"{name}.jpg")

        if "after_submit" in name and form_el:
            try:
                if await form_el.is_visible():
                    await form_el.scroll_into_view_if_needed()
                else:
                    for sel in [
                        '.success', '[class*="thank" i]',
                        '.alert', '[class*="success" i]',
                    ]:
                        el = await page.query_selector(
                            sel
                        )
                        if el and await el.is_visible():
                            await el.scroll_into_view_if_needed()
                            break
            except Exception:
                pass

        try:
            await page.screenshot(
                path=path_str, type="jpeg",
                quality=75,
                full_page=False, timeout=6000,
            )
        except Exception:
            await page.screenshot(
                path=path_str, type="jpeg",
                quality=60,
                full_page=True, timeout=8000,
            )

        if log := get_logger():
            log.log_shot(name, path_str)
    except Exception:
        pass


async def dismiss_cookie_banners(
    page, cookie_selector=None
):
    if cookie_selector:
        try:
            el = await page.query_selector(
                cookie_selector
            )
            if el and await el.is_visible():
                await el.click()
                await asyncio.sleep(0.8)
                return
        except Exception:
            pass

    for sel in [
        '[id*="cookie" i] button',
        '[class*="cookie" i] button',
        '[id*="consent" i] button',
        '[class*="consent" i] button',
        '[id*="gdpr" i] button',
        'button[id*="cookie-accept" i]',
        '[data-cookiefirst-action="accept"]',
    ]:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.click()
                await asyncio.sleep(0.8)
                return
        except Exception:
            continue

    for container in await page.query_selector_all(
        '[id*="cookie" i],[class*="cookie" i],'
        '[id*="consent" i],[class*="consent" i],'
        '[id*="gdpr" i],[class*="gdpr" i]'
    ):
        try:
            if not await container.is_visible():
                continue
            for btn in await container.query_selector_all(
                'button, a, [role="button"]'
            ):
                if not await btn.is_visible():
                    continue
                text = (
                    await btn.inner_text()
                ).strip().lower()
                if text and any(
                    text == t or text.startswith(t)
                    for t in COOKIE_BTN_TEXTS
                ):
                    await btn.click()
                    await asyncio.sleep(0.8)
                    return
        except Exception:
            continue


async def suppress_widgets(page):
    try:
        await page.evaluate(r"""() => {
            const sels = [
                '[class*="chat" i]','[id*="chat" i]',
                '[class*="widget" i]',
                '[class*="whatsapp" i]',
                '[class*="jivo" i]','[id*="jivo" i]',
                '[class*="crisp" i]',
                '[class*="calltouch" i]',
                '[class*="envybox" i]',
                '[class*="callbackhunter" i]',
                '[class*="comagic" i]',
                '[class*="mango" i]',
                '[class*="roistat" i]',
                '[class*="callibri" i]',
                '[class*="chatra" i]',
            ];
            const isForm = (el) =>
                !!el.closest('form');
            for (const s of sels) {
                for (const n of
                    document.querySelectorAll(s)) {
                    if (!n || isForm(n)) continue;
                    const st = getComputedStyle(n);
                    if (st.position !== 'fixed'
                        && st.position !== 'sticky')
                        continue;
                    const r = n.getBoundingClientRect();
                    if (r.width < 24 || r.height < 24)
                        continue;
                    if (r.top > innerHeight * 0.55
                        || r.left > innerWidth * 0.55) {
                        n.style.setProperty(
                            'display','none','important'
                        );
                        n.style.setProperty(
                            'pointer-events',
                            'none','important'
                        );
                    }
                }
            }
        }""")
    except Exception:
        pass


async def clear_overlays(page):
    try:
        await page.evaluate(r"""() => {
            const cands = [
                '[class*="overlay" i]',
                '[class*="modal" i]',
                '[class*="popup" i]',
                '[class*="backdrop" i]',
                '[role="dialog"]',
            ];
            const isBig = (el) => {
                const r = el.getBoundingClientRect();
                return r.width >= innerWidth * 0.28
                    && r.height >= innerHeight * 0.18;
            };
            const isBlocking = (el) => {
                const st = getComputedStyle(el);
                if (st.display === 'none'
                    || st.visibility === 'hidden'
                    || st.opacity === '0') return false;
                return st.position === 'fixed'
                    || st.position === 'sticky'
                    || parseInt(st.zIndex||0) > 999;
            };
            for (const sel of cands) {
                for (const el of
                    document.querySelectorAll(sel)) {
                    if (!el || !isBlocking(el)
                        || !isBig(el)) continue;
                    if (el.querySelector(
                        'input[type="tel"],'
                        + 'input[name*="phone" i],'
                        + 'form'
                    )) continue;
                    el.style.setProperty(
                        'display','none','important'
                    );
                }
            }
        }""")
    except Exception:
        pass


async def dismiss_popups(page, form_el=None):
    try:
        closed = await page.evaluate(r"""formEl => {
            let n = 0;
            const isOurs = (el) => {
                if (!formEl) return false;
                return el === formEl
                    || el.contains(formEl)
                    || formEl.contains(el);
            };
            const isShown = (el) => {
                try {
                    const st = getComputedStyle(el);
                    if (st.display === 'none'
                        || st.visibility === 'hidden'
                        || st.opacity === '0')
                        return false;
                    const r =
                        el.getBoundingClientRect();
                    return r.width > 30
                        && r.height > 30;
                } catch(e) { return false; }
            };
            const acceptTexts = new Set([
                'принять','accept','ok','ок',
                'понятно','согласен','agree','allow',
            ]);
            // Cookie баннеры
            for (const sel of [
                '[class*="cookie" i]',
                '[id*="cookie" i]',
                '[class*="consent" i]',
            ]) {
                for (const box of
                    document.querySelectorAll(sel)) {
                    if (!isShown(box) || isOurs(box))
                        continue;
                    for (const btn of
                        box.querySelectorAll(
                            'button, a, [role="button"]'
                        )) {
                        if (!isShown(btn)) continue;
                        const t = (btn.innerText||'')
                            .trim().toLowerCase();
                        if (acceptTexts.has(t)) {
                            try{btn.click();n++;}
                            catch(e){}
                            break;
                        }
                    }
                }
            }
            // Промо-попапы
            for (const sel of [
                '[class*="popup" i]:not(nav)',
                '[class*="modal" i]:not(nav)',
                '[role="dialog"]',
            ]) {
                for (const box of
                    document.querySelectorAll(sel)) {
                    if (!isShown(box) || isOurs(box))
                        continue;
                    const st = getComputedStyle(box);
                    if (st.position !== 'fixed'
                        && st.position !== 'sticky'
                        && parseInt(st.zIndex||0)<=999)
                        continue;
                    const closeChars = new Set(
                        ['×','✕','✗','✖','X','x']
                    );
                    let closed_it = false;
                    for (const btn of
                        box.querySelectorAll(
                            'button, a, span, div'
                        )) {
                        if (!isShown(btn)) continue;
                        const t = (btn.innerText||'')
                            .trim();
                        const cls = (
                            (btn.className||'') + ' '
                            + (btn.getAttribute(
                                'aria-label'
                            )||'')
                        ).toLowerCase();
                        if (closeChars.has(t)
                            || cls.includes('close')
                            || acceptTexts.has(
                                t.toLowerCase()
                            )) {
                            try{btn.click();n++;}
                            catch(e){}
                            closed_it = true;
                            break;
                        }
                    }
                    if (!closed_it) {
                        box.style.setProperty(
                            'display','none','important'
                        );
                        n++;
                    }
                }
            }
            return n;
        }""", form_el)
        if closed:
            await asyncio.sleep(0.6)
    except Exception:
        pass


async def smart_click(
    page, el, aggressive=False, timeout_ms=4000
):
    try:
        await el.scroll_into_view_if_needed()
    except Exception:
        pass
    try:
        await el.click(timeout=timeout_ms)
        return True
    except Exception:
        pass
    if aggressive:
        await suppress_widgets(page)
        await clear_overlays(page)
        await dismiss_popups(page)
    try:
        await el.click(
            timeout=max(1200, timeout_ms // 2),
            force=True,
        )
        return True
    except Exception:
        pass
    try:
        await page.evaluate(
            "el => { try{el.click();}catch(e){} }", el
        )
        return True
    except Exception:
        return False


async def find_el(page, raw_sel, timeout=1500):
    for sel in [
        s.strip()
        for s in raw_sel.split(',') if s.strip()
    ]:
        try:
            if (sel.startswith('#')
                    and ' ' not in sel
                    and ':' not in sel):
                attr_sel = f'[id="{sel[1:]}"]'
                for el in (
                    await page.query_selector_all(
                        attr_sel
                    )
                ):
                    try:
                        if await el.is_visible():
                            return el
                    except Exception:
                        continue
            el = await page.wait_for_selector(
                sel, timeout=timeout,
                state="visible",
            )
            if el:
                return el
        except Exception:
            pass
        try:
            el = await page.wait_for_selector(
                sel, timeout=500,
                state="attached",
            )
            if el:
                try:
                    await page.evaluate(
                        r"""el => {
                        let n = el;
                        for (let i=0;i<10&&n;i++){
                            const st =
                                getComputedStyle(n);
                            if(st.display==='none')
                                n.style.setProperty(
                                    'display','block',
                                    'important');
                            if(st.visibility
                                ==='hidden')
                                n.style.setProperty(
                                    'visibility',
                                    'visible',
                                    'important');
                            if(parseFloat(
                                st.opacity)<0.1)
                                n.style.setProperty(
                                    'opacity','1',
                                    'important');
                            n = n.parentElement;
                        }
                    }""", el,
                    )
                except Exception:
                    pass
                return el
        except Exception:
            continue
    return None


async def scroll_page_for_lazy(page):
    try:
        for _ in range(12):
            await page.evaluate(
                "() => window.scrollBy(0, "
                "Math.min(520, "
                "window.innerHeight * 0.92))"
            )
            await asyncio.sleep(0.12)
        await page.evaluate(
            "() => window.scrollTo(0, "
            "Math.max("
            "document.body?"
            "document.body.scrollHeight:0,"
            "document.documentElement?"
            "document.documentElement"
            ".scrollHeight:0))"
        )
        await asyncio.sleep(0.85)
    except Exception:
        pass


async def react_patch_input(page, el, value):
    try:
        await page.evaluate(
            r"""([el, val]) => {
                try {
                    const isTa =
                        el.tagName === 'TEXTAREA';
                    const proto = isTa
                        ? HTMLTextAreaElement.prototype
                        : HTMLInputElement.prototype;
                    const desc =
                        Object.getOwnPropertyDescriptor(
                            proto, 'value'
                        );
                    if (desc && desc.set)
                        desc.set.call(el, val);
                    else el.value = val;
                } catch (e) { el.value = val; }
                el.dispatchEvent(new Event(
                    'input', { bubbles: true }
                ));
                el.dispatchEvent(new Event(
                    'change', { bubbles: true }
                ));
                el.dispatchEvent(new Event(
                    'blur', { bubbles: true }
                ));
            }""",
            [el, value],
        )
    except Exception:
        pass