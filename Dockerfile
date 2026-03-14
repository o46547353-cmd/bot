FROM python:3.12-slim

RUN apt-get update && apt-get install -y curl wget gcc && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

# Шаг 1: обновляем pip
RUN pip install --no-cache-dir --upgrade pip

# Шаг 2: h11 нужной версии сначала
RUN pip install --no-cache-dir h11==0.14.0

# Шаг 3: все зависимости (включая apscheduler, threads-api, aiohttp)
RUN pip install --no-cache-dir -r requirements.txt

# Шаг 4: metathreads без его зависимостей (они уже стоят выше, нужные версии)
RUN pip install --no-cache-dir --no-deps metathreads

RUN mkdir -p /app/images /app/logs

# BUG-14 FIX: убрали uvicorn web_app.main:app — модуль не существует
# BUG-15 FIX: healthcheck проверяет сам процесс python, а не HTTP-порт
# Бот работает как pure Telegram bot без web-сервера

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD pgrep -f "python bot.py" || exit 1

CMD ["sh", "-c", "mkdir -p /app/images /app/logs && while true; do python bot.py 2>&1 | tee -a /app/logs/bot.log; echo '[RESTART] через 5 сек...'; sleep 5; done"]
