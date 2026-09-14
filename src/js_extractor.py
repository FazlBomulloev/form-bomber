
from typing import Optional
from logger import get_logger

FORM_EXTRACTOR_JS = r"""() => {
    function isVisible(el) {
        if (!el) return false;
        const st = getComputedStyle(el);
        if (st.display === 'none'
            || st.visibility === 'hidden') return false;
        if (parseFloat(st.opacity || '1') < 0.05) return false;
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
        if (df) return el.tagName.toLowerCase()
            + '[data-field="' + df + '"]';
        const dn = el.getAttribute('data-name');
        if (dn) return el.tagName.toLowerCase()
            + '[data-name="' + dn + '"]';
        if (el.name) {
            const tag = el.tagName.toLowerCase();
            const sel = tag + '[name="' + el.name + '"]';
            const scope = el.closest('form')
                || el.closest('[role="dialog"]')
                || document;
            const matches = scope.querySelectorAll(sel);
            if (matches.length === 1) return sel;
            const tp = (el.type||'').toLowerCase();
            if (tp) {
                const sel2 = tag + '[name="' + el.name
                    + '"][type="' + tp + '"]';
                if (scope.querySelectorAll(sel2).length === 1)
                    return sel2;
            }
        }
        const ph = (el.placeholder||'').trim();
        if (ph && ph.length < 40) {
            const sel = el.tagName.toLowerCase()
                + '[placeholder="' + ph + '"]';
            const scope = el.closest('form') || document;
            if (scope.querySelectorAll(sel).length <= 2)
                return sel;
        }
        const tp = (el.type||'').toLowerCase();
        const cls = (el.className||'').toString()
            .split(/\s+/).filter(Boolean)[0];
        if (tp && cls) {
            try {
                return el.tagName.toLowerCase()
                    + '[type="' + tp + '"].' + CSS.escape(cls);
            } catch(e) {}
        }
        const ac = el.getAttribute('autocomplete');
        if (ac) return el.tagName.toLowerCase()
            + '[autocomplete="' + ac + '"]';
        const parent = el.closest('form') || el.parentElement;
        if (parent) {
            const tag = el.tagName.toLowerCase();
            const siblings = Array.from(
                parent.querySelectorAll(tag));
            const idx = siblings.indexOf(el) + 1;
            if (idx > 0) return tag + ':nth-of-type(' + idx + ')';
        }
        return null;
    }

    function getLabel(el) {
        if (el.id) {
            try {
                const lbl = document.querySelector(
                    'label[for="' + CSS.escape(el.id) + '"]');
                if (lbl) return (lbl.innerText||'')
                    .trim().substring(0, 120);
            } catch(e) {}
        }
        const closest = el.closest('label');
        if (closest) {
            const t = (closest.innerText||'').trim();
            if (t.length < 120) return t;
        }
        const al = el.getAttribute('aria-label');
        if (al) return al.trim().substring(0, 120);
        const ab = el.getAttribute('aria-labelledby');
        if (ab) {
            try {
                const ref = document.getElementById(ab);
                if (ref) return (ref.innerText||'')
                    .trim().substring(0, 120);
            } catch(e) {}
        }
        return '';
    }

    const W = {
        autocomplete: 10,
        type:          8,
        inputmode:     8,
        data_attr:     6,
        name:          5,
        label:         5,
        aria:          4,
        placeholder:   3,
        mask:          3,
        pattern:       2,
        class:         2,
        id:            2,
    };
    const ROLE_NORM = 14;
    const ROLE_THRESHOLD = 0.5;

    const PATTERNS = {
        phone: [
            /\bphone\b/i, /\btel(?:ephone)?\b/i,
            /\bmobile\b/i, /\bcell\b/i,
            /телефон/i, /\bтел\b/i, /моб/i,
            /phonemask/i, /tildaspec-phone/i,
            /номер.{0,3}тел/i,
        ],
        email: [
            /e-?mail/i, /почт/i, /электронн/i,
        ],
        firstname: [
            /first.?name/i, /given.?name/i,
            /^имя$/i, /\bимя\b/i,
        ],
        lastname: [
            /last.?name/i, /surname/i, /family.?name/i,
            /фамили/i,
        ],
        patronymic: [
            /patronymic/i, /middle.?name/i, /отчеств/i,
        ],
        name: [
            /\bname\b/i, /\bfio\b/i, /\bфио\b/i,
            /ваше.?имя/i, /full.?name/i, /полное.?имя/i,
            /\bимя\b/i, /\bимени/i,
        ],
        comment: [
            /comment/i, /message/i, /текст/i,
            /коммент/i, /сообщ/i, /вопрос/i,
            /пожелан/i, /опишите/i,
        ],
        date: [
            /\bdate\b/i, /дата/i, /когда/i,
            /удобн.{0,8}время/i,
        ],
        company: [
            /company/i, /компани/i, /организаци/i,
            /firm/i, /юрлиц/i,
        ],
        address: [
            /address/i, /адрес/i, /city/i, /город/i,
        ],
    };

    const AC_MAP = {
        'tel': 'phone',
        'tel-national': 'phone',
        'tel-local': 'phone',
        'tel-area-code': 'phone',
        'tel-country-code': 'phone',
        'tel-extension': 'phone',
        'mobile tel': 'phone',
        'mobile': 'phone',
        'email': 'email',
        'given-name': 'firstname',
        'family-name': 'lastname',
        'additional-name': 'patronymic',
        'name': 'name',
        'cc-name': 'name',
        'organization': 'company',
        'organization-title': 'company',
        'street-address': 'address',
        'address-line1': 'address',
        'address-line2': 'address',
        'address-level1': 'address',
        'address-level2': 'address',
        'postal-code': 'address',
        'country': 'address',
        'country-name': 'address',
        'bday': 'date',
        'bday-day': 'date',
    };

    function acToRole(ac) {
        if (!ac) return null;
        ac = ac.trim().toLowerCase();
        if (!ac || ac === 'on' || ac === 'off' || ac === 'nope')
            return null;
        if (AC_MAP[ac]) return AC_MAP[ac];
        const toks = ac.split(/\s+/).filter(Boolean);
        if (toks.length) {
            const last = toks[toks.length - 1];
            if (AC_MAP[last]) return AC_MAP[last];
        }
        return null;
    }

    function ngramFallback(bag) {
        if (!bag) return null;
        bag = bag.toLowerCase();
        const isLogin = /user|login|nick|pass|логин|псевдоним/.test(bag);
        if (/mail|почт|email/.test(bag)) return 'email';
        if (/тел|phon|\btel|mobil|\bмоб|gsm|whats|viber/.test(bag))
            return 'phone';
        if (/fam|фами|surname|lastname/.test(bag)) return 'lastname';
        if (/first.?name|given.?name|\bимя\b|имен/.test(bag))
            return 'firstname';
        if (!isLogin
            && /fio|фио|nam|klient|client|zovut|зовут/.test(bag))
            return 'name';
        return null;
    }

    function scoreRole(el, signals) {
        const scores = {};
        function add(role, w) {
            scores[role] = (scores[role] || 0) + w;
        }

        const ac = signals.ac;
        if (ac && AC_MAP[ac]) add(AC_MAP[ac], W.autocomplete);

        if (signals.type === 'tel') add('phone', W.type);
        if (signals.type === 'email') add('email', W.type);
        if (signals.type === 'date'
            || signals.type === 'datetime-local')
            add('date', W.type);

        if (signals.im === 'tel') add('phone', W.inputmode);
        if (signals.im === 'email') add('email', W.inputmode);
        if (signals.im === 'numeric'
            && (/phone|tel|телефон/.test(signals.name
                + ' ' + signals.label)))
            add('phone', W.inputmode / 2);

        const dataBag = (
            signals.df + ' ' + signals.dn
            + ' ' + signals.rule);
        if (signals.rule === 'phone') add('phone', W.data_attr);
        if (signals.rule === 'name') add('name', W.data_attr);
        if (signals.rule === 'email') add('email', W.data_attr);
        if (dataBag.trim()) {
            for (const [role, pats] of Object.entries(PATTERNS)) {
                for (const p of pats) {
                    if (p.test(dataBag)) {
                        add(role, W.data_attr);
                        break;
                    }
                }
            }
        }

        const sources = [
            ['name',        signals.name,        W.name],
            ['label',       signals.label,       W.label],
            ['aria',        signals.aria,        W.aria],
            ['title',       signals.title,       W.aria],
            ['placeholder', signals.ph,          W.placeholder],
            ['class',       signals.cls,         W.class],
            ['id',          signals.id,          W.id],
        ];
        for (const [, txt, weight] of sources) {
            if (!txt) continue;
            for (const [role, pats] of Object.entries(PATTERNS)) {
                for (const p of pats) {
                    if (p.test(txt)) {
                        add(role, weight);
                        break;
                    }
                }
            }
        }

        if (signals.ph && /\+7|\+9|\(\d{2,4}\)|___[ -]___/
            .test(signals.ph))
            add('phone', W.mask);

        if (signals.pattern) {
            const pat = signals.pattern;
            if (/\\d.{0,3}\\d/.test(pat)
                && /(?:tel|phone)/.test(signals.name
                    + ' ' + signals.label))
                add('phone', W.pattern);
            if (/@/.test(pat)) add('email', W.pattern);
        }

        return scores;
    }

    function classifyField(el) {
        const tag = el.tagName.toLowerCase();
        const type = (el.type||'').toLowerCase();
        const signals = {
            type,
            name:    (el.name||'').toLowerCase(),
            id:      (el.id||'').toLowerCase(),
            ph:      (el.placeholder||'').toLowerCase(),
            ac:      (el.getAttribute('autocomplete')||'')
                        .toLowerCase().trim(),
            cls:     (el.className||'').toString().toLowerCase(),
            im:      (el.inputMode||'').toLowerCase(),
            label:   getLabel(el).toLowerCase(),
            aria:    (el.getAttribute('aria-label')||'')
                        .toLowerCase(),
            title:   (el.getAttribute('title')||'')
                        .toLowerCase(),
            df:      (el.getAttribute('data-field')||'')
                        .toLowerCase(),
            dn:      (el.getAttribute('data-name')||'')
                        .toLowerCase(),
            rule:    (el.getAttribute('data-tilda-rule')||'')
                        .toLowerCase(),
            pattern: el.getAttribute('pattern') || '',
        };

        if (tag === 'textarea') {
            return {
                role: 'comment', confidence: 0.9,
                alternatives: [], signals,
            };
        }
        if (tag === 'select') {
            return {
                role: 'dropdown', confidence: 0.95,
                alternatives: [], signals,
            };
        }
        if (type === 'radio') {
            return {
                role: 'radio', confidence: 0.95,
                alternatives: [], signals,
            };
        }
        if (type === 'checkbox') {
            const sigStr = [
                signals.name, signals.id, signals.cls,
                signals.label, signals.aria,
            ].join(' ');
            const consent =
                /policy|consent|agree|terms|privacy|gdpr|соглас|политик|персональн|обработк|конфиденц/
                    .test(sigStr);
            return {
                role: consent
                    ? 'checkbox_consent' : 'checkbox_other',
                confidence: consent ? 0.85 : 0.6,
                alternatives: [], signals,
            };
        }

        const acRole = acToRole(signals.ac);
        if (acRole) {
            return {
                role: acRole, confidence: 0.97,
                alternatives: [], signals,
            };
        }

        const ngBag = [
            signals.name, signals.id, signals.cls,
            signals.label, signals.aria, signals.title,
            signals.ph, signals.df, signals.dn, signals.rule,
        ].join(' ');

        const scores = scoreRole(el, signals);
        const ranked = Object.entries(scores)
            .sort((a, b) => b[1] - a[1]);

        if (!ranked.length) {
            if (type === 'tel') return {
                role: 'phone', confidence: 0.7,
                alternatives: [], signals};
            if (type === 'email') return {
                role: 'email', confidence: 0.7,
                alternatives: [], signals};
            if (type === 'date') return {
                role: 'date', confidence: 0.8,
                alternatives: [], signals};
            const ng = ngramFallback(ngBag);
            if (ng) return {
                role: ng, confidence: 0.55,
                alternatives: [], signals};
            return {
                role: 'text_unknown', confidence: 0.0,
                alternatives: [], signals,
            };
        }

        const [topRole, topScore] = ranked[0];
        const confidence = Math.min(1, topScore / ROLE_NORM);
        const alternatives = ranked.slice(1, 4).map(
            ([r, s]) => [r, Math.min(1, s / ROLE_NORM)]);

        if (confidence < ROLE_THRESHOLD) {
            const ng = ngramFallback(ngBag);
            if (ng) {
                return {
                    role: ng, confidence: Math.max(0.55, confidence),
                    alternatives: [[topRole, confidence],
                        ...alternatives],
                    signals,
                };
            }
            return {
                role: 'text_unknown',
                confidence: confidence,
                alternatives: [[topRole, confidence],
                    ...alternatives],
                signals,
            };
        }

        return {role: topRole, confidence, alternatives, signals};
    }

    function extractMask(el, signals) {
        const ph = el.placeholder || '';
        if (/\+7|\+9|\(\d{2,4}\)|___|XXX|999/.test(ph))
            return ph;
        const mask = el.getAttribute('data-mask')
            || el.getAttribute('data-format')
            || el.getAttribute('data-input-mask');
        return mask || null;
    }

    function extractValidation(el) {
        const v = {};
        const p = el.getAttribute('pattern');
        if (p) v.pattern = p;
        const minL = el.getAttribute('minlength');
        if (minL) v.minLength = parseInt(minL, 10);
        const maxL = el.getAttribute('maxlength');
        if (maxL) v.maxLength = parseInt(maxL, 10);
        const mn = el.getAttribute('min');
        if (mn) v.min = mn;
        const mx = el.getAttribute('max');
        if (mx) v.max = mx;
        return Object.keys(v).length ? v : null;
    }

    function detectCaptcha(container) {
        const root = container || document;

        const rcEl = root.querySelector(
            '.g-recaptcha,[data-sitekey],'
            + 'iframe[src*="recaptcha"]');
        if (rcEl) {
            let sk = rcEl.getAttribute('data-sitekey');
            if (!sk) {
                const inner = root.querySelector(
                    '.g-recaptcha[data-sitekey]');
                if (inner) sk = inner.getAttribute('data-sitekey');
            }
            const size = (rcEl.getAttribute('data-size')||'')
                .toLowerCase();
            const cb = rcEl.getAttribute('data-callback');
            const isEnt = !!root.querySelector(
                'script[src*="recaptcha/enterprise"]')
                || /enterprise/i.test(
                    (rcEl.getAttribute('data-action')||''));
            if (sk || rcEl.tagName === 'IFRAME') {
                return {
                    type: 'recaptcha',
                    sitekey: sk || null,
                    is_invisible: size === 'invisible',
                    is_enterprise: isEnt,
                    callback: cb || null,
                };
            }
        }

        const hc = root.querySelector(
            '.h-captcha,iframe[src*="hcaptcha"]');
        if (hc) {
            return {
                type: 'hcaptcha',
                sitekey: hc.getAttribute('data-sitekey') || null,
                is_invisible: (
                    hc.getAttribute('data-size')||'') === 'invisible',
                is_enterprise: false,
                callback: hc.getAttribute('data-callback') || null,
            };
        }

        const ts = root.querySelector(
            '.cf-turnstile,iframe[src*="turnstile"]');
        if (ts) {
            return {
                type: 'turnstile',
                sitekey: ts.getAttribute('data-sitekey') || null,
                is_invisible: false,
                is_enterprise: false,
                callback: ts.getAttribute('data-callback') || null,
            };
        }

        const yc = root.querySelector(
            '.smart-captcha,[data-sitekey][class*="smart"],'
            + 'iframe[src*="smartcaptcha"]');
        if (yc) {
            return {
                type: 'yandex',
                sitekey: yc.getAttribute('data-sitekey') || null,
                is_invisible: yc.getAttribute('data-invisible')
                    === 'true',
                is_enterprise: false,
                callback: yc.getAttribute('data-callback') || null,
            };
        }

        return null;
    }

    function detectHoneypots(container) {
        const root = container || document;
        const out = [];
        for (const el of root.querySelectorAll(
            'input:not([type="hidden"]):not([type="submit"])'
            + ':not([type="button"])')) {
            const st = getComputedStyle(el);
            const r = el.getBoundingClientRect();
            const hiddenCss = (
                st.display === 'none'
                || st.visibility === 'hidden'
                || parseFloat(st.opacity||'1') < 0.05
                || r.width < 2 || r.height < 2);
            const offscreen = (
                Math.abs(parseFloat(st.left)) > 9000
                || Math.abs(parseFloat(st.top)) > 9000);
            const name = (el.name||'').toLowerCase();
            const id = (el.id||'').toLowerCase();
            const sig = name + ' ' + id;
            const trapName = /honeypot|website|url|fax|bot|trap|hp_/
                .test(sig);
            if (hiddenCss || offscreen || trapName) {
                const sel = buildSelector(el);
                if (sel) out.push(sel);
            }
        }
        return out;
    }

    function detectCsrf(container) {
        const root = container || document;
        const sels = [
            'input[name="_token"]',
            'input[name="csrf_token"]',
            'input[name="csrfmiddlewaretoken"]',
            'input[name="authenticity_token"]',
            'input[name="__RequestVerificationToken"]',
            'input[name*="csrf" i]',
        ];
        for (const s of sels) {
            const el = root.querySelector(s);
            if (el) {
                return {
                    selector: buildSelector(el) || s,
                    value: el.value || '',
                };
            }
        }
        return null;
    }

    function detectSubmitStrategy(container, submitEl) {
        if (container && container.tagName === 'FORM') {
            if (submitEl) {
                const tp = (submitEl.type||'').toLowerCase();
                if (tp === 'submit') return 'requestSubmit';
            }
            return 'requestSubmit';
        }
        return 'click';
    }

    function scoreForm(container, fields, checkboxes) {
        let score = 0;
        const hasPhone = fields.some(
            f => f.role === 'phone');
        const hasName = fields.some(
            f => ['name','firstname','lastname'].includes(f.role));
        const hasEmail = fields.some(f => f.role === 'email');
        const hasPassword = Array.from(container.querySelectorAll(
            'input[type="password"]')).some(isVisible);
        const visibleFields = fields.filter(f => f.visible).length;
        const radios = fields.filter(f => f.role === 'radio').length;

        if (hasPhone) score += 30;
        if (hasPhone && hasName) score += 15;
        if (visibleFields >= 2 && visibleFields <= 5) score += 10;
        if (radios > 4) score -= radios * 3;

        if (hasPassword) score -= 60;

        if (hasEmail && !hasPhone && visibleFields <= 2)
            score -= 25;

        const html = (container.innerHTML||'').toLowerCase();

        const searchBtn = Array.from(container.querySelectorAll(
            'button, input[type="submit"]')).some(b => {
                const t = ((b.innerText||b.value||'')+'').toLowerCase();
                return /search|найти|поиск/i.test(t);
            });
        if (searchBtn && !hasPhone) score -= 50;

        if (/заказать звонок|перезвон|callback/.test(html))
            score += 20;
        else if (/консультац/.test(html)) score += 12;
        else if (/записаться|запись/.test(html)) score += 8;
        if (/поиск|search|найти/.test(html)) score -= 25;
        if (/подписаться|subscribe|newsletter/.test(html))
            score -= 20;
        if (/отзыв|review/.test(html)) score -= 30;
        if (/логин|войти|sign.?in|log.?in/.test(html)
            && hasPassword) score -= 40;

        const textareas = container.querySelectorAll('textarea');
        for (const ta of textareas) {
            const ph = (ta.placeholder||'').toLowerCase();
            if (/отзыв|текст отзыва|review/.test(ph)) score -= 30;
        }
        const headings = container.querySelectorAll(
            'h1,h2,h3,h4,h5,h6');
        for (const hd of headings) {
            const ht = (hd.innerText||'').toLowerCase();
            if (/отзыв|отзывы|reviews/.test(ht)) score -= 25;
        }
        return score;
    }

    function findSubmit(container) {
        for (const sel of [
            'button[type="submit"]', 'input[type="submit"]',
        ]) {
            const el = container.querySelector(sel);
            if (el && isVisible(el))
                return {selector: buildSelector(el), el};
        }
        const submitTexts = [
            'отправить', 'записаться', 'оставить заявку',
            'заказать звонок', 'получить консультацию',
            'submit', 'send',
        ];
        for (const btn of container.querySelectorAll(
            'button, input[type="button"]')) {
            if (!isVisible(btn)) continue;
            const t = (btn.innerText||btn.value||'')
                .toLowerCase().trim();
            if (submitTexts.some(st => t.includes(st)))
                return {selector: buildSelector(btn), el: btn};
        }
        for (const btn of container.querySelectorAll(
            'button:not([type])')) {
            if (isVisible(btn))
                return {selector: buildSelector(btn), el: btn};
        }
        return {selector: null, el: null};
    }

    function rationalize(fields, container) {
        const seen = {};
        const dedupRoles = new Set([
            'phone', 'email', 'name', 'firstname', 'lastname',
            'patronymic', 'comment', 'date',
            'checkbox_consent',
        ]);
        for (const f of fields) {
            if (!dedupRoles.has(f.role)) continue;
            if (seen[f.role]) {
                if (f.alternatives && f.alternatives.length) {
                    const next = f.alternatives.find(
                        a => !seen[a[0]]
                            && a[0] !== 'text_unknown');
                    if (next) {
                        f.role = next[0];
                        f.confidence = next[1];
                    } else {
                        f.role = 'text_unknown';
                        f.confidence = 0;
                    }
                } else {
                    f.role = 'text_unknown';
                    f.confidence = 0;
                }
            }
            if (dedupRoles.has(f.role)) seen[f.role] = true;
        }

        const hasFL = (
            fields.some(f => f.role === 'firstname')
            && fields.some(f => f.role === 'lastname'));
        if (hasFL) {
            for (const f of fields) {
                if (f.role === 'name') {
                    f.role = 'text_unknown';
                    f.confidence = 0;
                }
            }
        }

        const hasPhone = fields.some(f => f.role === 'phone');
        const hasAnyName = fields.some(
            f => ['name','firstname','lastname'].includes(f.role));
        if (hasPhone && !hasAnyName) {
            const unknowns = fields.filter(
                f => f.role === 'text_unknown' && f.visible
                    && f.tag !== 'textarea'
                    && f.type !== 'email' && f.type !== 'tel');
            let pick = unknowns.find(f => f.name_hint);
            if (!pick) pick = unknowns.find(f => f.tilda_input);
            if (!pick && unknowns.length >= 1) pick = unknowns[0];
            if (pick) {
                pick.role = 'name';
                pick.confidence = Math.max(0.5, pick.confidence);
            }
        }

        return fields;
    }

    function isRejectedForm(container, fields) {
        if (Array.from(container.querySelectorAll(
            'input[type="password"]')).some(isVisible))
            return 'has_password';

        const hasPhone = fields.some(f => f.role === 'phone');
        const hasEmail = fields.some(f => f.role === 'email');
        const visibleCount = fields.filter(f => f.visible).length;
        if (hasEmail && !hasPhone && visibleCount <= 2)
            return 'newsletter';

        const sigText = (
            (container.getAttribute('action')||'') + ' '
            + (container.getAttribute('role')||'') + ' '
            + (container.className||'') + ' '
            + (container.id||'')).toLowerCase();
        if (/search/.test(sigText) && !hasPhone) {
            const hasSearchBtn = Array.from(
                container.querySelectorAll(
                    'button, input[type="submit"]')).some(b => {
                    const t = ((b.innerText||b.value||'')+'')
                        .toLowerCase();
                    return /search|найти|поиск/i.test(t);
                });
            if (hasSearchBtn) return 'search_form';
        }

        return null;
    }

    function isSearchForm(form) {
        const act = (form.getAttribute('action')||'').toLowerCase();
        const role = (form.getAttribute('role')||'').toLowerCase();
        return act.includes('search') || role === 'search';
    }

    function showHidden(node) {
        for (let i = 0; i < 12 && node; i++) {
            try {
                const st = getComputedStyle(node);
                if (st.display === 'none')
                    node.style.setProperty(
                        'display','block','important');
                if (st.visibility === 'hidden')
                    node.style.setProperty(
                        'visibility','visible','important');
                if (parseFloat(st.opacity) < 0.1)
                    node.style.setProperty(
                        'opacity','1','important');
            } catch(e) {}
            node = node.parentElement;
        }
    }

    function extractContainer(container) {
        const fields = [];
        const checkboxes = [];
        const allInputs = container.querySelectorAll(
            'input:not([type="hidden"])'
            + ':not([type="submit"])'
            + ':not([type="button"])'
            + ':not([type="reset"]),'
            + 'textarea, select');
        for (const el of allInputs) {
            const type = (el.type||'').toLowerCase();
            const vis = isVisible(el)
                || type === 'checkbox' || type === 'radio';
            if (!vis && type !== 'checkbox'
                && type !== 'radio') continue;

            const cls = classifyField(el);
            const selector = buildSelector(el);
            if (!selector) continue;

            const fld = {
                tag: el.tagName.toLowerCase(),
                type: type,
                name: el.name || '',
                id: el.id || '',
                placeholder: (el.placeholder||'').trim(),
                label: getLabel(el),
                role: cls.role,
                confidence: cls.confidence,
                alternatives: cls.alternatives,
                visible: isVisible(el),
                required: el.required
                    || el.getAttribute('aria-required') === 'true',
                selector: selector,
                priority: vis ? 0 : 1,
                validation: extractValidation(el),
                mask: extractMask(el, cls.signals),
            };

            const _nameBag = [
                fld.name, fld.id, fld.placeholder, fld.label,
                (el.getAttribute('data-tilda-rule') || ''),
                (el.getAttribute('data-tilda-fieldname') || ''),
            ].join(' ').toLowerCase();
            fld.name_hint = /\bимя\b|\bимени\b|\bname\b|\bфио\b|\bфамили|как вас зовут|как к вам обращаться|ваше имя/
                .test(_nameBag);
            fld.tilda_input = (
                el.hasAttribute('data-tilda-rule')
                || el.hasAttribute('data-tilda-req')
                || /t-input(?!-phonemask)/i.test(
                    (el.className || '').toString()));

            if (el.tagName === 'SELECT') {
                fld.options = Array.from(el.options).slice(0, 8)
                    .map(o => ({
                        text: o.text.trim(),
                        value: o.value,
                    }));
            }

            if (type === 'checkbox') {
                checkboxes.push({
                    selector: selector,
                    role: cls.role === 'checkbox_consent'
                        ? 'consent' : 'other',
                    confidence: cls.confidence,
                    required: fld.required,
                    default_checked: !!el.checked,
                    label: fld.label,
                    name: fld.name,
                });
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
                    formSelector = 'form#' + container.id;
                }
            } else if (container.action
                && container.action !== window.location.href) {
                formSelector = 'form[action="'
                    + container.getAttribute('action') + '"]';
            } else {
                const cls = (container.className||'')
                    .split(' ').filter(c => c)[0];
                if (cls) {
                    try {
                        formSelector = 'form.' + CSS.escape(cls);
                    } catch(e) {
                        formSelector = 'form.' + cls;
                    }
                } else {
                    formSelector = 'form';
                }
            }
        }

        const sub = findSubmit(container);
        const strategy = detectSubmitStrategy(container, sub.el);

        const captcha = detectCaptcha(container);
        const honeypots = detectHoneypots(container);
        const csrf = detectCsrf(container);

        return {
            form_selector: formSelector,
            submit_selector: sub.selector,
            submit_strategy: strategy,
            fields: fields,
            checkboxes: checkboxes,
            captcha_hint: captcha,
            honeypots: honeypots,
            csrf_token: csrf,
            score: scoreForm(container, fields, checkboxes),
        };
    }

    function finalize(data, container, source) {
        data.fields = rationalize(data.fields, container);
        const rejectReason = isRejectedForm(container, data.fields);
        if (rejectReason) {
            data._rejected = rejectReason;
            return null;
        }
        data.source = source;
        return data;
    }

    function hasPhoneField(data) {
        return data && data.fields.some(
            f => f.role === 'phone');
    }


    let visibleCandidates = [];
    for (const form of document.querySelectorAll('form')) {
        if (!isVisible(form)) continue;
        if (isSearchForm(form)) continue;
        const raw = extractContainer(form);
        if (!raw.fields.length) continue;
        if (!hasPhoneField(raw)) continue;
        const data = finalize(raw, form, 'form');
        if (!data) continue;
        data._visibleCount = data.fields.filter(
            f => f.visible).length;
        visibleCandidates.push(data);
    }
    if (visibleCandidates.length) {
        visibleCandidates.sort((a, b) => {
            if (b.score !== a.score) return b.score - a.score;
            return a._visibleCount - b._visibleCount;
        });
        return visibleCandidates[0];
    }

    for (const form of document.querySelectorAll('form')) {
        if (isSearchForm(form)) continue;
        const raw = extractContainer(form);
        if (!raw.fields.length) continue;
        if (!hasPhoneField(raw)) continue;
        showHidden(form);
        const data = finalize(raw, form, 'hidden_form');
        if (data) return data;
    }

    const modalSels = [
        '[role="dialog"]', '[aria-modal="true"]',
        '[class*="modal" i]:not(nav)',
        '[class*="popup" i]:not(nav)',
        '[class*="t-popup" i]',
        '[class*="callback" i]',
        '[class*="b24-form" i]',
        '[class*="form-wrapper" i]',
        '[class*="feedback" i]',
    ];
    for (const sel of modalSels) {
        for (const div of document.querySelectorAll(sel)) {
            if (div.tagName === 'FORM') continue;
            const inputs = div.querySelectorAll(
                'input:not([type="hidden"]),textarea, select');
            if (inputs.length < 1) continue;
            const raw = extractContainer(div);
            if (!raw.fields.length) continue;
            if (!hasPhoneField(raw)) continue;
            showHidden(div);
            const data = finalize(raw, div, 'container');
            if (data) return data;
        }
    }

    try {
        const allEls = document.querySelectorAll('*');
        for (const host of allEls) {
            if (!host.shadowRoot) continue;
            const sr = host.shadowRoot;
            const forms = sr.querySelectorAll('form');
            for (const form of forms) {
                const raw = extractContainer(form);
                if (!raw.fields.length) continue;
                if (!hasPhoneField(raw)) continue;
                const data = finalize(raw, form, 'shadow_dom');
                if (data) return data;
            }
            const phoneSelsSD = [
                'input[type="tel"]',
                'input[name*="phone" i]',
            ].join(',');
            const phoneSD = sr.querySelector(phoneSelsSD);
            if (phoneSD) {
                const container = phoneSD.closest('form')
                    || phoneSD.closest('[class*="form" i]')
                    || host;
                const raw = extractContainer(container);
                if (raw.fields.length && hasPhoneField(raw)) {
                    const data = finalize(
                        raw, container, 'shadow_dom');
                    if (data) return data;
                }
            }
        }
    } catch(e) {}

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
            for (let i = 0; i < 5 && container.parentElement; i++)
                container = container.parentElement;
        }
        if (container) {
            const raw = extractContainer(container);
            if (raw.fields.length) {
                const data = finalize(
                    raw, container, 'phone_ancestor');
                if (data) return data;
            }
        }
    }

    const ctaRe = /заказать звонок|перезвон|callback|записаться|\bзапись\b|оставить заявк|\bзаявк|консультац/i;
    for (const form of document.querySelectorAll('form')) {
        if (isSearchForm(form)) continue;
        const sub = findSubmit(form);
        const btnText = sub.el
            ? ((sub.el.innerText || sub.el.value || '') + '')
                .toLowerCase()
            : '';
        const formText = (form.innerText || '')
            .toLowerCase().slice(0, 400);
        if (!ctaRe.test(btnText) && !ctaRe.test(formText)) continue;
        const raw = extractContainer(form);
        if (!raw.fields.length) continue;
        const hasNameOrEmail = raw.fields.some(
            f => ['name','firstname','lastname','email']
                .includes(f.role));
        const hasTextarea = raw.fields.some(
            f => f.tag === 'textarea');
        if (!hasNameOrEmail && !hasTextarea) continue;
        showHidden(form);
        const data = finalize(raw, form, 'cta_no_phone');
        if (!data) continue;
        if (!hasPhoneField(data)) {
            const usedSel = new Set(data.fields.filter(
                f => ['name','firstname','lastname','email',
                    'comment','date'].includes(f.role))
                .map(f => f.selector));
            let phoneTarget = form.querySelector(
                'input[type="tel"],input[inputmode="numeric"]');
            if (!phoneTarget) {
                for (const inp of form.querySelectorAll(
                    'input[type="text"],input:not([type])')) {
                    if (!isVisible(inp)) continue;
                    if ((inp.value || '').trim()) continue;
                    const s = buildSelector(inp);
                    if (s && usedSel.has(s)) continue;
                    phoneTarget = inp; break;
                }
            }
            if (phoneTarget) {
                const psel = buildSelector(phoneTarget);
                if (psel) {
                    data.fields.unshift({
                        tag: phoneTarget.tagName.toLowerCase(),
                        type: (phoneTarget.type || '').toLowerCase(),
                        name: phoneTarget.name || '',
                        id: phoneTarget.id || '',
                        placeholder: (
                            phoneTarget.placeholder || '').trim(),
                        label: getLabel(phoneTarget),
                        role: 'phone',
                        confidence: 0.4,
                        alternatives: [],
                        visible: isVisible(phoneTarget),
                        required: false,
                        selector: psel,
                        priority: 0,
                        validation: null,
                        mask: null,
                        phone_fallback: true,
                    });
                    data.phone_fallback_hint = psel;
                }
            }
        }
        return data;
    }

    return null;
}"""

async def extract_form_json(page) -> Optional[dict]:
    try:
        result = await page.evaluate(FORM_EXTRACTOR_JS)
        if result and result.get("fields"):
            return result
        if log := get_logger():
            log.step(
                "js_extractor",
                f"пусто: type={type(result).__name__} "
                f"fields=0",
            )
    except Exception as e:
        if log := get_logger():
            log.err("js_extractor", msg=str(e)[:200])
    return None
