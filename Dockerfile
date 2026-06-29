# ---- builder: compile wheels with the build toolchain (kept OUT of the runtime image) ----
FROM python:3.12-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# Build (or download) wheels for every dependency so the runtime stage can install
# them with --no-index (no compiler / -dev headers needed at runtime).
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

# ---- runtime: slim image, no gcc/libpq-dev, runs as a non-root user ----
FROM python:3.12-slim AS runtime

WORKDIR /app

# Non-root system user that owns the app and the writable uploads dir.
RUN adduser --system --group app

COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

COPY . .

# Ensure uploads exists and the whole app tree is owned by the non-root user.
RUN mkdir -p /app/uploads && chown -R app:app /app

USER app

EXPOSE 8000

# --proxy-headers + --forwarded-allow-ips=* let uvicorn trust nginx's X-Forwarded-For
# so request.client.host is the REAL client IP (needed for per-IP login throttling).
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4", "--proxy-headers", "--forwarded-allow-ips=*"]
