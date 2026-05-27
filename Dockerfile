FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ---- builder ----------------------------------------------------------------
FROM base AS builder

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir --upgrade pip build && \
    pip wheel --no-cache-dir --wheel-dir /wheels .

# ---- runtime ----------------------------------------------------------------
FROM base AS runtime

# Non-root user for Unraid friendliness.
# Unraid's nobody:users defaults are 99:100. On Debian slim GID 100 (users)
# already exists, so reuse it when present; UID 99 is free.
ARG PUID=99
ARG PGID=100
RUN if ! getent group ${PGID} >/dev/null; then groupadd -g ${PGID} app; fi && \
    useradd -u ${PUID} -g ${PGID} -m -s /sbin/nologin app && \
    mkdir -p /config && \
    chown -R ${PUID}:${PGID} /config

WORKDIR /app

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

COPY --chown=app:app src ./src

USER app

ENV PB2C_CONFIG_DIR=/config \
    PB2C_WEB_HOST=0.0.0.0 \
    PB2C_WEB_PORT=8080

VOLUME ["/config"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status==200 else 1)"

CMD ["python", "-m", "pb2craft.main"]
