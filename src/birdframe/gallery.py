"""The rotation: what has been heard, what hangs on the wall, and the disk mirror.

All mutable state lives in one ``Gallery`` instance owned by the app, rather than
in module globals, so tests can build a fresh one per case and so it is obvious
what the single gunicorn worker is guarding.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .config import Settings
from .plates import PlateIndex

log = logging.getLogger("birdframe.gallery")

# Remembered detection keys, for poller de-duplication. Generous, since a key is
# ~50 bytes and the poller re-reads the same recent window every cycle.
SEEN_KEYS = 500


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class Stats:
    received: int = 0
    matched: int = 0
    unmatched: int = 0
    rejected: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "received": self.received,
            "matched": self.matched,
            "unmatched": self.unmatched,
            "rejected": self.rejected,
        }


@dataclass(slots=True)
class PollHealth:
    """Whether the poller is actually working.

    Without this, /healthz returns ok while the poller has been failing for a
    week, which makes the container healthcheck blind to the one failure mode
    that takes the wall down.
    """

    enabled: bool = False
    last_ok: float | None = None
    last_error: str | None = None
    consecutive_failures: int = 0

    def as_dict(self, stale_after: float) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "healthy": self.is_healthy(stale_after),
            "seconds_since_success": self.age(),
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
        }

    def age(self) -> float | None:
        if self.last_ok is None:
            return None
        return round(time.monotonic() - self.last_ok, 1)

    def is_healthy(self, stale_after: float) -> bool:
        if not self.enabled:
            return True  # webhook-only mode: nothing to be unhealthy about
        age = self.age()
        if age is None:
            # Never succeeded. Allow one stale window for startup before failing.
            return self.consecutive_failures == 0
        return age <= stale_after


@dataclass(slots=True)
class Detection:
    """One heard bird, with the plate that will represent it."""

    scientific_name: str
    common_name: str
    confidence: float | None
    detected_at: str
    source: str
    plate: int | None = None
    file_name: str | None = None
    audubon_name: str | None = None
    match: str | None = None

    @property
    def displayable(self) -> bool:
        return self.plate is not None

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "scientific_name": self.scientific_name,
            "common_name": self.common_name,
            "confidence": self.confidence,
            "detected_at": self.detected_at,
            "source": self.source,
        }
        if self.plate is not None:
            d |= {
                "plate": self.plate,
                "file_name": self.file_name,
                "audubon_name": self.audubon_name,
                "match": self.match,
            }
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Detection | None:
        """Rebuild from the disk mirror. Returns None for anything malformed."""
        plate = raw.get("plate")
        if not isinstance(plate, int):
            return None
        conf = raw.get("confidence")
        if conf is not None and not isinstance(conf, int | float):
            conf = None
        return cls(
            scientific_name=str(raw.get("scientific_name") or ""),
            common_name=str(raw.get("common_name") or ""),
            confidence=float(conf) if conf is not None else None,
            detected_at=str(raw.get("detected_at") or ""),
            source=str(raw.get("source") or "restored"),
            plate=plate,
            file_name=str(raw.get("file_name") or ""),
            audubon_name=str(raw.get("audubon_name") or ""),
            match=str(raw.get("match") or "curated"),
        )


@dataclass(slots=True)
class RecordResult:
    """Outcome of offering a detection to the gallery."""

    displayed: bool
    reason: str


class Gallery:
    """Detection history, de-duplication, stats and the disk mirror."""

    def __init__(self, settings: Settings, index: PlateIndex) -> None:
        self.settings = settings
        self.index = index
        self._lock = threading.Lock()
        self._history: deque[Detection] = deque(maxlen=settings.history_size)
        self._seen: deque[str] = deque(maxlen=SEEN_KEYS)
        self._seen_set: set[str] = set()
        self._unmatched: deque[str] = deque(maxlen=settings.unmatched_log_size)
        self.stats = Stats()
        self.poll_health = PollHealth(enabled=settings.polling_enabled)
        # Bounded: a poll cycle can yield a dozen new species at once, and an
        # unbounded thread-per-detection spawn is how you get a thread storm.
        self._warmers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="warm")
        # Wired to the art driver by create_app, so a bird that lands mid-nap
        # goes up now rather than at the end of the rotation interval.
        self.on_change: Callable[[], None] | None = None

    # --------------------------------------------------------------- de-dupe

    @staticmethod
    def dedupe_key(scientific: str, common: str, when: str) -> str:
        return f"{scientific or common}@{when}"

    def seen_before(self, key: str) -> bool:
        """Test-and-set: True if this key has already been processed."""
        with self._lock:
            if key in self._seen_set:
                return True
            if len(self._seen) == self._seen.maxlen:
                self._seen_set.discard(self._seen[0])
            self._seen.append(key)
            self._seen_set.add(key)
            return False

    # ---------------------------------------------------------------- record

    def record(
        self,
        scientific: str,
        common: str,
        confidence: float | None,
        when: str,
        source: str,
    ) -> RecordResult:
        """Offer a detection to the gallery.

        ``confidence`` is None when the payload carried none; that is distinct
        from 0.0, which is a real (and very low) reading. Both are compared
        against the threshold -- a missing confidence used to skip the check
        entirely and put unvetted birds on the wall.
        """
        with self._lock:
            self.stats.received += 1

        if confidence is None or confidence < self.settings.min_confidence:
            with self._lock:
                self.stats.rejected += 1
            log.debug(
                "below threshold: %s (%s)",
                common or scientific,
                "no confidence" if confidence is None else f"{confidence:.2f}",
            )
            return RecordResult(False, "below_confidence_threshold")

        match = self.index.resolve(scientific, common)
        if match is None:
            label = scientific or common or "unknown"
            with self._lock:
                self.stats.unmatched += 1
                if label not in self._unmatched:
                    self._unmatched.append(label)
            log.info("no plate for %s / %s", scientific, common)
            return RecordResult(False, "no_plate")

        entry = Detection(
            scientific_name=scientific,
            common_name=common or scientific or "Unknown",
            confidence=round(confidence, 4),
            detected_at=when,
            source=source,
            plate=match.plate,
            file_name=match.file_name,
            audubon_name=match.audubon_name,
            match=match.match,
        )

        with self._lock:
            self._history.appendleft(entry)
            self.stats.matched += 1

        # Warm the cache off the request thread so the webhook returns fast.
        self._warmers.submit(self.index.ensure_cached, match.plate, match.file_name)
        self.save()
        self._notify()

        log.info(
            "%s -> plate %d (%s)", entry.common_name, match.plate, match.audubon_name
        )
        return RecordResult(True, "displayed")

    def _notify(self) -> None:
        """Tell the art driver something changed.

        Deliberately swallowing: a listener that raises must not turn a good
        webhook into a 500, and the driver will pick the change up on its next
        cycle anyway.
        """
        listener = self.on_change
        if listener is None:
            return
        try:
            listener()
        except Exception as exc:
            log.warning("on_change listener raised: %s", exc)

    # ------------------------------------------------------------------ reads

    def snapshot(self, limit: int | None = None) -> list[Detection]:
        """Displayable history, newest first."""
        with self._lock:
            items = [d for d in self._history if d.displayable]
        return items[:limit] if limit else items

    def health(self) -> dict[str, Any]:
        with self._lock:
            history = len(self._history)
            stats = self.stats.as_dict()
            unmatched = list(self._unmatched)
        return {
            "history": history,
            "cached_plates": self.index.cached_count(),
            "stats": stats,
            # Names the species worth adding to curated_map.json, so you do not
            # have to go grepping the logs for them.
            "unmatched_species": unmatched,
            "poller": self.poll_health.as_dict(self.settings.stale_after_seconds),
        }

    def is_healthy(self) -> bool:
        return self.poll_health.is_healthy(self.settings.stale_after_seconds)

    # ------------------------------------------------------------------- disk

    def save(self) -> None:
        path = self.settings.history_file
        try:
            with self._lock:
                items = [d.as_dict() for d in self._history]
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(items), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            log.debug("could not save history: %s", exc)

    def load(self) -> None:
        """Restore the gallery from the cache volume.

        Without this a restart leaves an empty mat on the wall until the next
        bird sings, which may be hours.
        """
        path = self.settings.history_file
        try:
            if not path.exists():
                return
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("could not restore history: %s", exc)
            return

        if not isinstance(raw, list):
            log.warning("history file is not a list; ignoring it")
            return

        restored = 0
        with self._lock:
            for item in reversed(raw[: self.settings.history_size]):
                if not isinstance(item, dict):
                    continue
                entry = Detection.from_dict(item)
                if entry is not None:
                    self._history.appendleft(entry)
                    restored += 1
        if restored:
            log.info("restored %d detections from previous run", restored)

    def shutdown(self) -> None:
        self._warmers.shutdown(wait=False, cancel_futures=True)


@dataclass(slots=True)
class DemoBird:
    scientific: str
    common: str
    confidence: float


DEMO_BIRDS: tuple[DemoBird, ...] = (
    DemoBird("Cardinalis cardinalis", "Northern Cardinal", 0.94),
    DemoBird("Cyanocitta cristata", "Blue Jay", 0.91),
    DemoBird("Colaptes auratus", "Northern Flicker", 0.88),
    DemoBird("Zenaida macroura", "Mourning Dove", 0.86),
    DemoBird("Baeolophus bicolor", "Tufted Titmouse", 0.83),
    DemoBird("Sitta carolinensis", "White-breasted Nuthatch", 0.81),
)
