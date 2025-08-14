# syntax=docker/dockerfile:1.7

ARG PYTHON_VERSION=3.12

############################
# Builder: resolve & install deps into SYSTEM Python
############################
FROM python:${PYTHON_VERSION}-slim AS builder
ARG TARGETPLATFORM

ENV UV_SYSTEM_PYTHON=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install uv (no compilers). Cache only /var/cache/apt to avoid apt lock issues.
RUN --mount=type=cache,id=apt-cache-${TARGETPLATFORM},target=/var/cache/apt,sharing=locked \
    apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -LsSf https://astral.sh/uv/install.sh | sh \
 && ln -sf /root/.local/bin/uv /usr/local/bin/uv \
 && rm -rf /var/lib/apt/lists/*

# Lock files first = best cache hits
COPY --link pyproject.toml uv.lock ./

# Export a pinned requirements.txt from the lock and install into SYSTEM site-packages
RUN --mount=type=cache,target=/root/.cache/uv \
    uv export --frozen --no-dev > /tmp/requirements.txt \
 && python -m pip install --no-cache-dir -r /tmp/requirements.txt

############################
# Runtime: tiny & fast
############################
FROM python:${PYTHON_VERSION}-slim AS runtime
ARG PYTHON_VERSION

# Fast, quiet Python; no bytecode writes at runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONOPTIMIZE=2 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Copy only system site-packages and console scripts from builder
COPY --link --from=builder /usr/local/lib/python${PYTHON_VERSION} /usr/local/lib/python${PYTHON_VERSION}
COPY --link --from=builder /usr/local/bin /usr/local/bin

# App code (ensure .dockerignore excludes logs/, __pycache__/, *.egg-info/, etc.)
COPY --link . .

# Precompile for faster cold starts (strips docstrings with -OO)
RUN python -OO -m compileall -q -j 0 /app

# Linter-safe exec-form healthcheck (no procps needed)
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import sys,pathlib; cmd=pathlib.Path('/proc/1/cmdline').read_bytes(); sys.exit(0 if (b'python' in cmd and b'main.py' in cmd) else 1)"]

CMD ["python", "main.py"]
