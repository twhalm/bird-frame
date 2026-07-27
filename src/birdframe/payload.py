"""Parsing the shapes BirdNET-Go sends.

BirdNET-Go's notification template is user-configurable, so the same detection
can arrive as ``{"species": ...}``, ``{"Title": ..., "Metadata": {...}}``, or
something a user hand-rolled. Rather than insisting on one shape, accept the
fields wherever they turn up -- but keep that tolerance in one pure function so
it can be tested against real payloads instead of guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SCIENTIFIC_KEYS = ("scientific_name", "scientificName", "sciName", "ScientificName")
COMMON_KEYS = (
    "species",
    "common_name",
    "commonName",
    "CommonName",
    "title",
    "Title",
    "bird",
)
CONFIDENCE_KEYS = ("confidence", "Confidence")
TIME_KEYS = ("timestamp", "Timestamp", "time", "date", "begin_time")


@dataclass(frozen=True, slots=True)
class ParsedDetection:
    scientific: str
    common: str
    confidence: float | None
    when: str | None

    @property
    def has_species(self) -> bool:
        return bool(self.scientific or self.common)


def normalise_confidence(raw: Any) -> float | None:
    """Coerce a confidence field to a 0-1 ratio, or None if it is not a number.

    BirdNET-Go sends either ``0.93`` or ``93`` depending on the template. None is
    returned rather than 0.0 for a missing value, because "absent" and "the model
    is certain this is not a bird" are different facts and the caller filters on
    them differently.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, str):
        raw = raw.strip().rstrip("%")
        if not raw:
            return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value:  # NaN
        return None
    if value > 1.0:
        value /= 100.0
    return max(0.0, min(1.0, value))


def parse_webhook(payload: dict[str, Any]) -> ParsedDetection:
    """Pull a detection out of a webhook body, checking metadata as well as top level."""
    meta = payload.get("metadata") or payload.get("Metadata") or {}
    if not isinstance(meta, dict):
        meta = {}

    def pick(*keys: str) -> Any:
        for key in keys:
            for src in (payload, meta):
                value = src.get(key)
                if value not in (None, ""):
                    return value
        return None

    when = pick(*TIME_KEYS)
    return ParsedDetection(
        scientific=str(pick(*SCIENTIFIC_KEYS) or ""),
        common=str(pick(*COMMON_KEYS) or ""),
        confidence=normalise_confidence(pick(*CONFIDENCE_KEYS)),
        when=str(when) if when is not None else None,
    )


def parse_poll_row(row: dict[str, Any]) -> ParsedDetection:
    """Pull a detection out of one /api/v2/detections/recent row."""
    scientific = row.get("scientificName") or row.get("scientific_name") or ""
    common = row.get("commonName") or row.get("common_name") or ""

    when = row.get("timestamp")
    if not when:
        # Some builds split it into separate date and time columns.
        when = f"{row.get('date', '')} {row.get('time', '')}".strip() or None

    return ParsedDetection(
        scientific=str(scientific),
        common=str(common),
        confidence=normalise_confidence(row.get("confidence")),
        when=str(when) if when else None,
    )


def extract_rows(data: Any) -> list[dict[str, Any]]:
    """Find the detection list in a response body, whatever it is wrapped in."""
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        for key in ("data", "detections", "results", "items"):
            value = data.get(key)
            if isinstance(value, list):
                rows = value
                break
        else:
            return []
    else:
        return []
    return [r for r in rows if isinstance(r, dict)]
