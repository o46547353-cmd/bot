FROM python:3.12-slim

RUN apt-get update && apt-get install -y curl wget && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

RUN mkdir -p /app/images /app/logs

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8000/ || exit 1

CMD ["sh", "-c", "\
    mkdir -p /app/images && \
    uvicorn web_app.main:app --host 0.0.0.0 --port 8000 --log-level info & \
    while true; do \
        python bot.py 2>&1; \
        echo '[RESTART] bot.py упал, перезапуск через 5 сек...'; \
        sleep 5; \
    done"]
