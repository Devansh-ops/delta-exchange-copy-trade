# syntax=docker/dockerfile:1.7

ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_SYSTEM_PYTHON=1 \
    VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:${PATH}"

# Minimal OS deps (curl only for uv install); purge afterwards to keep image small
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ---- install uv & deps using lockfile (best cache hit) ----
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
 && ln -sf /root/.local/bin/uv /usr/local/bin/uv \
 && uv --version

# Copy just dependency files first for better caching
COPY pyproject.toml uv.lock ./

# Create in-project venv and install (locked, no dev)
RUN uv venv ${VIRTUAL_ENV} \
 && uv pip install --upgrade pip \
 && uv sync --frozen --no-dev

# ---- app layer ----
# Now copy the rest of the source
COPY . .

# Create non-root user and fix permissions for logs/working dir
RUN useradd -m -u 10001 appuser \
 && mkdir -p /app/logs \
 && chown -R appuser:appuser /app

USER appuser

# (Optional) Switch timezone in container if you want system time to match IST
# ENV TZ=Asia/Kolkata
# RUN sudo ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ | sudo tee /etc/timezone

# Healthcheck: is the process running?
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD pgrep -f "python .*main.py" >/dev/null || exit 1

# Default command
CMD ["python", "main.py"]
