"""AI-провайдер: только Claude.
Включает очистку HTML и сбор iframe-контента."""

import json
import re
import time
import requests as _requests

CLAUDE_URL = "https://api.oneprovider.dev/v1/messages"
CLAUDE_MODEL = "claude-sonnet-4-6"

_SYSTEM = "Верни ТОЛЬКО JSON. Без markdown и текста."

_PROMPT = """\
Найди контактную форму на странице (обратный звонок, запись, \
консультация, заявка) и составь план заполнения.

JSON формат:
{"f":true,"fs":"CSS селектор формы","a":[
{"s":1,"a":"fill","f":"phone","sel":"CSS","v":"{phone}"},
{"s":2,"a":"fill","f":"name","sel":"CSS","v":"{name}"},
{"s":3,"a":"click","f":"checkbox","sel":"CSS"},
{"s":4,"a":"submit","f":"submit","sel":"CSS"}],
"cap":false,"ct":null}

Правила:
- f=true если форма найдена, false если нет
- a=actions: s=шаг, a=действие (fill|click|select_first|submit), \
f=поле, sel=CSS selector, v=значение
- Значения v: {phone},{name},{firstname},{lastname},{patronymic},\
{email},{comment},{date}
- submit ВСЕГДА последний шаг
- select_first для <select>
- cap/ct: капча recaptcha|hcaptcha|turnstile|smartcaptcha
- Нет телефона → f:false

HTML:
%%HTML%%"""


_STRIP_TAGS = re.compile(
    r'<(script|style|noscript|svg|path|iframe|video'
    r'|audio|picture|source|link|meta|symbol|defs'
    r'|linearGradient|radialGradient|clipPath'
    r'|template)\b[^>]*>.*?</\1>',
    re.S | re.I,
)
_STRIP_TAGS_VOID = re.compile(
    r'<(script|style|link|meta|br|hr|img|input'
    r'|source|track|wbr)\b[^>]*/?>',
    re.I,
)
_COMMENTS = re.compile(r'<!--.*?-->', re.S)
_KEEP_ATTRS = {
    'id', 'name', 'type', 'placeholder', 'class',
    'action', 'method', 'href', 'role', 'for',
    'value', 'required', 'autocomplete', 'inputmode',
    'data-field', 'data-name', 'data-sitekey',
    'data-callback', 'aria-label', 'aria-required',
    'aria-modal', 'data-b24-form-id',
}
_ATTR_RE = re.compile(
    r'\s([a-zA-Z][a-zA-Z0-9_-]*(?::[a-zA-Z0-9_-]+)?)'
    r'\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s>]+)'
)
_EMPTY_TAG = re.compile(
    r'<(div|span|p|section|article|aside|main'
    r'|ul|ol|li|dl|dt|dd|figure|figcaption'
    r'|b|i|em|strong|small|u|s)\b[^>]*>\s*'
    r'</\1>',
    re.I,
)
_MULTI_WS = re.compile(r'[ \t]+')
_MULTI_NL = re.compile(r'\n{3,}')


def _strip_attrs(tag_match):
    full = tag_match.group(0)
    lt = full.index('<')
    gt_search = re.search(r'[\s/>]', full[lt + 1:])
    if not gt_search:
        return full
    tag_end = lt + 1 + gt_search.start()
    tag_name = full[lt + 1:tag_end]
    kept = []
    for m in _ATTR_RE.finditer(full):
        attr_name = m.group(1).lower()
        if attr_name in _KEEP_ATTRS:
            kept.append(m.group(0))
    close = '/>' if full.rstrip().endswith('/>') else '>'
    return f'<{tag_name}{"".join(kept)}{close}'


def clean_html(raw_html: str, limit: int = 8000) -> str:
    h = raw_html
    h = _COMMENTS.sub('', h)
    h = _STRIP_TAGS.sub('', h)
    h = _STRIP_TAGS_VOID.sub(
        lambda m: m.group(0)
        if m.group(1).lower() == 'input'
        else '', h,
    )
    for tag in ('nav', 'footer', 'header'):
        pat = re.compile(
            rf'<{tag}\b[^>]*>(.*?)</{tag}>',
            re.S | re.I,
        )
        for m in pat.finditer(h):
            inner = m.group(1)
            has_form = bool(re.search(
                r'<(form|input)\b', inner, re.I,
            ))
            if not has_form:
                h = h.replace(m.group(0), '')
    h = re.compile(
        r'<(img|br|hr|track|wbr)\b[^>]*/?>',
        re.I,
    ).sub('', h)
    h = re.sub(
        r'<[a-zA-Z][^>]*>',
        _strip_attrs, h,
    )
    for _ in range(3):
        h = _EMPTY_TAG.sub('', h)
    h = re.sub(
        r'(?<=>)([^<]{80,}?)(?=<)',
        lambda m: m.group(1)[:60] + '…',
        h,
    )
    h = _MULTI_WS.sub(' ', h)
    lines = [
        ln.strip() for ln in h.splitlines()
        if ln.strip()
    ]
    h = '\n'.join(lines)
    h = _MULTI_NL.sub('\n\n', h)

    if len(h) <= limit:
        return h

    form_re = re.compile(
        r'<form\b[^>]*>.*?</form>',
        re.S | re.I,
    )
    forms_html = '\n'.join(
        m.group(0) for m in form_re.finditer(h)
    )
    input_containers = re.findall(
        r'<(?:div|section|aside)[^>]*>'
        r'(?:(?!<(?:div|section|aside)\b).)*?'
        r'<input\b[^>]*type=["\']?tel[^>]*>.*?'
        r'</(?:div|section|aside)>',
        h, re.S | re.I,
    )
    containers_html = '\n'.join(input_containers)
    priority = forms_html or containers_html

    if priority:
        budget = limit - len(priority) - 100
        if budget > 500:
            rest = form_re.sub('', h)
            for ic in input_containers:
                rest = rest.replace(ic, '')
            rest = rest[:budget]
            return rest + '\n' + priority
        return priority[:limit]

    return h[:limit] + '\n...(обрезано)'


def _expand_ai_response(short: dict) -> dict:
    actions = []
    for a in short.get('a', []):
        act = {
            'step': a.get('s', 0),
            'action': a.get('a', ''),
            'field': a.get('f', ''),
            'selector': a.get('sel', ''),
        }
        if 'v' in a:
            act['value'] = a['v']
        if 't' in a:
            act['type'] = a['t']
        actions.append(act)
    return {
        'form_found': short.get('f', False),
        'form_selector': short.get('fs'),
        'actions': actions,
        'has_captcha': short.get('cap', False),
        'captcha_type': short.get('ct'),
        'notes': short.get('n', ''),
    }


def _parse(content):
    content = re.sub(
        r'^```(?:json)?\s*', '', content.strip(),
    )
    content = re.sub(r'\s*```\s*$', '', content)
    raw = json.loads(content)
    if 'a' in raw and 'actions' not in raw:
        return _expand_ai_response(raw)
    return raw


def _retry(fn, *args, retries=3, delay=4):
    last = None
    for i in range(retries):
        try:
            return fn(*args)
        except Exception as e:
            last = e
            if i < retries - 1:
                wait = delay * (2 ** i)
                time.sleep(min(wait, 30))
    raise last


def _claude_call(prompt, system, api_key):
    sess = _requests.Session()
    sess.headers["Connection"] = "close"
    resp = sess.post(
        CLAUDE_URL,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "User-Agent": "curl/7.68.0",
        },
        json={
            "model": CLAUDE_MODEL,
            "max_tokens": 600,
            "system": system,
            "messages": [
                {"role": "user",
                 "content": prompt},
            ],
        },
        timeout=90,
    )
    resp.raise_for_status()
    return resp.json()


def ask_ai_sync(page_html, url, claude_key):
    """Принимает сырой HTML, чистит, отправляет в Claude.
    Возвращает (result_dict, tokens, provider)."""
    cleaned = clean_html(page_html)
    prompt = _PROMPT.replace("%%HTML%%", cleaned)

    if not claude_key:
        raise RuntimeError("Claude API ключ не указан")

    data = _retry(
        _claude_call, prompt, _SYSTEM, claude_key,
    )
    content = ""
    for block in data.get("content", []):
        if block.get("type") == "text":
            content = block["text"].strip()
            break
    usage = data.get("usage", {})
    tokens = (
        usage.get("input_tokens", 0)
        + usage.get("output_tokens", 0)
    )
    return _parse(content), tokens, "claude"


async def collect_full_html(page):
    """Собирает HTML главной страницы + все iframe."""
    parts = []
    try:
        main_html = await page.content()
        parts.append(main_html)
    except Exception:
        pass
    try:
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                frame_html = await frame.content()
                if frame_html and len(frame_html) > 100:
                    parts.append(
                        f"<!-- IFRAME: {frame.url} -->"
                        f"\n{frame_html}"
                    )
            except Exception:
                continue
    except Exception:
        pass
    return "\n".join(parts)
