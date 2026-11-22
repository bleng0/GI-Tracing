# Local dashboard image. Build and start with `docker compose up --build`.
# Dependencies are installed in a builder stage so the running image carries only
# the packages, not pip's build cache.
FROM python:3.12-slim AS builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GI_DOCKER=1 \
    GI_HOST=0.0.0.0 \
    GI_PORT=5000
WORKDIR /app
COPY --from=builder /install /usr/local
COPY gi_trace/ ./gi_trace/
# The dashboard binds 0.0.0.0 inside the container; Compose publishes it on
# 127.0.0.1 of the host, so it stays reachable from this machine only.
EXPOSE 5000
CMD ["python", "-m", "gi_trace.web"]
