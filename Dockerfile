# Stage 1: Builder
FROM python:3.11-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libffi-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip \
 && pip install --prefix=/install --no-cache-dir -r requirements.txt

# Stage 2: Runtime
FROM python:3.11-slim AS runtime

WORKDIR /app

COPY --from=builder /install /usr/local

RUN addgroup --system saarthi && adduser --system --ingroup saarthi saarthi

COPY --chown=saarthi:saarthi . .

RUN rm -f serviceAccountKey.json .env

USER saarthi

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# PORT Railway automatically inject karta hai
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --loop uvloop --http httptools --proxy-headers"]
