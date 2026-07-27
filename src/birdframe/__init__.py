"""BirdFrame: Audubon plates on a screen, driven by BirdNET-Go detections."""

from __future__ import annotations

__all__ = ["__version__", "create_app"]

__version__ = "0.2.0"


def __getattr__(name: str) -> object:
    # Lazy, so `import birdframe` does not drag in Flask (and so the version is
    # readable from packaging tools without the app's dependencies installed).
    if name == "create_app":
        from .app import create_app

        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
