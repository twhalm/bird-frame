"""Species -> Audubon plate resolution, and lazy image fetch/cache.

Matching strategy, in order of trust:
  1. curated_map.json, keyed by scientific name  (hand-verified, authoritative)
  2. exact match of BirdNET common name against the plate name
  3. exact match after light normalisation (case/punctuation/spacing)

There is deliberately NO fuzzy matching. It was measured and it produces
confidently wrong species: "Golden-winged Woodpecker" (a Northern Flicker)
fuzzy-matches "Golden-naped Woodpecker", and Wikipedia redirects send
"Le petit caporal" to Napoleon Bonaparte. A wrong bird on the wall is worse
than an empty mat.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import requests

from .config import DATA_DIR, Settings

log = logging.getLogger("birdframe.plates")

MatchKind = Literal["curated", "name"]


@dataclass(frozen=True, slots=True)
class PlateMatch:
    """A resolved plate. Merged into the detection entry as-is."""

    plate: int
    file_name: str
    audubon_name: str
    match: MatchKind

    def as_dict(self) -> dict[str, object]:
        return {
            "plate": self.plate,
            "file_name": self.file_name,
            "audubon_name": self.audubon_name,
            "match": self.match,
        }


def normalise(s: str) -> str:
    """Fold a bird name for exact-after-normalisation comparison.

    Case, punctuation and whitespace only. Nothing here can turn one species
    into another, which is the whole point.
    """
    s = s.lower().replace("’", "'").replace("&", "and")
    s = re.sub(r"[^a-z ]", " ", s)
    return " ".join(s.split())


def bucket_for(plate: int) -> str:
    """Plate images are grouped into directories by hundred in the source repo."""
    if plate < 100:
        return "1-99"
    if plate < 200:
        return "100-199"
    if plate < 300:
        return "200-299"
    if plate < 400:
        return "300-399"
    return "400-435"


class PlateIndex:
    """The plate catalogue plus the on-disk image cache."""

    def __init__(self, settings: Settings, data_dir: Path | None = None) -> None:
        self.settings = settings
        self.data_dir = data_dir or DATA_DIR
        # Shapes as they appear in the JSON: plates.json rows carry
        # plate/name/slug/fileName, curated_map values carry plate/audubon.
        self.plates: dict[int, dict[str, Any]] = {}
        self.by_scientific: dict[str, dict[str, Any]] = {}
        self.by_common: dict[str, int] = {}
        self._fetch_locks: dict[int, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._load()

    def _load(self) -> None:
        raw = json.loads((self.data_dir / "plates.json").read_text(encoding="utf-8"))
        for p in raw:
            self.plates[p["plate"]] = p
            self.by_common[normalise(p["name"])] = p["plate"]

        curated = json.loads(
            (self.data_dir / "curated_map.json").read_text(encoding="utf-8")
        )["map"]
        for sci, entry in curated.items():
            self.by_scientific[sci.strip().lower()] = entry

        log.info(
            "loaded %d plates, %d curated species mappings",
            len(self.plates),
            len(self.by_scientific),
        )

    # ------------------------------------------------------------------ resolve

    def resolve(self, scientific: str | None, common: str | None) -> PlateMatch | None:
        """Return plate info for a detection, or None if there is no honest match."""
        if scientific:
            hit = self.by_scientific.get(scientific.strip().lower())
            if hit:
                plate = self.plates.get(hit["plate"])
                if plate:
                    return PlateMatch(
                        plate=plate["plate"],
                        file_name=plate["fileName"],
                        audubon_name=hit.get("audubon") or plate["name"],
                        match="curated",
                    )
                log.warning(
                    "curated_map points %s at plate %s, which is not in plates.json",
                    scientific,
                    hit.get("plate"),
                )

        if common:
            plate_no = self.by_common.get(normalise(common))
            if plate_no:
                plate = self.plates[plate_no]
                return PlateMatch(
                    plate=plate["plate"],
                    file_name=plate["fileName"],
                    audubon_name=plate["name"],
                    match="name",
                )

        return None

    # -------------------------------------------------------------------- cache

    def local_path(self, plate: int, file_name: str) -> Path:
        return self.settings.plate_cache_dir / bucket_for(plate) / file_name

    def cached_count(self) -> int:
        root = self.settings.plate_cache_dir
        if not root.exists():
            return 0
        return sum(1 for _ in root.rglob("*.jpg"))

    def ensure_cached(self, plate: int, file_name: str) -> Path | None:
        """Download the plate into the cache volume if we don't have it yet.

        Serialised per-plate so a burst of detections for the same species does
        not start several downloads of the same file.
        """
        dest = self.local_path(plate, file_name)
        if self._usable(dest):
            return dest

        with self._locks_guard:
            lock = self._fetch_locks.setdefault(plate, threading.Lock())

        with lock:
            # Another thread may have finished the download while we waited.
            if self._usable(dest):
                return dest

            url = f"{self.settings.image_repo}/{bucket_for(plate)}/{file_name}"
            tmp = dest.with_suffix(dest.suffix + ".part")
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                log.info("fetching plate %d from %s", plate, url)
                with requests.get(
                    url, stream=True, timeout=60, verify=self.settings.verify_tls
                ) as r:
                    r.raise_for_status()
                    with tmp.open("wb") as fh:
                        for chunk in r.iter_content(65536):
                            fh.write(chunk)
                # Atomic swap, so a partial file is never served.
                tmp.replace(dest)
                log.info("cached plate %d (%.1f MB)", plate, dest.stat().st_size / 1e6)
            except Exception as exc:
                log.warning("failed to fetch plate %d: %s", plate, exc)
                tmp.unlink(missing_ok=True)
                return None
            return dest

    @staticmethod
    def _usable(path: Path) -> bool:
        try:
            return path.stat().st_size > 0
        except OSError:
            return False
