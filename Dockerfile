# Plate images are NOT baked in: they are fetched on first sighting of each
# species and cached in the /cache volume, so the image stays small and disk only
# grows with birds you actually get.

# --------------------------------------------------------------------- build
FROM ghcr.io/astral-sh/uv:0.9-python3.13-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, in their own layer: they change far less often than the
# source, so editing plates.py does not re-resolve the lockfile.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev

COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/

# --no-editable matters: the default editable install leaves a .pth pointing at
# /app/src, which does not exist in the runtime stage that only copies the venv.
# This puts birdframe (and its data/ and web/) inside site-packages instead.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# ------------------------------------------------------------------- runtime
FROM python:3.13-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    PORT=8080

# Non-root, and it owns the cache volume mountpoint so the lazy plate downloads
# can actually write there.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin birdframe \
 && mkdir -p /cache/plates \
 && chown -R birdframe:birdframe /cache

WORKDIR /app
COPY --from=builder --chown=birdframe:birdframe /app/.venv /app/.venv

# The package carries its own data/ and web/ directories, resolved relative to
# the installed module -- no repo-layout symlinks and no CWD dependence.
USER birdframe

VOLUME ["/cache"]
EXPOSE 8080

# /healthz returns 503 when the poller has been failing for three cycles, so an
# unreachable BirdNET-Go marks the container unhealthy instead of going unnoticed.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"]

# One gunicorn worker on purpose: the rotation and the poller live in process
# memory, so multiple workers would each hold a different gallery. Threads handle
# the concurrent image requests fine.
CMD ["gunicorn", \
     "--bind", "0.0.0.0:8080", \
     "--workers", "1", \
     "--threads", "8", \
     "--graceful-timeout", "10", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "birdframe.wsgi:app"]
