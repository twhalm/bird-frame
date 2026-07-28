# Plate images are NOT baked in: they are fetched on first sighting of each
# species and cached in the /cache volume, so the image stays small and disk only
# grows with birds you actually get.

# --------------------------------------------------------------------- build
FROM ghcr.io/astral-sh/uv:0.9-python3.13-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# samsungtvws comes from a git ref (see pyproject.toml), and uv shells out to
# git to fetch it. Builder stage only - the runtime image never sees it.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

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

# Set by the release workflow from the semantic-release version, so `docker
# inspect` on a running container tells you which release it came from.
ARG BIRDFRAME_VERSION=dev

LABEL org.opencontainers.image.title="BirdFrame" \
      org.opencontainers.image.description="Audubon plates in a Samsung Frame's Art Mode, driven by BirdNET-Go detections" \
      org.opencontainers.image.source="https://github.com/twhalm/bird-frame" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${BIRDFRAME_VERSION}"

ENV BIRDFRAME_VERSION=${BIRDFRAME_VERSION} \
    PYTHONUNBUFFERED=1 \
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

# Shell form so $PORT expands. The healthcheck must follow PORT too, or changing
# it makes the container permanently unhealthy while the app is fine.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz', timeout=4).status == 200 else 1)"

# One gunicorn worker on purpose: the rotation and the poller live in process
# memory, so multiple workers would each hold a different gallery. Threads handle
# the concurrent image requests fine.
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT} --workers 1 --threads 8 --graceful-timeout 10 --access-logfile - --error-logfile - birdframe.wsgi:app"]
