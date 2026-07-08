FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY seatbot ./seatbot
COPY config.example.yaml ./

RUN pip install --upgrade pip && pip install -e .

RUN playwright install --with-deps chromium || true

EXPOSE 8080
VOLUME ["/app/data", "/app/logs"]
CMD ["python", "-m", "seatbot", "run", "--config", "/app/data/config.yaml"]
