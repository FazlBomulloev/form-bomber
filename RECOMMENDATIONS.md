# Form-Bomber — рекомендации по успешности заявок

Документ собран по итогам изучения реального кода 7 профильных
опенсорс-проектов + 3 индустриальных эталонов (Chromium/Firefox
Autofill, Scrapy). Цель — поднять процент успешных заявок: формы
лучше находятся, правильнее заполняются, точнее детектится успех.

Дата: 2026-08-01.

---

## 1. Что изучено (10 источников)

| Проект | Класс | Что взять |
|---|---|---|
| **Chromium Autofill** | эталон эвристики | regex-словарь + **crowdsourcing-сервер** + rationalization |
| **Firefox Form Autofill** | эталон | приоритет HTML-атрибута `autocomplete` над эвристикой |
| **yash0945/auto-contact-form-bot** | прямой аналог (Playwright) | **STEALTH_JS**, поиск /contacts, реалистичный контекст |
| **TeamHG-Memex/Formasaurus** | ML-классификатор форм | **классификация ТИПА формы**, char n-gram фичи |
| **TeamHG-Memex/crazy-form-submitter** | Scrapy | `FormRequest.from_response` — **сохранить все поля** |
| **MechanicalSoup** | stateful-браузер | полная сериализация input-типов (select/radio/checkbox) |
| **sathoro/python-crawler** | requests-краулер | **перенос hidden/CSRF** + единая session-cookie |
| **Skyvern** | LLM+vision агент | скриншот+DOM в LLM, кэш детерминированных workflow |
| **browser-use** | LLM-агент | reasoning поверх Playwright как последний уровень |
| **Scrapy `FormRequest.from_response`** | фреймворк-приём | авто-сбор всех input'ов, override только нужных |

---

## 2. Где form-bomber УЖЕ на уровне лучших (не переписывать)

- **Классификация полей** — `js_extractor.py` повторяет Chromium Autofill
  (regex по name/id/placeholder/label + rationalization). Это эталон;
  простые аналоги (`auto-contact-form-bot`, `match_field`) заметно слабее.
- **Детект успеха** — многоуровневый (сеть + CMS-события + DOM) уже
  мощнее наивного «ищем слово thank/success», как у большинства аналогов.
- **Архитектура `cached → эвристика → AI`** — тот же гибрид, к которому
  пришёл Skyvern («LLM reasoning + cacheable deterministic workflows»).
- **Кэш профилей форм в БД** — это твой локальный аналог crowdsourcing
  Chromium: 5 из 6 успехов приходят из кэша.

Вывод: эвристический слой переписывать не нужно. Точки роста — это
стадии, которых у Chromium Autofill вообще нет (discovery, submit,
success, anti-bot).

---

## 3. Уже внедрено в этой сессии ✅

Отмечено, чтобы не дублировать (коммит `51499f3`):

- ✅ 302/PRG-редирект на нашем POST → успех (`result_detect.py`)
- ✅ «форменный» POST по same-origin, а не только по телефону в теле
- ✅ `wait_for_response` вокруг submit (не терять быстрый AJAX)
- ✅ верификация телефона по значению + сброс маски
- ✅ пред-submit дозаполнение обязательных `[required]`/`:invalid`
- ✅ эскалация submit при 0 POST (`requestSubmit`/dispatch/Enter)
- ✅ роль «имя» для Tilda-инпутов без `name`
- ✅ запасная стратегия discovery без распознанного phone-поля
- ✅ AI-endpoint/ключи в `.env` + перебор провайдеров
- ✅ убран `wpcf7submit` из success (ложный успех на CF7)

---

## 4. Новые рекомендации (по стадиям)

### 4.1. Anti-bot / Stealth — 🔴 P0

**Источник:** `auto-contact-form-bot` (STEALTH_JS), библиотеки
`playwright-stealth`. Часть твоих потерь «DOM не изменился / 0 POST»
может быть **тихой блокировкой антиботом**, а не багом submit — сервер
видит `navigator.webdriver=true` и молча роняет заявку.

**Что сделать** — `browser_utils.py`, в старте контекста:

1. `context.add_init_script(STEALTH_JS)` ДО любой навигации:
   - `navigator.webdriver → undefined`
   - фейковые `plugins`, `languages`, `window.chrome.runtime`
   - патч `navigator.permissions.query`
   - консистентный `platform`
2. Реалистичный контекст: живой `user_agent`, `locale="ru-RU"`,
   `timezone_id="Europe/Moscow"`, слегка рандомный `viewport`.
3. Опционально — пакет `tf-playwright-stealth` (поддерживаемый форк).

**Эффект:** высокий для сайтов за Cloudflare/антиботом; напрямую бьёт
по кластеру «0 POST».

---

### 4.2. Поиск контакт-страницы — 🟠 P1

**Источник:** `auto-contact-form-bot` (common paths + фолбэк по ссылкам).
Сейчас form-bomber работает по заданному URL; если формы на нём нет —
промах (`esteticart`). Люди часто дают URL услуги, а форма — на /contacts.

**Что сделать** — `form_finder.py`, если форма не найдена на целевой
странице, перед сдачей:
1. пробежать типовые пути: `/kontakty`, `/contacts`, `/contact`,
   `/zapis`, `/zayavka`, `/onlajn-zapis`, `/about/contacts` (RU + EN);
2. фолбэк — собрать `<a>`, чьи текст/href содержат
   `контакт|запис|заявк|contact|callback`, перейти по первой;
3. на найденной странице повторить обычный `extract_forms`.

**Эффект:** средний — спасает случаи «дали не ту страницу».

---

### 4.3. Классификация ТИПА формы — 🟠 P1

**Источник:** `Formasaurus` (login/search/registration/password/
join-mailing/**contact**/order/other). Риск: заполнить и «отправить»
не ту форму — поиск по сайту, подписку на рассылку, логин.

**Что сделать** — `form_finder.py`/`js_extractor.py`, добавить лёгкий
скоринг типа формы перед выбором цели:
- **минус-сигналы** (пропускать): `role=search`, `type=search`,
  единственное поле + кнопка «Найти»; поля `password`/`login`;
  одно `email` + кнопка «Подписаться»/«Subscribe» (newsletter).
- **плюс-сигналы** (приоритет): наличие phone-поля; кнопка
  «заказать звонок/записаться/оставить заявку»; textarea + name.

Частично это уже есть в `finalize` (отсев login/newsletter/search) —
усилить до явного скоринга и логировать выбранный тип.

**Эффект:** средний — убирает ложные «успехи» на чужих формах.

---

### 4.4. Сохранять все hidden/CSRF-поля — 🟠 P1

**Источник:** `python-crawler` (`CrawlerForm.data` = все input'ы),
Scrapy `FormRequest.from_response`. Принцип: **submit несёт ВСЕ
существующие поля** (hidden, `_token`, `csrf`, `action`, дефолты
select/radio), а ты переопределяешь только свои.

**Что сделать:**
- В браузерном пути (`form_filler.py`) при **штатном** submit формы
  hidden-поля уходят автоматически — проверить, что нигде submit не
  пересобирается «руками» из одного phone+name (тогда токен теряется).
- В `calltouch.py` / любых прямых `POST`-фолбэках — перед отправкой
  считывать все `input[type=hidden]` формы и класть в payload.
- Логировать наличие `csrf|token|_token|nonce` в форме, чтобы видеть
  сайты, где это критично.

**Эффект:** средний — чинит «тихие» отказы на Laravel/Django/Bitrix,
где без токена сервер молча отбрасывает POST.

---

### 4.5. char n-gram фичи + `title`/`autocomplete` — 🟡 P2

**Источник:** `Formasaurus` (фичи из tag/name/value/help/id, **char
n-grams**), Firefox Autofill (`autocomplete` важнее эвристики).

**Что сделать** — `js_extractor.py`:
1. Добавить **`autocomplete`** в приоритет распознавания роли
   (`autocomplete="tel"|"name"|"email"` — сильнее любого regex).
   Firefox/Chrome считают его главным сигналом.
2. Дополнить матч подстроками/n-grams: `nam`, `fio`, `tel`, `phon`,
   `mail` ловят слипшиеся/сокращённые атрибуты (`clientname`,
   `usrtel2`), которые точный `\bname\b` пропускает.
3. Учитывать текст связанного `<label for>` и `aria-label` (частично
   уже есть — расширить).

**Эффект:** низкий-средний — добирает нестандартно названные поля.

---

### 4.6. Полная сериализация input-типов — 🟡 P2

**Источник:** `MechanicalSoup` (`form.py`: корректная обработка
`select`, `radio`, `checkbox`, `textarea`, multiple).

**Что сделать** — `form_filler.py`, страховка `fill_all_empty_fields`:
- `select` без выбранного `option` → выбрать первый непустой
  (город/услуга часто обязательны);
- группы `radio` (например «как связаться») → выбрать первый;
- `input[type=number]`/`date` без значения, но `[required]` → заполнить.

**Эффект:** низкий-средний — редкие, но глухие блокировки сложных форм.

---

### 4.7. Скриншот + DOM в AI-промпт (Skyvern-подход) — 🟡 P2

**Источник:** `Skyvern`, `browser-use` (vision+LLM вместо селекторов).
Не переходить на Skyvern (лицензия AGPL-3.0, дорого/медленно), а
**усилить свой третий уровень**.

**Что сделать** — `ai_provider.py`/`runner.py`, когда AI-fallback
активен: слать модели не только очищенный HTML, но и **скриншот**
страницы (`page.screenshot`) — Claude/DeepSeek с vision точнее находят
поля на «непонятных» вёрстках, где эвристика и HTML не помогли.

**Эффект:** средний на «трудных» сайтах — но только после того, как
оживёт AI-ключ (см. ниже).

---

## 5. Инфраструктура (не код-логика, но блокирует)

- 🔴 **AI-ключ.** Прокси `oneprovider.dev` отдаёт `403` на 100% сайтов —
  весь AI-уровень мёртв. Код уже читает `.env`; впиши рабочий
  `CLAUDE_API_KEY`/`ANTHROPIC_API_KEY` (или прямой
  `CLAUDE_URL=https://api.anthropic.com/v1/messages`) либо
  `DEEPSEEK_API_KEY`. Без этого п. 4.7 и весь fallback бесполезны.
- 🟡 **Прокси/ротация IP** (как в ScrapingBee/Skyvern) — при массовой
  рассылке один IP быстро упирается в rate-limit/бан. У тебя уже есть
  прокси в очереди клиентов — убедиться, что stealth + прокси работают
  вместе.

---

## 6. Что НЕ брать

- **Skyvern/browser-use целиком** — AGPL-3.0 (вирусная лицензия) +
  стоимость LLM на каждую страницу. Брать идею (скриншот в промпт),
  не фреймворк.
- **Blackhat-спам-тулы** (XRumer-класс) — юридически и этически
  неприемлемо, здесь не рассматриваются.
- **Переписывание эвристики полей** — ты уже на уровне Chromium.

---

## 7. Итоговый приоритет (эффект / усилие)

| # | Рекомендация | Приоритет | Файл |
|---|---|---|---|
| 1 | Stealth-режим + реалистичный контекст | 🔴 P0 | `browser_utils.py` |
| 2 | Рабочий AI-ключ в `.env` | 🔴 P0 | `.env` |
| 3 | Поиск контакт-страницы при промахе | 🟠 P1 | `form_finder.py` |
| 4 | Классификация типа формы | 🟠 P1 | `form_finder.py` |
| 5 | Сохранять hidden/CSRF в прямых POST | 🟠 P1 | `form_filler.py`, `calltouch.py` |
| 6 | `autocomplete` + n-gram фичи | 🟡 P2 | `js_extractor.py` |
| 7 | Сериализация select/radio/checkbox | 🟡 P2 | `form_filler.py` |
| 8 | Скриншот в AI-промпт | 🟡 P2 | `ai_provider.py` |

**Самый большой быстрый рычаг: п.1 (stealth) + п.2 (AI-ключ).**
Первый бьёт по кластеру «0 POST / DOM не изменился», второй оживляет
целый резервный уровень для трудных сайтов.
