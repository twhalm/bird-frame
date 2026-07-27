"""Local development server: `python -m birdframe`.

Production runs under gunicorn (see wsgi.py); this exists so you can restyle the
frame without building a container.
"""

from __future__ import annotations

from .app import configure_logging, create_app
from .config import Settings


def main() -> None:
    configure_logging()
    settings = Settings.from_env()
    app = create_app(settings)
    # Binding to all interfaces is the point: the TV is another machine.
    app.run(host="0.0.0.0", port=settings.port, threaded=True)  # noqa: S104


if __name__ == "__main__":
    main()
