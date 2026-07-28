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


class TestTvSettings:
    def test_no_tv_by_default(self):
        """An unset TV_HOST composes but never pushes, so the preview still
        works and nothing goes looking for a TV that is not there."""
        assert Settings().tv_configured is False

    def test_reads_the_tv_block(self, monkeypatch):
        monkeypatch.setenv("TV_HOST", "192.168.1.50")
        monkeypatch.setenv("TV_NAME", "Living Room")
        monkeypatch.setenv("ROTATE_SECONDS", "1800")
        monkeypatch.setenv("ART_ON_START", "true")
        s = Settings.from_env()
        assert s.tv_configured is True
        assert (s.tv_host, s.tv_name, s.rotate_seconds) == (
            "192.168.1.50",
            "Living Room",
            1800,
        )
        assert s.art_on_start is True

    def test_the_rotation_has_a_floor(self, monkeypatch):
        """Every change writes ~1.5MB to the TV's flash. A one-second rotation
        is not a setting, it is a mistake."""
        monkeypatch.setenv("ROTATE_SECONDS", "1")
        assert Settings.from_env().rotate_seconds == 30

    def test_an_empty_tv_name_falls_back(self, monkeypatch):
        """It is what the TV shows on its pairing prompt; blank is useless."""
        monkeypatch.setenv("TV_NAME", "   ")
        assert Settings.from_env().tv_name == "BirdFrame"

    def test_at_least_one_upload_is_kept(self, monkeypatch):
        """Zero would mean deleting the picture currently on the wall."""
        monkeypatch.setenv("TV_KEEP_UPLOADS", "0")
        assert Settings.from_env().tv_keep_uploads == 1

    def test_the_tv_is_checked_far_more_often_than_it_rotates(self):
        """It is how BirdFrame notices somebody picked up the remote, so it
        cannot be tied to the rotation."""
        s = Settings()
        assert s.art_check_seconds < s.rotate_seconds

    def test_the_check_has_a_floor(self, monkeypatch):
        """It is a round trip to the TV; once a second is a pointless amount of
        traffic for something a person did with a remote."""
        monkeypatch.setenv("ART_CHECK_SECONDS", "1")
        assert Settings.from_env().art_check_seconds == 5


class TestLight:
    def test_the_default_is_the_gallery_raking_light(self):
        assert Settings().light == (-35.0, 40.0)

    def test_reads_a_pair(self, monkeypatch):
        monkeypatch.setenv("LIGHT", "20,55")
        assert Settings.from_env().light == (20.0, 55.0)

    @pytest.mark.parametrize("raw", ["", "  ", "20", "20,55,90", "left,up", "20,up"])
    def test_junk_falls_back_to_the_whole_default(self, monkeypatch, raw):
        """Half-parsing "20,up" into (20, 40) would silently relight the mat."""
        monkeypatch.setenv("LIGHT", raw)
        assert Settings.from_env().light == (-35.0, 40.0)

    def test_the_bevel_is_a_four_ply_cut_by_default(self):
        """5px on a 4K panel is ~2mm of bevel face, which is 4-ply rag board."""
        assert Settings().bevel_px == 5

    def test_the_bevel_can_be_tuned_for_your_own_sofa(self, monkeypatch):
        monkeypatch.setenv("BEVEL_PX", "7")
        assert Settings.from_env().bevel_px == 7

    def test_the_bevel_never_disappears(self, monkeypatch):
        """A zero cut is a print floating on board with no bevel at all."""
        monkeypatch.setenv("BEVEL_PX", "0")
        assert Settings.from_env().bevel_px == 1

    def test_the_board_is_flat_by_default(self):
        """Mottling was tried on a real panel and read as a texture rather than
        as paper. A constant colour has nothing for JPEG to band, either."""
        assert Settings().mat_texture == 0.0
        assert Settings.from_env().mat_texture == 0.0

    def test_texture_can_be_turned_on(self, monkeypatch):
        monkeypatch.setenv("MAT_TEXTURE", "1.6")
        assert Settings.from_env().mat_texture == pytest.approx(1.6)

    def test_negative_texture_is_clamped_off(self, monkeypatch):
        monkeypatch.setenv("MAT_TEXTURE", "-3")
        assert Settings.from_env().mat_texture == 0.0

    def test_frame_size_is_read_as_two_values(self, monkeypatch):
        monkeypatch.setenv("FRAME_WIDTH", "1920")
        monkeypatch.setenv("FRAME_HEIGHT", "1080")
        assert Settings.from_env().frame_size == (1920, 1080)


class TestDerived:
    def test_paths_hang_off_cache_dir(self, tmp_path):
        s = Settings(cache_dir=tmp_path)
        assert s.plate_cache_dir == tmp_path / "plates"
        assert s.history_file == tmp_path / "history.json"
        assert s.tv_token_file == tmp_path / "tv-token.txt"
        assert s.tv_state_file == tmp_path / "tv-state.json"

    def test_stale_window_is_three_cycles(self):
        assert Settings(poll_seconds=60).stale_after_seconds == 180

    def test_stale_window_has_a_floor(self):
        """A 5s poll interval should not make a single slow request unhealthy."""
        assert Settings(poll_seconds=5).stale_after_seconds == 120

    def test_settings_are_immutable(self):
        s = Settings()
        with pytest.raises(AttributeError):
            s.min_confidence = 0.9  # type: ignore[misc]
