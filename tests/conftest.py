from __future__ import annotations

import pytest

from birdframe.app import create_app
from birdframe.config import Settings
from birdframe.gallery import Gallery
from birdframe.plates import PlateIndex


@pytest.fixture
def settings(tmp_path):
    """Real plate data, throwaway cache dir, no network."""
    return Settings(
        cache_dir=tmp_path / "cache",
        min_confidence=0.65,
        history_size=5,
        image_repo="https://example.invalid/plates",
    )


@pytest.fixture
def index(settings):
    return PlateIndex(settings)


@pytest.fixture
def gallery(settings, index):
    g = Gallery(settings, index)
    yield g
    g.shutdown()


@pytest.fixture
def app(settings):
    application = create_app(settings, start_poller=False, load_history=False)
    yield application
    application.extensions["birdframe"]["gallery"].shutdown()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def no_warm(monkeypatch):
    """Stop the gallery reaching for the network to warm its cache.

    Every record() of a matched plate submits ensure_cached to a thread pool;
    left alone, tests would try to fetch from example.invalid.
    """
    monkeypatch.setattr(PlateIndex, "ensure_cached", lambda self, plate, name: None)
