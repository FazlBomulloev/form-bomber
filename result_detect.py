from config import SUCCESS_TEXTS, ERROR_PHRASES


async def _fallback_detect(page, pre_text, url_changed):
    """Fallback-детекция когда основной evaluate упал
    (например, страница перешла и form_el стал stale)."""
    try:
        text = await page.evaluate(
            "() => (document.body.innerText || '')"
            ".toLowerCase()"
        )
    except Exception:
        if url_changed:
            return {
                "state": "likely_success",
                "match": "page navigated (context lost)",
            }
        return {"state": "unchanged", "match": ""}

    for phrase in SUCCESS_TEXTS:
        if phrase in text and (
            not pre_text or phrase not in pre_text
        ):
            return {"state": "success", "match": phrase}

    for phrase in ERROR_PHRASES:
        if phrase in text and (
            not pre_text or phrase not in pre_text
        ):
            return {"state": "error", "match": phrase}

    if url_changed:
        url = ""
        try:
            url = page.url.lower()
        except Exception:
            pass
        if any(
            w in url for w in (
                "thank", "success", "спасибо",
                "заявка", "blagodar",
            )
        ):
            return {
                "state": "likely_success",
                "match": "redirect to success URL",
            }
        return {
            "state": "unchanged",
            "match": "page navigated",
        }

    return {"state": "unchanged", "match": ""}


async def setup_xhr_listener(page):
    """Перехватывает fetch/XHR чтобы отследить
    ответы сервера после submit."""
    try:
        await page.evaluate(r"""() => {
            window.__fbXHR = [];

            // Patch fetch
            const _f = window.fetch;
            window.fetch = async function(...a) {
                const r = await _f.apply(this, a);
                try {
                    const c = r.clone();
                    const t = await c.text();
                    window.__fbXHR.push({
                        url: (a[0]?.url || a[0]
                            || '').toString()
                            .substring(0, 200),
                        s: r.status,
                        b: t.substring(0, 500),
                        tp: 'f',
                    });
                } catch(e) {}
                return r;
            };

            // Patch XMLHttpRequest
            const _o = XMLHttpRequest.prototype.open;
            const _s = XMLHttpRequest.prototype.send;
            XMLHttpRequest.prototype.open =
                function(m, u, ...r) {
                    this._u = (u||'').toString()
                        .substring(0, 200);
                    return _o.apply(this, [m, u, ...r]);
                };
            XMLHttpRequest.prototype.send =
                function(body) {
                    this.addEventListener('load',
                        function() {
                        try {
                            window.__fbXHR.push({
                                url: this._u || '',
                                s: this.status,
                                b: (this.responseText
                                    ||'').substring(
                                    0, 500),
                                tp: 'x',
                            });
                        } catch(e) {}
                    });
                    return _s.apply(this, [body]);
                };
        }""")
    except Exception:
        pass


async def check_xhr_result(page):
    """Проверяет перехваченные XHR/fetch на
    признаки успеха или ошибки."""
    try:
        return await page.evaluate(r"""() => {
            const rs = window.__fbXHR || [];
            if (!rs.length) return null;

            const okRe =
                /success|"ok"|"status":\s*"?(?:ok|true)|спасибо|thank|принят|отправлен|записан|получили|"result":\s*"?(?:ok|success)|mail_sent|sent_ok|"sent":\s*true|"message_sent"|благодар/i;
            const errRe =
                /error|"status":\s*"?(?:fail|error)|ошибка|invalid|captcha|validation/i;

            for (const r of rs) {
                const b = r.b || '';
                if (!b || b.length < 3) continue;
                // Пропуск аналитики/метрик
                const u = (r.url||'').toLowerCase();
                if (/metric|analytic|yandex|google|pixel|beacon|log|stat/
                    .test(u)) continue;

                // Tilda needcaptcha → специальный статус
                if (/needcaptcha/.test(b)) {
                    return {
                        state: 'captcha_required',
                        match: 'XHR: needcaptcha',
                    };
                }

                if (r.s >= 200 && r.s < 300
                    && okRe.test(b)) {
                    return {
                        state: 'success',
                        match: 'XHR: '
                            + b.substring(0, 60),
                    };
                }
                if ((r.s >= 400 || errRe.test(b))
                    && !okRe.test(b)) {
                    return {
                        state: 'error',
                        match: 'XHR err: '
                            + b.substring(0, 60),
                    };
                }
            }

            // Успешный POST без тела ответа
            for (const r of rs) {
                const u = (r.url||'').toLowerCase();
                if (/metric|analytic|yandex|google|pixel/
                    .test(u)) continue;
                if (r.s >= 200 && r.s < 300
                    && r.tp !== 'f') {
                    return null;
                }
            }
            return null;
        }""")
    except Exception:
        return None


async def capture_pre_submit_text(page, form_el=None):
    try:
        return await page.evaluate(
            r"""() => {
            return (document.body.innerText || '')
                .toLowerCase();
        }""")
    except Exception:
        return ""


async def detect_submission_result(
    page, form_el=None, pre_text="",
    url_changed=False,
):
    safe_form_el = None if url_changed else form_el
    try:
        dom_result = await page.evaluate(r"""(args) => {
            const formEl = args.formEl;
            const preText = args.preText || '';
            const successPhrases = args.successPhrases;
            const errorPhrases = args.errorPhrases;
            const urlChanged = args.urlChanged || false;

            function getFormScope(fe) {
                if (!fe) return null;
                let n = fe;
                for (let i = 0; i < 3 && n.parentElement; i++)
                    n = n.parentElement;
                return n;
            }

            function isNew(phrase, pre) {
                return !pre || !pre.includes(phrase);
            }

            function isVis(el) {
                try {
                    const st = getComputedStyle(el);
                    return st.display !== 'none'
                        && st.visibility !== 'hidden'
                        && st.opacity !== '0';
                } catch(e) { return false; }
            }

            // --- Проверка: форма исчезла ---
            let formGone = false;
            if (formEl) {
                try {
                    if (!document.contains(formEl)) {
                        formGone = true;
                    } else {
                        const st = getComputedStyle(formEl);
                        if (st.display === 'none'
                            || st.visibility === 'hidden'
                            || st.opacity === '0')
                            formGone = true;
                    }
                } catch(e) { formGone = true; }
            }

            // --- Scope: сначала область формы, потом body ---
            const formScope = getFormScope(formEl);
            const scopes = formScope
                ? [formScope, document.body]
                : [document.body];

            for (let si = 0; si < scopes.length; si++) {
                const scope = scopes[si];
                const isFallback = si > 0;
                const text = (scope.innerText || '').toLowerCase();

                // Приоритет 1: ERROR-фразы (только НОВЫЕ)
                for (const p of errorPhrases) {
                    if (text.includes(p) && isNew(p, preText)) {
                        return {state: 'error', match: p};
                    }
                }

                // Error-элементы в scope формы
                if (!isFallback && formEl) {
                    const errSels = '.form-error, '
                        + '.field-error, '
                        + '.is-invalid, '
                        + '[aria-invalid="true"]';
                    let visErrors = 0;
                    for (const el of formEl.querySelectorAll(errSels)) {
                        try {
                            if (isVis(el)) visErrors++;
                        } catch(e) {}
                    }
                    if (visErrors > 0)
                        return {
                            state: 'validation_error',
                            match: visErrors + ' error elements',
                        };
                }

                // Приоритет 2: SUCCESS-фразы
                // При навигации на другую страницу не доверяем
                // обычному текстовому поиску в body —
                // только success-элементам (ниже)
                if (!(urlChanged && isFallback)) {
                    for (const p of successPhrases) {
                        if (text.includes(p)) {
                            if (!isNew(p, preText)) continue;
                            return {state: 'success', match: p};
                        }
                    }
                }

                // Новые success-элементы
                const successSels = '.success, .alert-success, '
                    + '.form-success, '
                    + '[class*="success" i], '
                    + '[class*="thank" i], '
                    + '.wpcf7-mail-sent-ok, '
                    + '.wpcf7-response-output.wpcf7-mail-sent-ok, '
                    + '[class*="wpcf7-mail-sent" i], '
                    + '.toast, .snackbar, .notification, '
                    + '[class*="toast" i], '
                    + '[class*="snackbar" i], '
                    + '[class*="popup-thank" i], '
                    + '[class*="modal-thank" i], '
                    + '.t-form__successbox';
                for (const el of scope.querySelectorAll(successSels)) {
                    if (!isVis(el)) continue;
                    const t = (el.innerText || '').trim();
                    if (t.length > 3 && t.length < 300) {
                        const tl = t.toLowerCase();
                        if (isNew(tl, preText))
                            return {
                                state: 'success',
                                match: t.substring(0, 60),
                            };
                    }
                }

                // wpcf7: проверка по data-атрибуту
                const wpcf7 = scope.querySelector(
                    '.wpcf7-response-output');
                if (wpcf7 && isVis(wpcf7)) {
                    const wt = (wpcf7.innerText || '').trim();
                    if (wt.length > 3) {
                        const wtl = wt.toLowerCase();
                        const isErr = /ошибк|error|invalid|обязатель|заполн/
                            .test(wtl);
                        if (!isErr && isNew(wtl, preText))
                            return {
                                state: 'success',
                                match: 'wpcf7: ' + wt.substring(0, 50),
                            };
                    }
                }

                // Tilda success box
                const tSucc = scope.querySelector(
                    '.t-form__successbox, [class*="t-form__success" i]');
                if (tSucc && isVis(tSucc)) {
                    const tt = (tSucc.innerText || '').trim();
                    if (tt.length > 2)
                        return {
                            state: 'success',
                            match: 'tilda: ' + tt.substring(0, 50),
                        };
                }

                if (!isFallback) continue;

                // Fallback body: error-элементы (строже)
                const errSelsFb = '.form-error, .field-error, '
                    + '.is-invalid, [aria-invalid="true"]';
                let visErrFb = 0;
                for (const el of scope.querySelectorAll(errSelsFb)) {
                    try {
                        if (isVis(el)) visErrFb++;
                    } catch(e) {}
                }
                if (visErrFb > 0)
                    return {
                        state: 'validation_error',
                        match: visErrFb + ' error elements (body)',
                    };
            }

            // --- Форма исчезла ---
            if (formGone) {
                if (urlChanged) {
                    const url = location.href.toLowerCase();
                    if (/thank|success|спасибо|заявка|blagodar/.test(url))
                        return {state: 'likely_success',
                            match: 'redirect to success URL'};
                    // Ищем success в заголовках новой стр.
                    const hh = document.querySelectorAll(
                        'h1,h2,h3,h4,.title,[class*="title" i]');
                    for (const h of hh) {
                        if (!isVis(h)) continue;
                        const ht = (h.innerText||'')
                            .toLowerCase();
                        for (const p of successPhrases) {
                            if (ht.includes(p))
                                return {
                                    state: 'likely_success',
                                    match: 'heading: '
                                        + ht.substring(0,60),
                                };
                        }
                    }
                    return {state: 'unchanged',
                        match: 'page navigated'};
                }
                return {state: 'likely_success',
                    match: 'form disappeared'};
            }

            // --- Форма на месте, поля пустые ---
            if (formEl && !formGone) {
                try {
                    const inputs = formEl.querySelectorAll(
                        'input:not([type="hidden"])'
                        + ':not([type="submit"])'
                        + ':not([type="checkbox"])'
                        + ':not([type="radio"]),'
                        + 'textarea'
                    );
                    let emptyCount = 0;
                    let totalVisible = 0;
                    for (const inp of inputs) {
                        if (!isVis(inp)) continue;
                        totalVisible++;
                        if (!(inp.value || '').trim()) emptyCount++;
                    }
                    if (totalVisible > 0 && emptyCount === totalVisible) {
                        const errSelsAll = '.error, .form-error, '
                            + '.field-error, .invalid, '
                            + '.is-invalid, '
                            + '[aria-invalid="true"], '
                            + ':invalid';
                        let hasVisErr = false;
                        for (const el of formEl.querySelectorAll(errSelsAll)) {
                            try {
                                if (isVis(el) && el.tagName !== 'FORM')
                                    { hasVisErr = true; break; }
                            } catch(e2) {}
                        }
                        if (hasVisErr)
                            return {state: 'likely_failed',
                                match: 'fields empty + errors'};
                        return {state: 'likely_success',
                            match: 'form reset (fields cleared)'};
                    }
                } catch(e) {}
            }

            return {state: 'unchanged', match: ''};
        }""", {
            "formEl": safe_form_el,
            "preText": pre_text,
            "successPhrases": SUCCESS_TEXTS,
            "errorPhrases": ERROR_PHRASES,
            "urlChanged": url_changed,
        })
    except Exception:
        dom_result = await _fallback_detect(
            page, pre_text, url_changed,
        )

    if dom_result.get("state") == "unchanged":
        xhr = await check_xhr_result(page)
        if xhr:
            return xhr

    return dom_result


async def detect_client_validation_error(page):
    try:
        return await page.evaluate(r"""() => {
            const invalid = document.querySelectorAll(
                ':invalid, .is-invalid, '
                + '[aria-invalid="true"], '
                + '.error:not(nav), '
                + '.field-error'
            );
            const msgs = [];
            for (const el of invalid) {
                try {
                    const st = getComputedStyle(el);
                    if (st.display === 'none'
                        || st.visibility === 'hidden')
                        continue;
                    const msg =
                        el.validationMessage
                        || el.title
                        || (el.innerText || '')
                            .trim()
                            .substring(0, 60);
                    if (msg) msgs.push(msg);
                } catch(e) {}
            }
            return {
                has_errors: msgs.length > 0,
                messages: msgs.slice(0, 5),
            };
        }""")
    except Exception:
        return {"has_errors": False, "messages": []}


async def get_invalid_field_hint(page):
    try:
        return await page.evaluate(r"""() => {
            const els = document.querySelectorAll(
                ':invalid, .is-invalid, '
                + '[aria-invalid="true"]'
            );
            const hints = [];
            for (const el of els) {
                try {
                    const st = getComputedStyle(el);
                    if (st.display === 'none')
                        continue;
                    const tag =
                        el.tagName.toLowerCase();
                    const type = (
                        el.type || ''
                    ).toLowerCase();
                    const name = el.name || '';
                    const ph = (
                        el.placeholder || ''
                    ).trim();
                    const msg =
                        el.validationMessage || '';
                    hints.push({
                        tag, type, name, ph, msg,
                    });
                } catch(e) {}
            }
            return hints.slice(0, 5);
        }""")
    except Exception:
        return []
