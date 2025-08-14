# syntax=docker/dockerfile:1.7

ARG PYTHON_VERSION=3.12

# =========================
# Builder: install deps via uv into SYSTEM Python
# =========================
FROM python:${PYTHON_VERSION}-slim AS builder

ENV UV_SYSTEM_PYTHON=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install uv (no compilers), cache apt for speed only in this stage
RUN --mount=type=cache,target=/var/cache/apt \
    --mount=type=cache,target=/var/lib/apt \
    apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -LsSf https://astral.sh/uv/install.sh | sh \
 && ln -sf /root/.local/bin/uv /usr/local/bin/uv \
 && rm -rf /var/lib/apt/lists/*

# Copy lockfiles first for maximum cache hits
COPY --link pyproject.toml uv.lock ./

# Install only prod deps into the system interpreter
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --upgrade pip \
 && uv sync --frozen --no-dev

# =========================
# Runtime: tiny, fast
# =========================
FROM python:${PYTHON_VERSION}-slim AS runtime

# Fast, quiet Python; no bytecode writes at runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONOPTIMIZE=2 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Copy only what we need from builder:
# - site-packages and dist-info (libs)
# - console scripts installed into /usr/local/bin
# NOTE: Adjust the 3.12 if you change PYTHON_VERSION major.minor.
COPY --link --from=builder /usr/local/lib/python3.12 /usr/local/lib/python3.12
COPY --link --from=builder /usr/local/bin /usr/local/bin

# App code (make sure .dockerignore excludes logs, __pycache__, *.egg-info, etc.)
COPY --link . .

# Precompile for faster cold start (strips docstrings with -OO)
RUN python -OO -m compileall -q -j 0 /app

# Healthcheck without procps: verifies PID 1 is python main.py (no pgrep needed)
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import sys, pathlib; cmd=pathlib.Path('/proc/1/cmdline').read_bytes(); sys.exit(0 if (b'python' in cmd and b'main.py' in cmd) else 1)"]

CMD ["python", "main.py"]
