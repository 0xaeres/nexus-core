# syntax=docker/dockerfile:1.7

# ---------- builder ----------
FROM python:3.13-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

# Install uv
ADD https://astral.sh/uv/install.sh /tmp/uv-install.sh
RUN sh /tmp/uv-install.sh && cp /root/.local/bin/uv /usr/local/bin/uv

WORKDIR /app

# Resolve deps first (cache layer)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# Now install the package itself
COPY nexus ./nexus
COPY evals ./evals
COPY nexus.yaml.example .env.example ./
RUN uv sync --frozen

# ---------- runtime ----------
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}"

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /app /app

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
    CMD curl -sf http://localhost:8000/health || exit 1

CMD ["uvicorn", "nexus.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
