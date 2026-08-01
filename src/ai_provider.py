
import json
import os
import re
import time
import requests as _requests

try:
    from pathlib import Path as _Path
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(_Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

try:
    from logger import get_logger as _get_logger
except Exception:
    _get_logger = None

CLAUDE_URL = os.getenv(
    "CLAUDE_URL", "https://api.oneprovider.dev/v1/messages")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

DEEPSEEK_URL = os.getenv(
    "DEEPSEEK_URL", "https://api.deepseek.com/chat/completions")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro")

DEFAULT_PROVIDER = os.getenv("AI_PROVIDER", "deepseek").lower()

AI_PROVIDERS = ("deepseek", "claude")

_VISION_PROVIDERS = {"claude"}

def is_vision_provider(provider: str) -> bool:
    return (provider or "").lower() in _VISION_PROVIDERS

def _env_key(provider: str) -> str:
    if provider == "claude":
        return (os.getenv("CLAUDE_API_KEY", "")
                or os.getenv("ANTHROPIC_API_KEY", ""))
    if provider == "deepseek":
        return os.getenv("DEEPSEEK_API_KEY", "")
    return ""

_SYSTEM = (
    "Ты — эксперт по HTML-формам. Твоя задача — найти на странице "
    "форму заявки/обратной связи и составить пошаговый план её "
    "заполнения. Отвечай СТРОГО одним JSON-объектом, без markdown, "
    "без пояснений и текста вокруг."
)

_PROMPT = """\
На странице российской компании (часто клиника/услуги) нужно \
оставить заявку. Найди ПРАВИЛЬНУЮ форму и опиши, как её заполнить.

КАКУЮ форму выбирать:
- ДА: «обратный звонок», «запись на приём», «оставить заявку», \
«консультация», «перезвоните мне», «задать вопрос». Признаки: есть \
поле телефона; кнопка вида «Записаться / Заказать звонок / Отправить».
- НЕТ (не выбирай): поиск по сайту, форма входа/логина (есть пароль), \
подписка на рассылку (только e-mail + «Подписаться»), фильтры, \
калькуляторы. Если на странице только такие — верни {"f":false}.

Формат ответа (compact JSON, ключи сокращённые):
{"f":true,"fs":"CSS-селектор <form>","a":[
{"s":1,"a":"fill","f":"phone","sel":"CSS","v":"{phone}"},
{"s":2,"a":"fill","f":"name","sel":"CSS","v":"{name}"},
{"s":3,"a":"click","f":"checkbox","sel":"CSS"},
{"s":4,"a":"submit","f":"submit","sel":"CSS"}],
"cap":false,"ct":null}

Что означают поля JSON:
- f: true — форма заявки найдена; false — подходящей формы нет.
- fs: CSS-селектор самого элемента <form> (или контейнера формы).
- a: список действий по порядку (s — номер шага):
  - a="fill" — ввести значение v в поле sel;
  - a="click" — кликнуть (обычно чекбокс согласия);
  - a="select_first" — выбрать первый непустой пункт в <select>;
  - a="submit" — отправить форму (ВСЕГДА последний шаг).
- v: подставляемое значение-плейсхолдер (движок сам заменит):
  {phone},{name},{firstname},{lastname},{patronymic},{email},\
{comment},{date}.
- cap: true, если форму защищает капча; ct — её тип \
(recaptcha|hcaptcha|turnstile|smartcaptcha), иначе null.

ВАЖНЫЕ правила:
1. Телефон — обязательное поле. Если поля телефона в форме нет — \
скорее всего это не форма заявки: верни {"f":false}.
2. Заполни ВСЕ обязательные поля (помечены required/*): имя, \
телефон, при наличии — e-mail, город/услуга (select_first), дата.
3. Если есть чекбокс согласия на обработку данных — обязательно \
добавь шаг click по нему ПЕРЕД submit.
4. Давай МАКСИМАЛЬНО точные и уникальные CSS-селекторы (по id/name/\
type/placeholder), чтобы они однозначно попадали в нужный элемент.
5. Ровно один submit, и он всегда последним шагом.

HTML страницы:
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

class AIParseError(ValueError):

    def __init__(self, message: str, raw_text: str = ""):
        super().__init__(message)
        self.raw_text = raw_text or ""

def _parse(content):
    if not content or not content.strip():
        raise AIParseError(
            "пустой ответ AI",
            raw_text=content or "",
        )
    s = content.strip()
    s = re.sub(r'^```(?:json)?\s*', '', s)
    s = re.sub(r'\s*```\s*$', '', s).strip()
    start = s.find('{')
    if start < 0:
        raise AIParseError(
            f"JSON не найден; head={s[:120]!r}",
            raw_text=content,
        )
    try:
        raw, _end = json.JSONDecoder().raw_decode(
            s[start:],
        )
    except json.JSONDecodeError as e:
        raise AIParseError(
            f"raw_decode: {e}; "
            f"head={s[start:start + 200]!r}",
            raw_text=content,
        )
    if not isinstance(raw, dict):
        raise AIParseError(
            f"ожидался объект, получен {type(raw).__name__}",
            raw_text=content,
        )
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

def _claude_call(prompt, system, api_key, screenshot_b64=None):
    if screenshot_b64:
        user_content = [
            {"type": "text", "text": prompt},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": screenshot_b64,
                },
            },
        ]
    else:
        user_content = prompt
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
                 "content": user_content},
            ],
        },
        timeout=90,
    )
    resp.raise_for_status()
    return resp.json()

def _deepseek_call(prompt, system, api_key):
    sess = _requests.Session()
    sess.headers["Connection"] = "close"
    resp = sess.post(
        DEEPSEEK_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": DEEPSEEK_MODEL,
            "max_tokens": 600,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        },
        timeout=90,
    )
    resp.raise_for_status()
    return resp.json()

def _extract_claude(data):
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
    return content, tokens

def _extract_deepseek(data):
    choices = data.get("choices") or []
    content = ""
    if choices:
        msg = choices[0].get("message") or {}
        content = (msg.get("content") or "").strip()
    usage = data.get("usage", {})
    tokens = (
        usage.get("prompt_tokens", 0)
        + usage.get("completion_tokens", 0)
    )
    return content, tokens

def _dispatch(provider, prompt, api_key, screenshot_b64=None):
    if provider == "deepseek":
        data = _retry(
            _deepseek_call, prompt, _SYSTEM, api_key,
        )
        return _extract_deepseek(data)
    shot = screenshot_b64 if is_vision_provider(provider) else None
    data = _retry(
        _claude_call, prompt, _SYSTEM, api_key, shot,
    )
    return _extract_claude(data)

def _log_warn(msg, **kw):
    if not _get_logger:
        return
    try:
        lg = _get_logger()
        if lg:
            lg.warn(msg, **kw)
    except Exception:
        pass

def ask_ai_sync(page_html, url, api_key, provider=None,
                screenshot_b64=None):
    cleaned = clean_html(page_html)
    prompt = _PROMPT.replace("%%HTML%%", cleaned)

    provider = (provider or DEFAULT_PROVIDER).lower()
    if provider not in AI_PROVIDERS:
        raise RuntimeError(
            f"Неизвестный AI-провайдер: {provider}"
        )

    order = [provider] + [
        p for p in AI_PROVIDERS if p != provider
    ]

    total_tokens = 0
    last_exc = None
    tried_any = False

    for prov in order:
        key = api_key if prov == provider else ""
        if not key:
            key = _env_key(prov)
        if not key:
            last_exc = last_exc or RuntimeError(
                f"{prov} API ключ не указан"
            )
            _log_warn(f"AI:{prov} пропущен — нет ключа")
            continue

        tried_any = True
        try:
            content, tokens = _dispatch(
                prov, prompt, key, screenshot_b64,
            )
            total_tokens += tokens
        except Exception as e:
            total_tokens += getattr(e, "tokens", 0)
            last_exc = e
            _log_warn(
                f"AI:{prov} ошибка вызова → "
                f"следующий провайдер",
                err=str(e)[:160],
            )
            continue

        try:
            parsed = _parse(content)
        except AIParseError as e:
            e.tokens = total_tokens
            last_exc = e
            _log_warn(
                f"AI:{prov} битый JSON → "
                f"следующий провайдер",
                err=str(e)[:160],
            )
            continue

        return parsed, total_tokens, prov

    if not tried_any:
        raise RuntimeError(
            f"{provider} API ключ не указан"
        )
    if isinstance(last_exc, AIParseError):
        last_exc.tokens = total_tokens
        raise last_exc
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("AI: все провайдеры недоступны")

async def collect_full_html(page):
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
