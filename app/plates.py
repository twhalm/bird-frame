"""Species -> Audubon plate resolution, and lazy image fetch/cache.

Matching strategy, in order of trust:
  1. curated_map.json, keyed by scientific name  (hand-verified, authoritative)
  2. exact match of BirdNET common name against the plate name
  3. exact match after light normalisation (case/punctuation/spacing)

There is deliberately NO fuzzy matching. It was measured and it produces
confidently wrong species: "Golden-winged Woodpecker" (a Northern Flicker)
fuzzy-matches "Golden-naped Woodpecker", and Wikipedia redirects send
"Le petit caporal" to Napoleon Bonaparte. A wrong bird on the wall is worse
than a graceful "no plate" card.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path

import requests

log = logging.getLogger("birdframe.plates")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
# /cache is the volume mount in the container; override for local runs.
CACHE_DIR = Path(os.environ.get("CACHE_DIR", "/cache")) / "plates"

# The plate images live in this repo, downsized so the smallest dimension is
# 2000px (~1.1MB each, 487MB for all 435). The full-res repo averages 6.5MB
# per plate / 2.9GB total, which is overkill for a 4K screen.
IMAGE_REPO = os.environ.get(
    "IMAGE_REPO",
    "https://raw.githubusercontent.com/nathanbuchar/"
    "audubon-bird-plates-for-supernote/master/plates",
).rstrip("/")

# Networks that intercept TLS (corporate proxies) present their own CA and the
# fetch fails cert verification. Point CA_BUNDLE at that CA's PEM, or as a last
# resort set VERIFY_TLS=false.
_CA_BUNDLE = os.environ.get("CA_BUNDLE")
_VERIFY: bool | str = (
    _CA_BUNDLE if _CA_BUNDLE else os.environ.get("VERIFY_TLS", "true").lower() != "false"
)

_fetch_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()


def _norm(s: str) -> str:
    s = s.lower().replace("’", "'").replace("&", "and")
    s = re.sub(r"[^a-z ]", " ", s)
    return " ".join(s.split())


class PlateIndex:
    def __init__(self) -> None:
        self.plates: dict[int, dict] = {}
        self.by_scientific: dict[str, dict] = {}
        self.by_common: dict[str, int] = {}
        self._load()

    def _load(self) -> None:
        raw = json.loads((DATA_DIR / "plates.json").read_text(encoding="utf-8"))
        for p in raw:
            self.plates[p["plate"]] = p
            self.by_common[_norm(p["name"])] = p["plate"]

        curated = json.loads(
            (DATA_DIR / "curated_map.json").read_text(encoding="utf-8")
        )["map"]
        for sci, entry in curated.items():
            self.by_scientific[sci.lower()] = entry

        log.info(
            "loaded %d plates, %d curated species mappings",
            len(self.plates),
            len(self.by_scientific),
        )

    def bucket(self, plate: int) -> str:
        """Plate images are grouped into directories by hundred."""
        if plate < 100:
            return "1-99"
        if plate < 200:
            return "100-199"
        if plate < 300:
            return "200-299"
        if plate < 400:
            return "300-399"
        return "400-435"

    def resolve(self, scientific: str | None, common: str | None) -> dict | None:
        """Return plate info for a detection, or None if we have no honest match."""
        if scientific:
            hit = self.by_scientific.get(scientific.strip().lower())
            if hit:
                plate = self.plates.get(hit["plate"])
                if plate:
                    return {
                        "plate": plate["plate"],
                        "file_name": plate["fileName"],
                        "audubon_name": hit.get("audubon") or plate["name"],
                        "match": "curated",
                    }

        if common:
            plate_no = self.by_common.get(_norm(common))
            if plate_no:
                plate = self.plates[plate_no]
                return {
                    "plate": plate["plate"],
                    "file_name": plate["fileName"],
                    "audubon_name": plate["name"],
                    "match": "name",
                }

        return None

    def local_path(self, plate: int, file_name: str) -> Path:
        return CACHE_DIR / self.bucket(plate) / file_name

    def ensure_cached(self, plate: int, file_name: str) -> Path | None:
        """Download the plate into the cache volume if we don't have it yet.

        Serialised per-plate so a burst of detections for the same species
        doesn't start several downloads of the same file.
        """
        dest = self.local_path(plate, file_name)
        if dest.exists() and dest.stat().st_size > 0:
            return dest

        with _locks_guard:
            lock = _fetch_locks.setdefault(plate, threading.Lock())

        with lock:
            if dest.exists() and dest.stat().st_size > 0:
                return dest

            url = f"{IMAGE_REPO}/{self.bucket(plate)}/{file_name}"
            tmp = dest.with_suffix(dest.suffix + ".part")
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                log.info("fetching plate %d from %s", plate, url)
                with requests.get(url, stream=True, timeout=60, verify=_VERIFY) as r:
                    r.raise_for_status()
                    with tmp.open("wb") as fh:
                        for chunk in r.iter_content(65536):
                            fh.write(chunk)
                # Atomic swap so a partial file is never served.
                tmp.replace(dest)
                log.info("cached plate %d (%.1f MB)", plate, dest.stat().st_size / 1e6)
                return dest
            except Exception as exc:
                log.warning("failed to fetch plate %d: %s", plate, exc)
                tmp.unlink(missing_ok=True)
                return None
