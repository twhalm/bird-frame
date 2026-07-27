"""Configuration, read from the environment once at startup.

Everything is here rather than scattered as module-level ``os.environ`` reads so
that tests can build a ``Settings`` directly and the full set of knobs is
greppable in one place.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
DATA_DIR = PACKAGE_DIR / "data"
WEB_DIR = PACKAGE_DIR / "web"

# The plate images live in this repo, downsized so the smallest dimension is
# 2000px (~1.1MB each). The full-resolution repo averages 6.5MB per plate, which
# is more than a 4K screen can show.
DEFAULT_IMAGE_REPO = (
    "https://raw.githubusercontent.com/nathanbuchar/"
    "audubon-bird-plates-for-supernote/master/plates"
)


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime configuration. Immutable once built."""

    # --- detection filtering
    min_confidence: float = 0.65
    history_size: int = 40

    # --- BirdNET-Go polling
    birdnet_url: str = ""
    poll_seconds: int = 60
    poll_limit: int = 15

    # --- webhook
    webhook_token: str | None = None

    # --- images
    cache_dir: Path = Path("/cache")
    image_repo: str = DEFAULT_IMAGE_REPO
    verify_tls: bool | str = True

    # --- misc
    port: int = 8080
    dev: bool = False
    unmatched_log_size: int = 50

    # Populated in __post_init__ so callers never have to compute it.
    plate_cache_dir: Path = field(init=False)
    history_file: Path = field(init=False)

    def __post_init__(self) -> None:
        # frozen dataclass: bypass the setattr guard for derived fields.
        object.__setattr__(self, "plate_cache_dir", self.cache_dir / "plates")
        object.__setattr__(self, "history_file", self.cache_dir / "history.json")

    @property
    def polling_enabled(self) -> bool:
        return bool(self.birdnet_url)

    @property
    def recent_url(self) -> str:
        return f"{self.birdnet_url}/api/v2/detections/recent"

    @property
    def stale_after_seconds(self) -> float:
        """How long the poller may go without a success before /healthz fails.

        Three cycles, so a single dropped request or a BirdNET-Go restart does
        not flap the container healthcheck.
        """
        return max(3 * self.poll_seconds, 120)

    @classmethod
    def from_env(cls) -> Settings:
        # A TLS-intercepting proxy presents its own CA. CA_BUNDLE points at that
        # CA's PEM; VERIFY_TLS=false disables verification entirely and is a last
        # resort, since it also disables it for the plate downloads.
        ca_bundle = os.environ.get("CA_BUNDLE")
        verify: bool | str = ca_bundle if ca_bundle else _flag("VERIFY_TLS", True)

        token = os.environ.get("WEBHOOK_TOKEN") or None

        return cls(
            min_confidence=_float("MIN_CONFIDENCE", 0.65),
            history_size=_int("HISTORY_SIZE", 40, minimum=1),
            birdnet_url=os.environ.get("BIRDNET_URL", "").rstrip("/"),
            poll_seconds=_int("POLL_SECONDS", 60, minimum=1),
            poll_limit=_int("POLL_LIMIT", 15, minimum=1),
            webhook_token=token,
            cache_dir=Path(os.environ.get("CACHE_DIR", "/cache")),
            image_repo=os.environ.get("IMAGE_REPO", DEFAULT_IMAGE_REPO).rstrip("/"),
            verify_tls=verify,
            port=_int("PORT", 8080, minimum=1),
            dev=_flag("DEV", False),
        )
