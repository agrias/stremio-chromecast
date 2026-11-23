FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY stremio_cast ./stremio_cast
RUN uv sync --frozen --no-dev

ENV CACHE_DIR=/cache PORT=8766 HOST=0.0.0.0 MALLOC_ARENA_MAX=2 MALLOC_MMAP_THRESHOLD_=131072
EXPOSE 8766
CMD ["/app/.venv/bin/stremio-cast"]
