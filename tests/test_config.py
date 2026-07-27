"""Environment parsing. Bad input must fall back, never crash the container."""

from __future__ import annotations

import pytest

from birdframe.config import DEFAULT_IMAGE_REPO, Settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in (
        "MIN_CONFIDENCE",
        "HISTORY_SIZE",
        "BIRDNET_URL",
        "POLL_SECONDS",
        "POLL_LIMIT",
        "WEBHOOK_TOKEN",
        "CACHE_DIR",
        "IMAGE_REPO",
        "CA_BUNDLE",
        "VERIFY_TLS",
        "PORT",
        "DEV",
    ):
        monkeypatch.delenv(var, raising=False)


class TestDefaults:
    def test_documented_defaults(self):
        s = Settings.from_env()
        assert s.min_confidence == 0.65
        assert s.history_size == 40
        assert s.poll_seconds == 60
        assert s.birdnet_url == ""
        assert s.webhook_token is None
        assert s.verify_tls is True
        assert s.image_repo == DEFAULT_IMAGE_REPO

    def test_webhook_only_by_default(self):
        assert not Settings.from_env().polling_enabled


class TestParsing:
    def test_reads_values(self, monkeypatch):
        monkeypatch.setenv("MIN_CONFIDENCE", "0.8")
        monkeypatch.setenv("HISTORY_SIZE", "12")
        monkeypatch.setenv("POLL_SECONDS", "30")
        s = Settings.from_env()
        assert s.min_confidence == 0.8
        assert s.history_size == 12
        assert s.poll_seconds == 30

    @pytest.mark.parametrize("raw", ["", "  ", "banana", "1.2.3"])
    def test_junk_ints_fall_back_to_the_default(self, monkeypatch, raw):
        monkeypatch.setenv("HISTORY_SIZE", raw)
        assert Settings.from_env().history_size == 40

    @pytest.mark.parametrize("raw", ["", "banana"])
    def test_junk_floats_fall_back(self, monkeypatch, raw):
        monkeypatch.setenv("MIN_CONFIDENCE", raw)
        assert Settings.from_env().min_confidence == 0.65

    def test_history_size_cannot_be_zero(self, monkeypatch):
        """A maxlen of 0 would make the wall permanently blank."""
        monkeypatch.setenv("HISTORY_SIZE", "0")
        assert Settings.from_env().history_size == 1

    def test_poll_seconds_cannot_be_zero(self, monkeypatch):
        """Zero would busy-loop the poller against BirdNET-Go."""
        monkeypatch.setenv("POLL_SECONDS", "0")
        assert Settings.from_env().poll_seconds == 1

    @pytest.mark.parametrize("raw", ["true", "TRUE", "1", "yes", "on", " True "])
    def test_truthy_flags(self, monkeypatch, raw):
        monkeypatch.setenv("DEV", raw)
        assert Settings.from_env().dev is True

    @pytest.mark.parametrize("raw", ["false", "0", "no", "off", "", "banana"])
    def test_falsy_flags(self, monkeypatch, raw):
        monkeypatch.setenv("DEV", raw)
        assert Settings.from_env().dev is False

    def test_trailing_slash_stripped_from_urls(self, monkeypatch):
        monkeypatch.setenv("BIRDNET_URL", "http://birdnet:8080/")
        monkeypatch.setenv("IMAGE_REPO", "https://example.com/plates/")
        s = Settings.from_env()
        assert s.birdnet_url == "http://birdnet:8080"
        assert s.image_repo == "https://example.com/plates"
        assert s.recent_url == "http://birdnet:8080/api/v2/detections/recent"

    def test_empty_webhook_token_is_none(self, monkeypatch):
        """An empty string must not enable auth-with-an-empty-secret."""
        monkeypatch.setenv("WEBHOOK_TOKEN", "")
        assert Settings.from_env().webhook_token is None


class TestTls:
    def test_ca_bundle_becomes_the_verify_path(self, monkeypatch):
        monkeypatch.setenv("CA_BUNDLE", "/certs/corp.pem")
        assert Settings.from_env().verify_tls == "/certs/corp.pem"

    def test_verify_tls_false_disables_verification(self, monkeypatch):
        monkeypatch.setenv("VERIFY_TLS", "false")
        assert Settings.from_env().verify_tls is False

    def test_ca_bundle_wins_over_verify_tls(self, monkeypatch):
        monkeypatch.setenv("CA_BUNDLE", "/certs/corp.pem")
        monkeypatch.setenv("VERIFY_TLS", "false")
        assert Settings.from_env().verify_tls == "/certs/corp.pem"


class TestDerived:
    def test_paths_hang_off_cache_dir(self, tmp_path):
        s = Settings(cache_dir=tmp_path)
        assert s.plate_cache_dir == tmp_path / "plates"
        assert s.history_file == tmp_path / "history.json"

    def test_stale_window_is_three_cycles(self):
        assert Settings(poll_seconds=60).stale_after_seconds == 180

    def test_stale_window_has_a_floor(self):
        """A 5s poll interval should not make a single slow request unhealthy."""
        assert Settings(poll_seconds=5).stale_after_seconds == 120

    def test_settings_are_immutable(self):
        s = Settings()
        with pytest.raises(AttributeError):
            s.min_confidence = 0.9  # type: ignore[misc]
