FROM python:3.12-slim

RUN apt-get update && apt-get install -y curl wget gcc && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

# Шаг 1: основные зависимости
RUN pip install --no-cache-dir --upgrade pip

# Шаг 2: сначала h11 нужной версии (metathreads требует 0.14.0)
RUN pip install --no-cache-dir h11==0.14.0

# Шаг 3: остальные зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Шаг 4: metathreads без его зависимостей (они уже стоят выше)
RUN pip install --no-cache-dir --no-deps metathreads

RUN mkdir -p /app/images /app/logs

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8000/ || exit 1

CMD ["sh", "-c", "mkdir -p /app/images && uvicorn web_app.main:app --host 0.0.0.0 --port 8000 --log-level info & while true; do python bot.py 2>&1; echo '[RESTART] через 5 сек...'; sleep 5; done"]
