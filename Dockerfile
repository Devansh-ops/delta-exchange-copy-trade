# syntax=docker/dockerfile:1.7

ARG PYTHON_VERSION=3.12

# =========================
# Builder: install deps via uv into SYSTEM Python
# =========================
FROM python:${PYTHON_VERSION}-slim AS builder
ARG TARGETPLATFORM

ENV UV_SYSTEM_PYTHON=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install uv (no compilers). Cache only /var/cache/apt; avoid /var/lib/apt to prevent lock issues.
RUN --mount=type=cache,id=apt-cache-${TARGETPLATFORM},target=/var/cache/apt,sharing=locked \
    apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -LsSf https://astral.sh/uv/install.sh | sh \
 && ln -sf /root/.local/bin/uv /usr/local/bin/uv \
 && rm -rf /var/lib/apt/lists/*

# Copy lockfiles first for cache hits
COPY --link pyproject.toml uv.lock ./

# Install only prod deps into the system interpreter (no venv duplication)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --upgrade pip \
 && uv sync --frozen --no-dev

# =========================
# Runtime: tiny & fast
# =========================
FROM python:${PYTHON_VERSION}-slim AS runtime

# Fast, quiet Python; no bytecode writes at runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONOPTIMIZE=2 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Copy only the installed packages and console scripts
# (Adjust path if you change the Python minor version.)
COPY --link --from=builder /usr/local/lib/python3.12 /usr/local/lib/python3.12
COPY --link --from=builder /usr/local/bin /usr/local/bin

# App code (ensure .dockerignore excludes logs/, __pycache__/, *.egg-info/, etc.)
COPY --link . .

# Precompile for faster cold starts (strips docstrings with -OO)
RUN python -OO -m compileall -q -j 0 /app

# Healthcheck without procps: verifies PID 1 is python main.py
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import sys,pathlib; cmd=pathlib.Path('/proc/1/cmdline').read_bytes(); sys.exit(0 if (b'python' in cmd and b'main.py' in cmd) else 1)"]

CMD ["python", "main.py"]
