"""JS-код для извлечения структуры формы из DOM."""

from typing import Optional
from logger import get_logger

FORM_EXTRACTOR_JS = r"""() => {
    function isVisible(el) {
        if (!el) return false;
        const st = getComputedStyle(el);
        if (st.display === 'none'
            || st.visibility === 'hidden')
            return false;
        if (parseFloat(st.opacity || '1') < 0.05)
            return false;
        const r = el.getBoundingClientRect();
        return r.width > 6 && r.height > 6;
    }

    function buildSelector(el) {
        if (!el) return null;
        if (el.id) {
            try { return '#' + CSS.escape(el.id); }
            catch(e) { return '#' + el.id; }
        }
        const df = el.getAttribute('data-field');
        if (df)
            return el.tagName.toLowerCase()
                + '[data-field="' + df + '"]';
        const dn = el.getAttribute('data-name');
        if (dn)
            return el.tagName.toLowerCase()
                + '[data-name="' + dn + '"]';
        if (el.name) {
            const tag = el.tagName.toLowerCase();
            const sel = tag
                + '[name="' + el.name + '"]';
            const scope = el.closest('form')
                || el.closest('[role="dialog"]')
                || document;
            const matches =
                scope.querySelectorAll(sel);
            if (matches.length === 1) return sel;
            const tp = (el.type||'').toLowerCase();
            if (tp) {
                const sel2 = tag
                    + '[name="' + el.name
                    + '"][type="' + tp + '"]';
                if (scope.querySelectorAll(sel2)
                        .length === 1)
                    return sel2;
            }
        }
        const ph = (el.placeholder||'').trim();
        if (ph && ph.length < 40) {
            const sel = el.tagName.toLowerCase()
                + '[placeholder="' + ph + '"]';
            const scope = el.closest('form')
                || document;
            if (scope.querySelectorAll(sel).length <= 2)
                return sel;
        }
        const tp = (el.type||'').toLowerCase();
        const cls = (el.className||'').toString()
            .split(/\s+/).filter(Boolean)[0];
        if (tp && cls) {
            try {
                return el.tagName.toLowerCase()
                    + '[type="' + tp + '"].'
                    + CSS.escape(cls);
            } catch(e) {}
        }
        const ac = el.getAttribute('autocomplete');
        if (ac)
            return el.tagName.toLowerCase()
                + '[autocomplete="' + ac + '"]';
        const parent = el.closest('form')
            || el.parentElement;
        if (parent) {
            const tag = el.tagName.toLowerCase();
            const siblings = Array.from(
                parent.querySelectorAll(tag)
            );
            const idx = siblings.indexOf(el) + 1;
            if (idx > 0)
                return tag
                    + ':nth-of-type(' + idx + ')';
        }
        return null;
    }

    function getLabel(el) {
        if (el.id) {
            try {
                const lbl = document.querySelector(
                    'label[for="'
                    + CSS.escape(el.id) + '"]'
                );
                if (lbl)
                    return (lbl.innerText||'')
                        .trim().substring(0, 80);
            } catch(e) {}
        }
        const closest = el.closest('label');
        if (closest) {
            const t = (closest.innerText||'').trim();
            if (t.length < 80) return t;
        }
        const al = el.getAttribute('aria-label');
        if (al) return al.trim().substring(0, 80);
        return '';
    }

    function classifyField(el) {
        const tag = el.tagName.toLowerCase();
        const type = (el.type||'').toLowerCase();
        const name = (el.name||'').toLowerCase();
        const id = (el.id||'').toLowerCase();
        const ph = (el.placeholder||'').toLowerCase();
        const ac = (el.getAttribute('autocomplete')
            ||'').toLowerCase();
        const cls = (el.className||'').toString()
            .toLowerCase();
        const im = (el.inputMode||'').toLowerCase();
        const label = getLabel(el).toLowerCase();
        const df = (el.getAttribute('data-field')
            ||'').toLowerCase();
        const dn = (el.getAttribute('data-name')
            ||'').toLowerCase();
        const rule = (el.getAttribute(
            'data-tilda-rule')||'').toLowerCase();
        const sig = [
            name,id,ph,ac,cls,label,df,dn,rule
        ].join(' ');

        if (type==='tel' || im==='tel'
            || ac==='tel') return 'phone';
        if (/phone|tel[^a-z]|телефон|номер тел|mobile|моб|phonemask|tildaspec-phone/.test(sig))
            return 'phone';
        if (/\+7|\(\d{3}\)|___/.test(ph))
            return 'phone';
        if (rule === 'phone') return 'phone';

        if (type==='email' || ac==='email')
            return 'email';
        if (/e-?mail|почт|электронн/.test(sig))
            return 'email';

        if (tag === 'textarea') return 'comment';
        if (/comment|message|коммент|сообщ|вопрос/.test(sig))
            return 'comment';

        if (/patronymic|middle.?name|отчеств/.test(sig))
            return 'patronymic';
        if (/last.?name|surname|family.?name|фамили/.test(sig))
            return 'lastname';
        if (/first.?name|given.?name|^имя$/.test(sig.trim()))
            return 'firstname';
        if (rule === 'name') return 'name';
        if (/\bname\b|имя|фио|ваше имя/.test(sig))
            return 'name';
        if (ac==='name' || ac==='given-name')
            return 'name';

        if (type==='date' || /дата|date/.test(sig))
            return 'date';

        if (type === 'checkbox') {
            if (/policy|consent|agree|соглас|политик|персональн|обработк|конфиденц|privacy/.test(sig))
                return 'checkbox_consent';
            const allCb = (el.closest('form')
                || document).querySelectorAll(
                    'input[type="checkbox"]'
                );
            if (allCb.length === 1)
                return 'checkbox_consent';
            return 'checkbox_other';
        }

        if (type === 'radio') return 'radio';
        if (type==='text' || type==='' || !type)
            return 'text_unknown';
        return 'unknown';
    }

    function scoreForm(container, fields) {
        let score = 0;
        const hasPhone = fields.some(
            f => f.role === 'phone');
        const hasName = fields.some(
            f => ['name','firstname','lastname']
                .includes(f.role));
        const visibleFields = fields.filter(
            f => f.visible).length;
        const radios = fields.filter(
            f => f.role === 'radio').length;

        if (hasPhone) score += 30;
        if (hasPhone && hasName) score += 15;
        if (visibleFields >= 2
            && visibleFields <= 5)
            score += 10;
        if (radios > 4) score -= radios * 3;

        const html = (container.innerHTML||'')
            .toLowerCase();
        if (/заказать звонок|перезвон|callback/
            .test(html))
            score += 20;
        else if (/консультац/.test(html))
            score += 12;
        else if (/записаться|запись/.test(html))
            score += 8;
        if (/поиск|search|найти/.test(html))
            score -= 25;
        if (/подписаться|subscribe/.test(html))
            score -= 20;
        if (/отзыв|review/.test(html))
            score -= 30;

        const textareas = container.querySelectorAll(
            'textarea');
        for (const ta of textareas) {
            const ph = (ta.placeholder||'')
                .toLowerCase();
            if (/отзыв|текст отзыва|review/.test(ph))
                score -= 30;
        }

        const headings = container.querySelectorAll(
            'h1,h2,h3,h4,h5,h6');
        for (const hd of headings) {
            const ht = (hd.innerText||'')
                .toLowerCase();
            if (/отзыв|отзывы|reviews/.test(ht))
                score -= 25;
        }

        return score;
    }

    function findSubmit(container) {
        for (const sel of [
            'button[type="submit"]',
            'input[type="submit"]',
        ]) {
            const el = container.querySelector(sel);
            if (el && isVisible(el))
                return buildSelector(el);
        }
        const submitTexts = [
            'отправить','записаться',
            'оставить заявку','заказать звонок',
            'получить консультацию',
            'submit','send',
        ];
        for (const btn of container.querySelectorAll(
            'button, input[type="button"]'
        )) {
            if (!isVisible(btn)) continue;
            const t = (btn.innerText||btn.value||'')
                .toLowerCase().trim();
            if (submitTexts.some(st => t.includes(st)))
                return buildSelector(btn);
        }
        for (const btn of container.querySelectorAll(
            'button:not([type])'
        )) {
            if (isVisible(btn))
                return buildSelector(btn);
        }
        return null;
    }

    function extractContainer(container) {
        const fields = [];
        const allInputs = container.querySelectorAll(
            'input:not([type="hidden"])'
            + ':not([type="submit"])'
            + ':not([type="button"])'
            + ':not([type="reset"]),'
            + 'textarea, select'
        );
        for (const el of allInputs) {
            const type = (el.type||'').toLowerCase();
            const vis = isVisible(el)
                || type==='checkbox'
                || type==='radio';
            if (!vis && type!=='checkbox'
                && type!=='radio') continue;
            const role = classifyField(el);
            const selector = buildSelector(el);
            if (!selector) continue;
            const fld = {
                tag: el.tagName.toLowerCase(),
                type: type,
                name: el.name || '',
                id: el.id || '',
                placeholder: (
                    el.placeholder||''
                ).trim(),
                label: getLabel(el),
                role: role,
                visible: isVisible(el),
                required: el.required
                    || el.getAttribute(
                        'aria-required'
                    ) === 'true',
                selector: selector,
                priority: vis ? 0 : 1,
            };
            if (el.tagName === 'SELECT') {
                fld.options = Array.from(
                    el.options
                ).slice(0, 8).map(
                    o => ({
                        text: o.text.trim(),
                        value: o.value,
                    })
                );
                fld.role = 'dropdown';
            }
            fields.push(fld);
        }
        let formSelector = null;
        if (container.tagName === 'FORM') {
            if (container.id) {
                try {
                    formSelector = 'form#'
                        + CSS.escape(container.id);
                } catch(e) {
                    formSelector = 'form#'
                        + container.id;
                }
            } else if (container.action
                && container.action
                    !== window.location.href) {
                formSelector = 'form[action="'
                    + container.getAttribute('action')
                    + '"]';
            } else {
                const cls = (
                    container.className||''
                ).split(' ').filter(c => c)[0];
                if (cls) {
                    try {
                        formSelector = 'form.'
                            + CSS.escape(cls);
                    } catch(e) {
                        formSelector = 'form.' + cls;
                    }
                } else {
                    formSelector = 'form';
                }
            }
        }
        return {
            form_selector: formSelector,
            submit_selector: findSubmit(container),
            fields: fields,
            score: scoreForm(container, fields),
        };
    }

    function resolveUnknowns(fields) {
        let hasName = fields.some(
            f => ['name','firstname','lastname']
                .includes(f.role)
        );
        for (const f of fields) {
            if (f.role !== 'text_unknown') continue;
            if (!hasName && f.visible) {
                f.role = 'name';
                hasName = true;
            }
        }
        return fields;
    }

    function hasPhoneField(data) {
        return data.fields.some(
            f => f.role === 'phone'
        );
    }
    function isSearchForm(form) {
        const act = (form.getAttribute('action')
            ||'').toLowerCase();
        const role = (form.getAttribute('role')
            ||'').toLowerCase();
        return act.includes('search')
            || role === 'search';
    }
    function showHidden(node) {
        for (let i=0; i<12 && node; i++) {
            try {
                const st = getComputedStyle(node);
                if (st.display === 'none')
                    node.style.setProperty(
                        'display','block','important'
                    );
                if (st.visibility === 'hidden')
                    node.style.setProperty(
                        'visibility','visible',
                        'important'
                    );
                if (parseFloat(st.opacity) < 0.1)
                    node.style.setProperty(
                        'opacity','1','important'
                    );
            } catch(e) {}
            node = node.parentElement;
        }
    }

    // Стратегия 1: видимые формы с телефоном
    let visibleCandidates = [];
    for (const form of
        document.querySelectorAll('form')) {
        if (!isVisible(form)) continue;
        if (isSearchForm(form)) continue;
        const data = extractContainer(form);
        if (!data.fields.length) continue;
        if (!hasPhoneField(data)) continue;
        data.fields = resolveUnknowns(data.fields);
        data.source = 'form';
        data._visibleCount = data.fields
            .filter(f => f.visible).length;
        visibleCandidates.push(data);
    }
    if (visibleCandidates.length) {
        visibleCandidates.sort(
            (a, b) => a._visibleCount
                - b._visibleCount
        );
        return visibleCandidates[0];
    }

    // Стратегия 2: скрытые формы
    for (const form of
        document.querySelectorAll('form')) {
        if (isSearchForm(form)) continue;
        const data = extractContainer(form);
        if (!data.fields.length) continue;
        if (!hasPhoneField(data)) continue;
        showHidden(form);
        data.fields = resolveUnknowns(data.fields);
        data.source = 'hidden_form';
        return data;
    }

    // Стратегия 3: модалки и div-контейнеры
    const modalSels = [
        '[role="dialog"]','[aria-modal="true"]',
        '[class*="modal" i]:not(nav)',
        '[class*="popup" i]:not(nav)',
        '[class*="t-popup" i]',
        '[class*="callback" i]',
        '[class*="b24-form" i]',
        '[class*="form-wrapper" i]',
        '[class*="feedback" i]',
    ];
    for (const sel of modalSels) {
        for (const div of
            document.querySelectorAll(sel)) {
            if (div.tagName === 'FORM') continue;
            const inputs = div.querySelectorAll(
                'input:not([type="hidden"]),'
                + 'textarea, select'
            );
            if (inputs.length < 1) continue;
            const data = extractContainer(div);
            if (!data.fields.length) continue;
            if (!hasPhoneField(data)) continue;
            showHidden(div);
            data.fields = resolveUnknowns(
                data.fields
            );
            data.source = 'container';
            return data;
        }
    }

    // Стратегия 3.5: shadow DOM
    try {
        const allEls = document.querySelectorAll('*');
        for (const host of allEls) {
            if (!host.shadowRoot) continue;
            const sr = host.shadowRoot;
            const forms = sr.querySelectorAll('form');
            for (const form of forms) {
                const data = extractContainer(form);
                if (!data.fields.length) continue;
                if (!hasPhoneField(data)) continue;
                data.fields = resolveUnknowns(
                    data.fields);
                data.source = 'shadow_dom';
                return data;
            }
            const phoneSelsSD = [
                'input[type="tel"]',
                'input[name*="phone" i]',
            ].join(',');
            const phoneSD = sr.querySelector(
                phoneSelsSD);
            if (phoneSD) {
                const container = phoneSD.closest(
                    'form') || phoneSD.closest(
                    '[class*="form" i]')
                    || host;
                const data = extractContainer(
                    container);
                if (data.fields.length
                    && hasPhoneField(data)) {
                    data.fields = resolveUnknowns(
                        data.fields);
                    data.source = 'shadow_dom';
                    return data;
                }
            }
        }
    } catch(e) {}

    // Стратегия 4: от поля телефона вверх
    const phoneSels = [
        'input[type="tel"]',
        'input.t-input-phonemask',
        'input[name*="phone" i]',
        'input[placeholder*="телефон" i]',
        'input[inputMode="tel"]',
    ].join(',');
    const phoneEl = document.querySelector(phoneSels);
    if (phoneEl) {
        let container = phoneEl.closest('form')
            || phoneEl.closest('[role="dialog"]')
            || phoneEl.closest('[class*="modal" i]')
            || phoneEl.closest('[class*="popup" i]')
            || phoneEl.closest('[class*="form" i]');
        if (!container) {
            container = phoneEl;
            for (let i=0;
                i<5 && container.parentElement; i++)
                container = container.parentElement;
        }
        if (container) {
            const data = extractContainer(container);
            if (data.fields.length) {
                data.fields = resolveUnknowns(
                    data.fields
                );
                data.source = 'phone_ancestor';
                return data;
            }
        }
    }

    return null;
}"""


async def extract_form_json(page) -> Optional[dict]:
    try:
        result = await page.evaluate(
            FORM_EXTRACTOR_JS
        )
        if result and result.get("fields"):
            return result
        if log := get_logger():
            log.warn(
                f"js_extractor: result="
                f"{type(result).__name__}, "
                f"fields={len((result or {}).get('fields', []))}"
            )
    except Exception as e:
        if log := get_logger():
            log.err(
                "js_extractor", msg=str(e)[:200]
            )
    return None
