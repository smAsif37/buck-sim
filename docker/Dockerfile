# API + worker image using ngspice (open source, redistributable).
# Build:  docker build -f docker/Dockerfile -t buck-sim:local .
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ngspice tini \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data \
    && chown app:app /data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[ai]"

USER app
ENV DATA_DIR=/data \
    ENGINE=ngspice \
    HOST=0.0.0.0 \
    PORT=8000 \
    LOG_FORMAT=json
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"

ENTRYPOINT ["tini", "--"]
CMD ["buck-sim", "serve"]
