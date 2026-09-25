# M1 recorder image. Secrets come from .env at runtime (docker compose env_file), never baked in.
FROM python:3.12-slim

# MALLOC_ARENA_MAX: glibc otherwise opens up to 8 malloc arenas per core for the threads
# (Parquet writes run in a thread pool); fewer arenas = less fragmentation and lower RSS.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MALLOC_ARENA_MAX=2 \
    DATA_DIR=/app/data \
    CONFIG_DIR=/app/config

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && pip install .

COPY config ./config

RUN useradd --create-home --uid 10001 polybot && mkdir -p /app/data && chown polybot /app/data
USER polybot

HEALTHCHECK --interval=60s --timeout=10s --start-period=180s --retries=3 \
    CMD ["polybot", "health", "--max-age", "180"]

ENTRYPOINT ["polybot"]
CMD ["record"]
