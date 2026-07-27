"""BirdFrame: Audubon plates on a screen, driven by BirdNET-Go detections."""

from __future__ import annotations

import os

__all__ = ["__version__", "create_app"]

# Releases are identified by git tag alone -- nothing commits a version back into
# this file, so a literal here would go stale and then lie in /healthz. The
# release build passes the tag in as BIRDFRAME_VERSION; a local or source run has
# no release to name and says so.
__version__ = os.environ.get("BIRDFRAME_VERSION") or "dev"


def __getattr__(name: str) -> object:
    # Lazy, so `import birdframe` does not drag in Flask (and so the version is
    # readable from packaging tools without the app's dependencies installed).
    if name == "create_app":
        from .app import create_app

        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
