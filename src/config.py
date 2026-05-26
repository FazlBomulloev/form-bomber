PORT           = 8002
DB_PATH        = "data/checker_ai.db"
PROFILES_PATH  = "data/ai_profiles.json"
CONCURRENCY    = 5
AI_CONCURRENCY = 3

RUCAPTCHA_IN  = "https://rucaptcha.com/in.php"
RUCAPTCHA_RES = "https://rucaptcha.com/res.php"

OPENAI_VISION_BASE_URL = "https://api.artemox.com/v1"
OPENAI_VISION_MODEL    = "gpt-4o"
VISION_MAX_WIDTH       = 1280

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

MODAL_KW_CALLBACK = [
    "заказать звонок", "обратный звонок",
    "перезвоните", "жду звонка",
    "callback", "перезвонить",
]
MODAL_KW_CONSULT = [
    "получить консультацию",
    "бесплатная консультация",
    "консультация", "связаться",
    "обратная связь", "оставить заявку",
]
MODAL_KW_BOOK = [
    "записаться", "запись",
    "записаться на приём", "записаться на прием",
    "онлайн-запись", "запись онлайн",
    "выбрать время", "забронировать",
]
MODAL_KW_OTHER = [
    "заявк", "звонок", "перезвон", "оставить",
    "рассчитать", "стоимость", "диагностик",
    "узнать цену", "получить",
    "задать вопрос", "написать нам",
    "отправить сообщение",
]
MODAL_KEYWORDS = (
    MODAL_KW_CALLBACK + MODAL_KW_CONSULT
    + MODAL_KW_BOOK + MODAL_KW_OTHER
)

BITRIX_FORM_TRIGGER_SEL = (
    "[data-b24-form-id],[data-bx-form-id],"
    "[data-bx-web-form-id],[data-bx24-form-id],"
    "button.b24-form-btn,a.b24-form-btn,"
    ".b24-form-button button,.b24-form-button a,"
    "[class*='b24-form-button' i],"
    "[onclick*='b24form' i],"
    "[onclick*='CrmWebForm' i]"
)

PHONE_FALLBACKS = [
    'input.t-input-phonemask',
    'input[class*="t-input-phonemask" i]',
    'input.js-input-phone',
    'input._phone',
    'input[name="phone"]',
    'input[name="tel"]',
    'input[data-tel-input]',
    'input[data-mask*="+7" i]',
    'input[placeholder*="номер телефона" i]',
    'input[placeholder*="ваш номер" i]',
    'input[name="tildaspec-phone-part[]"]',
    'input[data-field*="phone" i]',
    'input[data-name*="phone" i]',
    'input[placeholder*="телефон" i]',
    'input[placeholder*="phone" i]',
    'input[type="tel"]',
    'input[name*="phone" i]',
    'input[name*="tel" i]',
    'input[id*="phone" i]',
    'input[class*="phone" i]',
]

WIDGET_BLACKLIST_RE = (
    r"callbackhunter|envybox|comagic"
    r"|mango|uiscom|roistat|marquiz|jivosite"
    r"|jivo|chatra|verbox|redconnect|callibri"
    r"|livechat|pozvonim|callkeeper"
    r"|widget-phone|widget_phone"
)

TRIGGER_BUTTON_SEL = (
    "button, a, [role='button'], "
    "span.btn, div.btn, "
    "span[class*='btn' i], div[class*='btn' i], "
    "[data-event], [onclick], "
    "[class*='callback' i], "
    "[data-b24-form-id], [data-param-id]"
)

SUCCESS_TEXTS = [
    "спасибо", "thank", "заявка принята",
    "заявка отправлена", "заявка получена",
    "скоро свяжемся", "перезвоним", "обработаем",
    "успешно", "отправлено", "получили вашу",
    "ваша заявка", "записаны", "подтверждение",
    "confirmation", "свяжемся с вами",
    "заявку получили", "ждите звонка",
    "мы свяжемся", "свяжемся в ближайшее",
    "заявка создана", "сообщение отправ",
    "запрос отправ", "успешно отправ",
    "благодарим", "приняли ваш",
    "данные отправ", "обращение принят",
    "администратор свяжется",
    "спасибо за заявку", "свяжется с вами",
]

ERROR_PHRASES = [
    "произошла ошибка", "ошибка сервера",
    "ошибка отправки", "ошибка при отправке",
    "не удалось отправить", "повторите попытку",
    "попробуйте ещё раз", "возникла ошибка",
    "обязательные поля",
    "заполните все обязательные",
    "обязательно для заполнения",
    "не все поля заполнены",
    "некорректный номер", "некорректные данные",
    "неверный формат", "server error",
    "something went wrong", "failed to submit",
    "captcha verification failed",
    "validation failed",
    "не прошли проверку", "invalid recaptcha",
    "выберите значок", "введите проверочный код",
    "not a robot", "превысили количество",
    "ни одно поле не заполнено",
    "содержат ошибочные данные",
    "слишком большой объём",
    "нужно дать согласие", "ошибочка вышла",
    "ошибка отправления",
    "отправление не удалось",
    "слишком много запросов",
    "too many requests",
]

COOKIE_BTN_TEXTS = [
    "принять", "принять все", "принимаю",
    "согласен", "согласиться", "согласна",
    "ok", "ок", "понятно", "хорошо", "закрыть",
    "принять cookie", "accept", "agree",
    "allow all", "разрешить", "да", "продолжить",
]

COOKIE_CONSENT_SCRIPT = r"""() => {
    const ck = {
        'cookieConsent':'accepted',
        'cookie_consent':'true',
        'gdpr':'true','gdpr_consent':'true',
        'cookies_accepted':'true',
        'consent':'true','CookieConsent':'allow',
    };
    try {
        Object.entries(ck).forEach(([k,v]) => {
            try { localStorage.setItem(k,v); }
            catch(e) {}
        });
    } catch(e) {}
    try {
        window.OneTrust = {
            OnConsentChanged:()=>{},
            IsAlertBoxClosed:()=>true
        };
        window.__tcfapi = (cmd,ver,cb) => {
            if(cb) cb({gdprApplies:false},true);
        };
    } catch(e) {}
}"""
