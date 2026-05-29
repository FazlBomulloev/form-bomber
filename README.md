# Form Bomber

Веб-сервис на FastAPI для автоматизированной проверки и заполнения форм на
сайтах. Под капотом — Playwright (headless Chromium), который открывает
страницы, находит формы обратной связи, заполняет их и фиксирует результат.
Интерфейс и API закрыты JWT-авторизацией, данные сессий и профили форм
хранятся в локальной SQLite-базе.

Ниже — полный путь от голого сервера Ubuntu до работающего контейнера.
Читается сверху вниз, ничего пропускать не нужно.

---

## Что внутри

- **FastAPI + uvicorn** — веб-сервер, слушает порт **8002**.
- **Playwright (Chromium)** — управляет браузером, заполняет формы.
- **SQLite** (`data/checker_ai.db`) — сессии и результаты.
- **JWT-авторизация** — вход по логину/паролю из `.env`.
- Веб-панель доступна на `/`, страница входа — на `/login`.

Структура репозитория:

```
form-bomber/
├── src/                  # исходный код приложения
│   ├── app.py            # точка входа (uvicorn)
│   ├── auth.py           # логин/пароль и JWT
│   ├── config.py         # порт, пути, селекторы
│   ├── runner.py         # оркестрация сессий
│   ├── form_*.py         # поиск и заполнение форм
│   └── static/           # HTML интерфейса
├── Dockerfile            # сборка образа
├── docker-compose.yml    # запуск одной командой
├── requirements.txt      # Python-зависимости
├── .env.example          # шаблон переменных окружения
└── README.md             # этот файл
```

---

## 1. Установка Docker на Ubuntu

На чистом сервере Ubuntu Docker по умолчанию **не установлен**. Проверить:

```bash
docker --version
docker compose version
```

Если команды не найдены — ставим Docker по официальной инструкции:

```bash
# 1. зависимости
sudo apt update
sudo apt install -y ca-certificates curl gnupg

# 2. GPG-ключ репозитория Docker
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
  sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

# 3. подключение репозитория Docker
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# 4. установка движка и плагина compose
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin

# 5. запускать docker без sudo
sudo usermod -aG docker $USER
newgrp docker   # либо перелогиньтесь в сессию
```

Проверяем, что всё работает:

```bash
docker run hello-world
```

> Альтернатива одной командой (официальный скрипт, удобно для одноразовой
> настройки сервера): `curl -fsSL https://get.docker.com | sudo sh`

---

## 2. Клонирование проекта

Понадобится **git**:

```bash
sudo apt update
sudo apt install -y git
```

Клонируем репозиторий по HTTPS и заходим в каталог проекта:

```bash
git clone https://github.com/FazlBomulloev/form-bomber.git
cd form-bomber
```

---

## 3. Настройка переменных окружения (`.env`)

Секреты в репозиторий не попадают (`.env` в `.gitignore`). В проекте лежит
шаблон `.env.example` — скопируйте его и отредактируйте.

```bash
cp .env.example .env
```

Откройте `.env` в редакторе (`nano .env`) и заполните три переменные:

| Переменная      | Назначение                                        | Что поставить                                  |
|-----------------|---------------------------------------------------|------------------------------------------------|
| `AUTH_LOGIN`    | Логин для входа в веб-панель                       | свой логин (не оставляйте `admin`)             |
| `AUTH_PASSWORD` | Пароль для входа в веб-панель                       | надёжный пароль                                |
| `JWT_SECRET`    | Секретный ключ для подписи токенов авторизации      | длинная случайная строка                       |

Сгенерировать надёжный `JWT_SECRET` можно так:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Скопируйте полученную строку в `JWT_SECRET`. Пример заполненного `.env`:

```env
AUTH_LOGIN=myadmin
AUTH_PASSWORD=S0me-Strong-Pass!
JWT_SECRET=8f3c2a9b7d1e4f6a0c5b8e2d7a1f9c3b6e4d8a2f0c7b5e9d1a3f6c8b2e4d7a09
```

> ⚠️ Без корректного `.env` приложение поднимется со значениями по умолчанию
> (`admin` / `admin`), что небезопасно. Меняйте обязательно.

---

## 4. Запуск через Docker

### Вариант А — Docker Compose (рекомендуется)

Из корня проекта:

```bash
docker compose up -d --build
```

Что происходит:

- `--build` — собирает образ из `Dockerfile` (первый раз дольше: ставятся
  зависимости и Chromium со всеми системными библиотеками);
- `-d` — запускает контейнер в фоне;
- порт `8002` пробрасывается на хост;
- каталог `./data` монтируется в контейнер, поэтому база и профили
  **сохраняются** между перезапусками.

Открываем в браузере: **http://СЕРВЕР:8002** (или `http://localhost:8002`,
если запускаете локально) — появится страница входа. Логиньтесь логином и
паролем из `.env`.

Полезные команды:

```bash
docker compose logs -f        # смотреть логи в реальном времени
docker compose restart        # перезапустить
docker compose down           # остановить и удалить контейнер (данные в ./data остаются)
docker compose up -d --build  # пересобрать после изменений в коде
```

### Вариант Б — чистый Docker (без compose)

```bash
# сборка образа
docker build -t form-bomber:latest .

# запуск контейнера
docker run -d \
  --name form-bomber \
  --restart unless-stopped \
  -p 8002:8002 \
  --env-file .env \
  -v "$(pwd)/data:/app/data" \
  form-bomber:latest
```

Управление:

```bash
docker logs -f form-bomber    # логи
docker stop form-bomber       # остановить
docker start form-bomber      # запустить снова
docker rm -f form-bomber      # удалить контейнер
```

---

## 5. Доступ снаружи (firewall)

Если сервер за фаерволом (ufw), откройте порт `8002`:

```bash
sudo ufw allow 8002/tcp
```

После этого панель будет доступна по адресу `http://IP-СЕРВЕРА:8002`.

---

## 6. Проверка, что всё работает

1. Откройте `http://IP-СЕРВЕРА:8002` — должна открыться страница входа.
2. Войдите логином/паролем из `.env`.
3. После входа откроется основная панель — можно добавлять URL и запускать
   проверку форм.

Если страница не открывается:

- убедитесь, что контейнер запущен: `docker compose ps`;
- посмотрите логи: `docker compose logs -f`;
- проверьте, что порт `8002` открыт в фаерволе и не занят другим процессом.

---

## Настройки и порт

Базовые параметры заданы в `src/config.py`:

- `PORT = 8002` — порт сервера;
- `DB_PATH = "data/checker_ai.db"` — путь к базе;
- `PROFILES_PATH = "data/ai_profiles.json"` — кеш профилей форм;
- `CONCURRENCY`, `AI_CONCURRENCY` — параллелизм обработки.

Чтобы сменить внешний порт, не трогая код, поправьте маппинг в
`docker-compose.yml`, например `"9000:8002"` — тогда панель будет на
`http://IP-СЕРВЕРА:9000`.

---

## Обновление проекта

После изменений в репозитории:

```bash
git pull
docker compose up -d --build
```

---

## Частые вопросы

**Где хранятся данные?** В каталоге `data/` (он же том контейнера). Удалять
контейнер можно безопасно — база и профили останутся.

**Забыл пароль от панели.** Пароль — это `AUTH_PASSWORD` в `.env`. Поменяйте
значение и перезапустите контейнер (`docker compose up -d`).

**Долгая первая сборка.** Это нормально: Playwright тянет Chromium и системные
зависимости. Повторные сборки используют кеш слоёв и идут быстрее.
