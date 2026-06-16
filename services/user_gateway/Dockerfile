FROM python:3.11-slim AS builder
WORKDIR /build
COPY services/user_gateway/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.11-slim
RUN useradd -m -u 10001 appuser
WORKDIR /app
COPY --from=builder /install /usr/local
COPY platform_common ./platform_common
COPY services/user_gateway/app ./app
COPY services ./services
COPY data ./data
RUN mkdir -p /app/.data/operator_sessions && chown -R appuser:appuser /app
USER appuser
EXPOSE 8005
HEALTHCHECK --interval=15s --timeout=3s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8005/health/live')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8005"]
