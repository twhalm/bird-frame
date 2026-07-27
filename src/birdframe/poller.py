"""Polling BirdNET-Go for detections.

The poller exists because BirdNET-Go's webhook only fires for *new* species: see
internal/notification/detection_consumer.go, which returns early unless
event.IsNewSpecies(). Webhook-only, the art changes the first time a cardinal
shows up and then basically never again. /api/v2/detections/recent needs no auth
and returns every detection, so the wall stays alive.
"""

from __future__ import annotations

import logging
import threading
import time

import requests

from .config import Settings
from .gallery import Gallery, utcnow_iso
from .payload import extract_rows, parse_poll_row

log = logging.getLogger("birdframe.poller")


class Poller:
    """Background thread that feeds the gallery from BirdNET-Go."""

    def __init__(self, settings: Settings, gallery: Gallery) -> None:
        self.settings = settings
        self.gallery = gallery
        # An Event rather than time.sleep: sleep is not interruptible, so a
        # SIGTERM during a 60s nap used to mean a 60s wait for shutdown.
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._session = requests.Session()

    def start(self) -> None:
        if not self.settings.polling_enabled:
            log.info("BIRDNET_URL not set - webhook-only mode")
            return
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self.run, name="poller", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        self._session.close()

    def run(self) -> None:
        log.info(
            "poller started: %s every %ds",
            self.settings.recent_url,
            self.settings.poll_seconds,
        )
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self.settings.poll_seconds)
        log.info("poller stopped")

    def poll_once(self) -> int:
        """One cycle. Returns how many detections were put on the wall."""
        health = self.gallery.poll_health
        try:
            response = self._session.get(
                self.settings.recent_url,
                params={"limit": self.settings.poll_limit},
                timeout=15,
            )
            response.raise_for_status()
            rows = extract_rows(response.json())
        except Exception as exc:
            health.consecutive_failures += 1
            health.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("poll failed (%d in a row): %s", health.consecutive_failures, exc)
            return 0

        health.last_ok = time.monotonic()
        health.consecutive_failures = 0
        health.last_error = None

        shown = 0
        # Oldest first, so the newest detection ends up at the front of the gallery.
        for row in reversed(rows):
            parsed = parse_poll_row(row)
            if not parsed.has_species:
                continue
            when = parsed.when or utcnow_iso()
            key = self.gallery.dedupe_key(parsed.scientific, parsed.common, when)
            if self.gallery.seen_before(key):
                continue
            result = self.gallery.record(
                parsed.scientific, parsed.common, parsed.confidence, when, "poll"
            )
            shown += int(result.displayed)
        return shown
