"""gunicorn entrypoint: `gunicorn birdframe.wsgi:app`."""

from __future__ import annotations

from .app import configure_logging, create_app

configure_logging()
app = create_app()

__all__ = ["app"]
