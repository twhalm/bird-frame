"""HTTP surface: routes, webhook auth, health status codes."""

from __future__ import annotations

from typing import ClassVar

import pytest
import responses

from birdframe.app import create_app
from birdframe.config import Settings


@pytest.mark.usefixtures("no_warm")
class TestHome:
    def test_serves_the_frame(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert b"BirdFrame" in r.data


@pytest.mark.usefixtures("no_warm")
class TestApiCurrent:
    def test_empty_gallery(self, client):
        body = client.get("/api/current").get_json()
        assert body["current"] is None
        assert body["recent"] == []
        assert body["count"] == 0

    def test_after_a_detection(self, client, app):
        app.extensions["birdframe"]["gallery"].record(
            "Cardinalis cardinalis", "Northern Cardinal", 0.94, "t", "test"
        )
        body = client.get("/api/current").get_json()
        assert body["current"]["common_name"] == "Northern Cardinal"
        assert body["count"] == 1
        assert body["current"]["plate"]

    def test_recent_is_not_capped_below_history_size(self, client, app, settings):
        """`recent` used to be sliced to 12, silently capping HISTORY_SIZE.

        The page rotates over exactly this list, so a truncation here is a
        truncation of the whole rotation.
        """
        gallery = app.extensions["birdframe"]["gallery"]
        for i in range(settings.history_size):
            gallery.record(
                "Cardinalis cardinalis", "Northern Cardinal", 0.9, f"t{i}", "test"
            )
        body = client.get("/api/current").get_json()
        assert len(body["recent"]) == settings.history_size

    def test_unplated_entries_never_appear(self, client, app):
        gallery = app.extensions["birdframe"]["gallery"]
        gallery.record("Fakeus fakeus", "Fake Bird", 0.99, "t", "test")
        body = client.get("/api/current").get_json()
        assert body["recent"] == []


@pytest.mark.usefixtures("no_warm")
class TestWebhook:
    def test_accepts_a_detection(self, client):
        r = client.post(
            "/webhook",
            json={
                "scientific_name": "Cardinalis cardinalis",
                "species": "Northern Cardinal",
                "confidence": 0.94,
            },
        )
        assert r.status_code == 200
        assert r.get_json() == {"ok": True, "displayed": True, "reason": "displayed"}

    def test_percentage_confidence(self, client):
        r = client.post(
            "/webhook",
            json={"scientific_name": "Cardinalis cardinalis", "confidence": 94},
        )
        assert r.get_json()["displayed"]

    def test_missing_confidence_is_not_displayed(self, client):
        r = client.post("/webhook", json={"scientific_name": "Cardinalis cardinalis"})
        assert r.status_code == 200
        assert not r.get_json()["displayed"]
        assert r.get_json()["reason"] == "below_confidence_threshold"

    def test_unmatched_species_reports_why(self, client):
        r = client.post(
            "/webhook", json={"scientific_name": "Fakeus fakeus", "confidence": 0.99}
        )
        assert r.get_json() == {"ok": True, "displayed": False, "reason": "no_plate"}

    def test_non_json_body_is_a_400(self, client):
        r = client.post("/webhook", data="not json", content_type="text/plain")
        assert r.status_code == 400

    def test_json_array_is_a_400(self, client):
        r = client.post("/webhook", json=[1, 2, 3])
        assert r.status_code == 400

    def test_no_species_field_is_a_400(self, client):
        r = client.post("/webhook", json={"confidence": 0.9})
        assert r.status_code == 400
        assert "no species" in r.get_json()["error"]

    def test_malformed_json_does_not_500(self, client):
        r = client.post("/webhook", data="{oops", content_type="application/json")
        assert r.status_code == 400


class TestWebhookAuth:
    TOKEN = "s3cret-token"
    PAYLOAD: ClassVar[dict[str, object]] = {
        "scientific_name": "Cardinalis cardinalis",
        "confidence": 0.94,
    }

    @pytest.fixture
    def secured(self, tmp_path, no_warm):
        settings = Settings(cache_dir=tmp_path / "cache", webhook_token=self.TOKEN)
        app = create_app(settings, start_poller=False, load_history=False)
        yield app.test_client()
        app.extensions["birdframe"]["gallery"].shutdown()

    def test_no_token_is_rejected(self, secured):
        assert secured.post("/webhook", json=self.PAYLOAD).status_code == 401

    def test_wrong_token_is_rejected(self, secured):
        r = secured.post(
            "/webhook", json=self.PAYLOAD, headers={"X-Webhook-Token": "wrong"}
        )
        assert r.status_code == 401

    def test_header_token_is_accepted(self, secured):
        r = secured.post(
            "/webhook", json=self.PAYLOAD, headers={"X-Webhook-Token": self.TOKEN}
        )
        assert r.status_code == 200

    def test_bearer_token_is_accepted(self, secured):
        r = secured.post(
            "/webhook",
            json=self.PAYLOAD,
            headers={"Authorization": f"Bearer {self.TOKEN}"},
        )
        assert r.status_code == 200

    def test_query_token_is_accepted(self, secured):
        r = secured.post(f"/webhook?token={self.TOKEN}", json=self.PAYLOAD)
        assert r.status_code == 200

    def test_demo_is_also_protected(self, secured):
        assert secured.post("/api/demo").status_code == 401

    def test_unauthenticated_by_default(self, client):
        """No token configured means an open endpoint, which is the LAN default."""
        assert client.post("/api/demo").status_code == 200


@pytest.mark.usefixtures("no_warm")
class TestDemo:
    def test_seeds_the_gallery(self, client):
        body = client.post("/api/demo").get_json()
        assert body["ok"]
        assert body["added"] > 0

    def test_every_demo_bird_resolves(self, client, app, settings):
        """The demo list is hardcoded, so a stale scientific name would show a
        partly empty wall to anyone pressing 'd'."""
        gallery = app.extensions["birdframe"]["gallery"]
        client.post("/api/demo")
        assert gallery.stats.unmatched == 0
        assert gallery.stats.rejected == 0


class TestPlateImage:
    def test_unknown_plate_is_a_404(self, client):
        assert client.get("/plate/99999").status_code == 404

    @responses.activate
    def test_serves_a_cached_plate_immutably(self, client, app):
        settings = app.extensions["birdframe"]["settings"]
        index = app.extensions["birdframe"]["index"]
        responses.add(
            responses.GET,
            f"{settings.image_repo}/1-99/plate-1-wild-turkey.jpg",
            body=b"jpegbytes",
            status=200,
        )
        r = client.get("/plate/1")
        try:
            assert r.status_code == 200
            assert "immutable" in r.headers["Cache-Control"]
            assert r.data == b"jpegbytes"
            assert index.cached_count() == 1
        finally:
            # send_file hands back an open file object; the test client will not
            # close it for us, and filterwarnings=error turns the leak into a failure.
            r.close()

    @responses.activate
    def test_unavailable_plate_is_a_503(self, client, app):
        settings = app.extensions["birdframe"]["settings"]
        responses.add(
            responses.GET,
            f"{settings.image_repo}/1-99/plate-1-wild-turkey.jpg",
            status=404,
        )
        assert client.get("/plate/1").status_code == 503


class TestHealthz:
    def test_webhook_only_mode_is_healthy(self, client):
        r = client.get("/healthz")
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"]
        assert body["poller"]["enabled"] is False

    def test_reports_stats_and_counts(self, client):
        body = client.get("/healthz").get_json()
        assert set(body["stats"]) == {"received", "matched", "unmatched", "rejected"}
        assert body["cached_plates"] == 0
        assert body["history"] == 0

    def test_reports_the_running_version(self, client):
        """Lets you confirm from Portainer that a redeploy actually took."""
        from birdframe import __version__

        assert client.get("/healthz").get_json()["version"] == __version__

    def test_failing_poller_returns_503(self, tmp_path, no_warm):
        """The container healthcheck depends on this status code."""
        settings = Settings(
            cache_dir=tmp_path / "cache", birdnet_url="http://birdnet.invalid"
        )
        app = create_app(settings, start_poller=False, load_history=False)
        try:
            gallery = app.extensions["birdframe"]["gallery"]
            gallery.poll_health.consecutive_failures = 3
            gallery.poll_health.last_error = "boom"

            r = app.test_client().get("/healthz")
            assert r.status_code == 503
            assert r.get_json()["ok"] is False
            assert r.get_json()["poller"]["last_error"] == "boom"
        finally:
            app.extensions["birdframe"]["gallery"].shutdown()


class TestFactory:
    def test_templates_do_not_autoreload_by_default(self, app):
        """Auto-reload stats the template on every request; dev-only."""
        assert app.config["TEMPLATES_AUTO_RELOAD"] is False

    def test_dev_enables_autoreload(self, tmp_path):
        settings = Settings(cache_dir=tmp_path / "cache", dev=True)
        app = create_app(settings, start_poller=False, load_history=False)
        try:
            assert app.config["TEMPLATES_AUTO_RELOAD"] is True
        finally:
            app.extensions["birdframe"]["gallery"].shutdown()

    def test_cache_dir_is_created(self, app, settings):
        assert settings.plate_cache_dir.is_dir()

    def test_two_apps_do_not_share_state(self, tmp_path, no_warm):
        """State used to live in module globals, so two apps in one process
        shared one gallery."""
        a = create_app(
            Settings(cache_dir=tmp_path / "a"), start_poller=False, load_history=False
        )
        b = create_app(
            Settings(cache_dir=tmp_path / "b"), start_poller=False, load_history=False
        )
        try:
            a.extensions["birdframe"]["gallery"].record(
                "Cardinalis cardinalis", "Northern Cardinal", 0.94, "t", "test"
            )
            assert len(a.extensions["birdframe"]["gallery"].snapshot()) == 1
            assert len(b.extensions["birdframe"]["gallery"].snapshot()) == 0
        finally:
            a.extensions["birdframe"]["gallery"].shutdown()
            b.extensions["birdframe"]["gallery"].shutdown()
