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

# The Frame's own panel. Composing at panel resolution means the TV never has to
# rescale, which is what keeps the bevel a crisp one-pixel-accurate edge.
DEFAULT_FRAME_SIZE = (3840, 2160)

# Gallery raking light: azimuth (0 = straight above, negative = from the left)
# and elevation above the wall plane, both in degrees. Shades the bevel.
DEFAULT_LIGHT = (-35.0, 40.0)

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


def _pair(name: str, default: tuple[float, float]) -> tuple[float, float]:
    """Parse a "a,b" pair. Anything malformed falls back to the default whole."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    parts = raw.split(",")
    if len(parts) != 2:
        return default
    try:
        return (float(parts[0]), float(parts[1]))
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

    # --- the Samsung Frame
    tv_host: str = ""
    tv_port: int = 8002
    tv_name: str = "BirdFrame"
    # How long one composition hangs before the next. Every change is an upload
    # to the TV's internal flash, so this wants to be minutes, not seconds.
    rotate_seconds: int = 900
    # Uploads kept on the TV. The one showing plus a little history, so a select
    # never races a delete of the image it just chose.
    tv_keep_uploads: int = 3
    # Whether art mode starts driven, or waits for the toggle.
    art_on_start: bool = False

    # --- composition
    frame_size: tuple[int, int] = DEFAULT_FRAME_SIZE
    light: tuple[float, float] = DEFAULT_LIGHT

    # --- misc
    port: int = 8080
    dev: bool = False
    unmatched_log_size: int = 50

    # Populated in __post_init__ so callers never have to compute it.
    plate_cache_dir: Path = field(init=False)
    history_file: Path = field(init=False)
    tv_token_file: Path = field(init=False)
    tv_state_file: Path = field(init=False)

    def __post_init__(self) -> None:
        # frozen dataclass: bypass the setattr guard for derived fields.
        object.__setattr__(self, "plate_cache_dir", self.cache_dir / "plates")
        object.__setattr__(self, "history_file", self.cache_dir / "history.json")
        # The pairing token lives on the cache volume: without it every restart
        # pops the "allow this device?" prompt on the TV again.
        object.__setattr__(self, "tv_token_file", self.cache_dir / "tv-token.txt")
        object.__setattr__(self, "tv_state_file", self.cache_dir / "tv-state.json")

    @property
    def polling_enabled(self) -> bool:
        return bool(self.birdnet_url)

    @property
    def tv_configured(self) -> bool:
        return bool(self.tv_host)

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
            tv_host=os.environ.get("TV_HOST", "").strip(),
            tv_port=_int("TV_PORT", 8002, minimum=1),
            tv_name=os.environ.get("TV_NAME", "BirdFrame").strip() or "BirdFrame",
            rotate_seconds=_int("ROTATE_SECONDS", 900, minimum=30),
            tv_keep_uploads=_int("TV_KEEP_UPLOADS", 3, minimum=1),
            art_on_start=_flag("ART_ON_START", False),
            frame_size=(
                _int("FRAME_WIDTH", DEFAULT_FRAME_SIZE[0], minimum=320),
                _int("FRAME_HEIGHT", DEFAULT_FRAME_SIZE[1], minimum=180),
            ),
            light=_pair("LIGHT", DEFAULT_LIGHT),
            port=_int("PORT", 8080, minimum=1),
            dev=_flag("DEV", False),
        )
