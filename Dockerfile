# syntax=docker/dockerfile:1

# Form Bomber — FastAPI + Playwright (Chromium)
# Базовый образ с Python 3.11 (нужен синтаксис `str | None`).
FROM python:3.11-slim

# Не пишем .pyc, логи сразу в stdout (важно для docker logs).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Сначала зависимости — слой переиспользуется при пересборке, пока
# requirements.txt не изменился.
COPY requirements.txt .
RUN python -m pip install --upgrade pip \
    && pip install -r requirements.txt

# Ставим Chromium вместе со всеми системными библиотеками, которые
# нужны браузеру (--with-deps подтягивает их через apt).
RUN playwright install --with-deps chromium

# Исходники приложения.
COPY src/ ./src/

# Каталог для SQLite-базы и профилей (config.py пишет в ./data).
# Его удобно прокидывать томом, чтобы данные переживали пересоздание
# контейнера.
RUN mkdir -p /app/data
VOLUME ["/app/data"]

# Порт из config.py (PORT = 8002).
EXPOSE 8002

# app.py поднимает uvicorn на 0.0.0.0:8002.
# Запуск из /app, чтобы относительный путь ./data резолвился верно.
CMD ["python", "src/app.py"]
