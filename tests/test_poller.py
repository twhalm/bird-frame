"""The BirdNET-Go poller."""

from __future__ import annotations

import threading
import time

import pytest
import requests
import responses

from birdframe.config import Settings
from birdframe.gallery import Gallery
from birdframe.plates import PlateIndex
from birdframe.poller import Poller

RECENT = "http://birdnet.invalid/api/v2/detections/recent"


@pytest.fixture
def poll_settings(tmp_path):
    return Settings(
        cache_dir=tmp_path / "cache",
        birdnet_url="http://birdnet.invalid",
        poll_seconds=1,
        history_size=10,
        image_repo="https://example.invalid/plates",
    )


@pytest.fixture
def poller(poll_settings, no_warm):
    index = PlateIndex(poll_settings)
    gallery = Gallery(poll_settings, index)
    p = Poller(poll_settings, gallery)
    yield p
    p.stop()
    gallery.shutdown()


def _row(sci, common, conf, when):
    return {
        "scientificName": sci,
        "commonName": common,
        "confidence": conf,
        "timestamp": when,
    }


class TestPollOnce:
    @responses.activate
    def test_records_detections(self, poller):
        responses.add(
            responses.GET,
            RECENT,
            json=[
                _row("Cardinalis cardinalis", "Northern Cardinal", 0.94, "t1"),
                _row("Cyanocitta cristata", "Blue Jay", 0.91, "t2"),
            ],
        )
        assert poller.poll_once() == 2
        assert len(poller.gallery.snapshot()) == 2

    @responses.activate
    def test_oldest_first_so_newest_ends_up_at_the_front(self, poller):
        # The API returns newest first; the gallery must end up with the same order.
        responses.add(
            responses.GET,
            RECENT,
            json=[
                _row("Cyanocitta cristata", "Blue Jay", 0.91, "newest"),
                _row("Cardinalis cardinalis", "Northern Cardinal", 0.94, "oldest"),
            ],
        )
        poller.poll_once()
        assert poller.gallery.snapshot()[0].detected_at == "newest"

    @responses.activate
    def test_repeat_poll_does_not_duplicate(self, poller):
        responses.add(
            responses.GET,
            RECENT,
            json=[_row("Cardinalis cardinalis", "Northern Cardinal", 0.94, "t1")],
        )
        assert poller.poll_once() == 1
        assert poller.poll_once() == 0
        assert len(poller.gallery.snapshot()) == 1

    @responses.activate
    def test_wrapped_response_body(self, poller):
        responses.add(
            responses.GET,
            RECENT,
            json={
                "data": [_row("Cardinalis cardinalis", "Northern Cardinal", 0.94, "t")]
            },
        )
        assert poller.poll_once() == 1

    @responses.activate
    def test_rows_without_species_are_skipped(self, poller):
        responses.add(responses.GET, RECENT, json=[{"confidence": 0.9}, "junk"])
        assert poller.poll_once() == 0

    @responses.activate
    def test_percentage_confidence(self, poller):
        responses.add(
            responses.GET,
            RECENT,
            json=[_row("Cardinalis cardinalis", "Northern Cardinal", 94, "t")],
        )
        assert poller.poll_once() == 1

    @responses.activate
    def test_limit_is_passed_through(self, poller):
        responses.add(responses.GET, RECENT, json=[])
        poller.poll_once()
        assert f"limit={poller.settings.poll_limit}" in str(
            responses.calls[0].request.url
        )


class TestPollHealthTracking:
    @responses.activate
    def test_success_marks_healthy(self, poller):
        responses.add(responses.GET, RECENT, json=[])
        poller.poll_once()
        assert poller.gallery.poll_health.last_ok is not None
        assert poller.gallery.poll_health.consecutive_failures == 0
        assert poller.gallery.is_healthy()

    @responses.activate
    def test_http_error_counts_a_failure(self, poller):
        responses.add(responses.GET, RECENT, status=500)
        assert poller.poll_once() == 0
        health = poller.gallery.poll_health
        assert health.consecutive_failures == 1
        assert health.last_error is not None

    @responses.activate
    def test_connection_error_counts_a_failure(self, poller):
        responses.add(responses.GET, RECENT, body=requests.ConnectionError("refused"))
        assert poller.poll_once() == 0
        assert poller.gallery.poll_health.consecutive_failures == 1

    @responses.activate
    def test_invalid_json_counts_a_failure(self, poller):
        responses.add(responses.GET, RECENT, body="<html>not json</html>", status=200)
        assert poller.poll_once() == 0
        assert poller.gallery.poll_health.consecutive_failures == 1

    @responses.activate
    def test_failures_accumulate_then_reset(self, poller):
        responses.add(responses.GET, RECENT, status=500)
        poller.poll_once()
        poller.poll_once()
        assert poller.gallery.poll_health.consecutive_failures == 2

        responses.reset()
        responses.add(responses.GET, RECENT, json=[])
        poller.poll_once()
        assert poller.gallery.poll_health.consecutive_failures == 0
        assert poller.gallery.poll_health.last_error is None

    @responses.activate
    def test_repeated_failure_makes_the_app_unhealthy(self, poller):
        """This is the point of the health tracking.

        /healthz used to return ok while the poller had been failing for a week,
        which left the container healthcheck blind to the only outage that
        actually takes the wall down.
        """
        responses.add(responses.GET, RECENT, status=500)
        poller.poll_once()
        assert not poller.gallery.is_healthy()


class TestLifecycle:
    def test_start_is_a_no_op_without_a_url(self, tmp_path, no_warm):
        settings = Settings(cache_dir=tmp_path / "c", birdnet_url="")
        index = PlateIndex(settings)
        gallery = Gallery(settings, index)
        p = Poller(settings, gallery)
        p.start()
        assert p._thread is None
        gallery.shutdown()

    @responses.activate
    def test_stop_interrupts_the_wait_promptly(self, poller):
        """time.sleep is not interruptible; Event.wait is.

        With sleep, a SIGTERM during a 60s nap meant a 60s wait for shutdown.
        """
        responses.add(responses.GET, RECENT, json=[])
        poller.settings = Settings(
            cache_dir=poller.settings.cache_dir,
            birdnet_url=poller.settings.birdnet_url,
            poll_seconds=3600,
            image_repo=poller.settings.image_repo,
        )
        poller.start()
        started = time.monotonic()
        poller.stop(timeout=5)
        assert time.monotonic() - started < 3
        assert threading.active_count() >= 1

    @responses.activate
    def test_double_start_makes_one_thread(self, poller):
        responses.add(responses.GET, RECENT, json=[])
        poller.start()
        first = poller._thread
        poller.start()
        assert poller._thread is first
