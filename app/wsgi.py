"""gunicorn entrypoint.

`python server.py` starts the poller in its __main__ block; under gunicorn that
block never runs, so start it here instead.
"""

import threading

from plates import CACHE_DIR
from server import BIRDNET_URL, _load_history, _poll_loop, app, log

CACHE_DIR.mkdir(parents=True, exist_ok=True)
_load_history()

if BIRDNET_URL:
    threading.Thread(target=_poll_loop, daemon=True).start()
else:
    log.info("BIRDNET_URL not set - webhook-only mode")

__all__ = ["app"]
