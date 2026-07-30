"""The art driver: what it sends the Frame, and what it does when the TV is off.

A real Frame is not available in CI, so FrameTV is the seam: it is the only
class that touches the network, and every test here swaps it for a recorder.
"""

from __future__ import annotations

import json
import threading
import time
from typing import cast

import pytest

from birdframe.config import Settings
from birdframe.gallery import Gallery
from birdframe.plates import PlateIndex
from birdframe.tv import RETRY_SECONDS, ArtDriver, FrameTV, TVError


class FakeTV:
    """Stands in for FrameTV. Records calls; can be told to fail."""

    def __init__(self):
        self.uploaded: list[bytes] = []
        self.selected: list[str] = []
        self.deleted: list[str] = []
        self.art_mode = False
        self.art_mode_calls: list[bool] = []
        self.fail_on: str | None = None
        self._next_id = 0

    def _maybe_fail(self, what):
        if self.fail_on == what:
            raise TVError(f"{what} failed: pretend the TV is unplugged")

    def upload(self, jpeg):
        self._maybe_fail("upload")
        self.uploaded.append(jpeg)
        self._next_id += 1
        return f"id-{self._next_id}"

    def select(self, content_id):
        self._maybe_fail("select")
        self.selected.append(content_id)

    def delete(self, content_ids):
        self._maybe_fail("delete")
        self.deleted.extend(content_ids)

    def set_art_mode(self, on):
        self._maybe_fail("set_art_mode")
        self.art_mode = on
        self.art_mode_calls.append(on)

    def art_mode_on(self):
        self._maybe_fail("art_mode_on")
        return self.art_mode

    def close(self):
        pass


@pytest.fixture
def tv_settings(tmp_path):
    return Settings(
        cache_dir=tmp_path / "cache",
        tv_host="10.0.0.5",
        # Distinct from RETRY_SECONDS (60), so a test asserting on one cannot pass
        # because it happened to match the other.
        art_check_seconds=90,
        tv_keep_uploads=2,
        # Small canvas: these tests care about the calls, not the pixels, and a
        # 4K render per test is a slow way to learn nothing.
        frame_size=(384, 216),
    )


@pytest.fixture
def driver(tv_settings, monkeypatch, tmp_path):
    """A driver with a fake TV and a real plate on disk per species.

    One file per plate number, not one shared file: the wall only changes when
    the plates do, so tests that mean "a different bird" have to produce a
    different picture or they prove nothing. Odd plates are portrait and even
    ones landscape, so pairing is exercised deliberately.
    """
    from PIL import Image

    lock = threading.Lock()

    def cached(self, plate, file_name):
        path = tmp_path / f"plate-{plate}.jpg"
        # Serialised and written atomically, exactly as the real ensure_cached is:
        # recording a bird warms the cache on a thread pool, so this is called
        # from the warmer and the test thread at once. Writing in place let them
        # interleave into a torn JPEG, which aspect_ratio could not measure - it
        # fell back to "portrait" and landscape plates silently paired up.
        with lock:
            if not path.exists():
                size = (800, 1000) if plate % 2 else (1700, 1000)
                tmp = path.with_suffix(".part")
                Image.new("RGB", size, (170, 130, 80)).save(tmp, format="JPEG")
                tmp.replace(path)
        return path

    monkeypatch.setattr(PlateIndex, "ensure_cached", cached)

    index = PlateIndex(tv_settings)
    gallery = Gallery(tv_settings, index)
    d = ArtDriver(tv_settings, gallery, index)
    d.tv = cast(FrameTV, FakeTV())
    yield d
    gallery.shutdown()


# Two species with portrait plates (odd numbers), so they pair with each other
# rather than either hanging alone.
CARDINAL = ("Cardinalis cardinalis", "Northern Cardinal")  # plate 159
BLUEJAY = ("Cyanocitta cristata", "Blue Jay")  # plate 102, landscape
FLICKER = ("Colaptes auratus", "Northern Flicker")  # plate 37, portrait


def heard(driver, name="Cardinalis cardinalis", common="Northern Cardinal", when="t"):
    return driver.gallery.record(name, common, 0.94, when, "test")


class TestToggle:
    def test_starts_off_by_default(self, driver):
        assert driver.enabled is False

    def test_art_on_start_starts_on(self, tmp_path):
        settings = Settings(cache_dir=tmp_path / "c", art_on_start=True)
        index = PlateIndex(settings)
        gallery = Gallery(settings, index)
        try:
            assert ArtDriver(settings, gallery, index).enabled is True
        finally:
            gallery.shutdown()

    def test_setting_it_wakes_the_thread(self, driver):
        driver._wake.clear()
        driver.set_enabled(True)
        assert driver.enabled is True
        assert driver._wake.is_set()

    def test_setting_the_same_value_is_a_no_op(self, driver):
        driver._wake.clear()
        driver.set_enabled(False)
        assert not driver._wake.is_set()

    def test_the_toggle_survives_a_restart(self, driver, tv_settings):
        """Otherwise a container update silently turns the wall off."""
        driver.set_enabled(True)
        revived = ArtDriver(tv_settings, driver.gallery, driver.index)
        assert revived.enabled is True

    def test_a_corrupt_state_file_is_ignored(self, driver, tv_settings):
        tv_settings.tv_state_file.parent.mkdir(parents=True, exist_ok=True)
        tv_settings.tv_state_file.write_text("{not json", encoding="utf-8")
        assert ArtDriver(tv_settings, driver.gallery, driver.index).enabled is False


class TestPush:
    def test_a_tick_uploads_selects_and_shows(self, driver):
        heard(driver)
        driver.set_enabled(True)
        driver._tick()

        assert len(driver.tv.uploaded) == 1
        assert driver.tv.selected == ["id-1"]
        assert driver.tv.art_mode is True

    def test_what_is_uploaded_is_a_jpeg(self, driver):
        heard(driver)
        driver.set_enabled(True)
        driver._tick()
        assert driver.tv.uploaded[0].startswith(b"\xff\xd8\xff")

    def test_the_preview_is_the_image_that_was_sent(self, driver):
        """The page must not show a second, differently-rendered mat."""
        heard(driver)
        driver.set_enabled(True)
        driver._tick()
        assert driver.preview() == driver.tv.uploaded[0]

    def test_art_mode_is_not_re_asserted_when_already_on(self, driver):
        """The second tick finds the same birds already hanging, so it is only a
        TV check -- it used to be "the rotation is not due yet"."""
        heard(driver)
        driver.set_enabled(True)
        driver._tick()
        driver._tick()
        assert driver.tv.art_mode_calls == [True]

    def test_status_names_what_is_hanging(self, driver):
        heard(driver)
        driver.set_enabled(True)
        driver._tick()
        status = driver.status()
        assert status["showing"][0]["common_name"] == "Northern Cardinal"
        assert status["last_push"]
        assert status["last_error"] is None

    def test_an_empty_gallery_hangs_a_bare_mat(self, driver):
        driver.set_enabled(True)
        driver._tick()
        assert driver.status()["showing"] == []
        assert len(driver.tv.uploaded) == 1  # board, no print

    def test_the_newest_bird_is_the_one_that_hangs(self, driver):
        """The whole point of the wall: what was heard most recently leads."""
        heard(driver, *CARDINAL, when="t1")
        heard(driver, *FLICKER, when="t2")
        driver.set_enabled(True)
        driver._tick()
        assert driver.status()["showing"][0]["common_name"] == "Northern Flicker"

    def test_a_portrait_pairs_with_the_next_newest_portrait(self, driver):
        heard(driver, *CARDINAL, when="t1")
        heard(driver, *FLICKER, when="t2")
        driver.set_enabled(True)
        driver._tick()
        names = [s["common_name"] for s in driver.status()["showing"]]
        assert names == ["Northern Flicker", "Northern Cardinal"]

    def test_a_landscape_bird_hangs_alone(self, driver):
        heard(driver, *CARDINAL, when="t1")
        heard(driver, *BLUEJAY, when="t2")
        driver.set_enabled(True)
        driver._tick()
        assert [s["common_name"] for s in driver.status()["showing"]] == ["Blue Jay"]

    def test_disabled_never_uploads(self, driver):
        heard(driver)
        driver._tick()
        assert driver.tv.uploaded == []


def hang_new_bird(driver, index):
    """Hear a bird nothing else in the suite uses, and tick.

    Uploads only happen when the plates change, so a test that wants N uploads
    has to supply N genuinely different birds.
    """
    species = [
        ("Cardinalis cardinalis", "Northern Cardinal"),
        ("Colaptes auratus", "Northern Flicker"),
        ("Zenaida macroura", "Mourning Dove"),
        ("Baeolophus bicolor", "Tufted Titmouse"),
        ("Sitta carolinensis", "White-breasted Nuthatch"),
        ("Cyanocitta cristata", "Blue Jay"),
    ]
    name, common = species[index % len(species)]
    heard(driver, name, common, when=f"t{index}")
    return driver._tick()


class TestHousekeeping:
    def test_old_uploads_are_deleted_once_over_the_cap(self, driver):
        """The Frame's storage is finite; without this it fills with old birds."""
        driver.set_enabled(True)
        hang_new_bird(driver, 0)
        hang_new_bird(driver, 1)
        assert driver.tv.deleted == []  # cap is 2, still inside it
        hang_new_bird(driver, 2)
        assert driver.tv.deleted == ["id-1"]

    def test_the_showing_image_is_never_the_one_deleted(self, driver):
        driver.set_enabled(True)
        for i in range(4):
            hang_new_bird(driver, i)
        assert driver.tv.selected[-1] not in driver.tv.deleted

    def test_a_failed_compose_does_not_bump_the_generation(self, driver, monkeypatch):
        """A bumped generation tells the page there is a new image to fetch."""
        import birdframe.tv as tv_module

        before = driver.status()["generation"]
        monkeypatch.setattr(
            tv_module, "render_jpeg", lambda *a, **k: (_ for _ in ()).throw(OSError("x"))
        )
        driver.set_enabled(True)
        driver._tick()
        assert driver.status()["generation"] == before
        assert "compose failed" in driver.status()["last_error"]

    def test_a_failed_compose_uploads_nothing_and_is_retried(self, driver, monkeypatch):
        """_preview still holds the previous wall, so pushing would put the old
        image up and then record the new one as hanging."""
        import birdframe.tv as tv_module

        real_render = tv_module.render_jpeg
        broken = True

        def flaky(*args, **kwargs):
            if broken:
                raise OSError("x")
            return real_render(*args, **kwargs)

        monkeypatch.setattr(tv_module, "render_jpeg", flaky)

        heard(driver)
        driver.set_enabled(True)
        assert driver._tick() == pytest.approx(RETRY_SECONDS, abs=2)
        assert driver.tv.uploaded == []

        broken = False
        driver._tick()
        assert len(driver.tv.uploaded) == 1

    def test_a_failed_delete_does_not_fail_the_push(self, driver):
        driver.set_enabled(True)
        hang_new_bird(driver, 0)
        hang_new_bird(driver, 1)
        driver.tv.fail_on = "delete"
        hang_new_bird(driver, 2)
        assert driver.status()["last_error"] is None
        assert len(driver.tv.selected) == 3

    def test_the_upload_list_survives_a_restart(self, driver, tv_settings):
        heard(driver)
        driver.set_enabled(True)
        driver._tick()
        revived = ArtDriver(tv_settings, driver.gallery, driver.index)
        assert list(revived._uploads) == ["id-1"]

    def test_state_on_disk_is_json(self, driver, tv_settings):
        driver.set_enabled(True)
        body = json.loads(tv_settings.tv_state_file.read_text(encoding="utf-8"))
        assert body["enabled"] is True


class TestFailure:
    def test_an_unreachable_tv_is_reported_not_raised(self, driver):
        heard(driver)
        driver.set_enabled(True)
        driver.tv.fail_on = "upload"
        delay = driver._tick()
        assert "unplugged" in driver.status()["last_error"]
        assert delay == pytest.approx(RETRY_SECONDS, abs=2)

    def test_a_failed_push_is_retried_and_then_clears(self, driver):
        """A failure must not be recorded as hung: with no rotation to come round
        again, latching here would leave the wall wrong until the next bird."""
        heard(driver)
        driver.set_enabled(True)
        driver.tv.fail_on = "upload"
        assert driver._tick() == pytest.approx(RETRY_SECONDS, abs=2)
        assert driver.tv.uploaded == []

        driver.tv.fail_on = None
        driver._tick()  # same bird, but nothing was ever hung
        assert len(driver.tv.uploaded) == 1
        assert driver.status()["last_error"] is None


class TestSurrender:
    """Somebody picked up the remote. The Frame's power button switches between
    the TV and Art Mode, so this happens every time anyone watches something."""

    @pytest.fixture
    def holding(self, driver, monkeypatch):
        """A driver that has the panel, past the settle grace period."""
        import birdframe.tv as tv_module

        monkeypatch.setattr(tv_module, "TAKEOVER_GRACE_SECONDS", 0)
        heard(driver)
        driver.set_enabled(True)
        driver._tick()
        assert driver.tv.art_mode is True
        return driver

    def test_leaving_art_mode_turns_the_switch_off(self, holding):
        holding.tv.art_mode = False  # the remote, not us
        holding._tick()
        assert holding.enabled is False

    def test_it_does_not_drag_the_tv_back(self, holding):
        """The actual bug: hanging the next bird used to call set_artmode(True)
        and pull the panel off whatever somebody was watching.

        A bird is heard first, so there genuinely is something waiting to hang on
        this tick -- otherwise the test would pass on an empty gallery and stop
        guarding the order of the surrender check against the change check.
        """
        holding.tv.art_mode = False
        heard(holding, *FLICKER, when="t2")  # something new, waiting to go up
        before = len(holding.tv.uploaded)
        holding._tick()
        assert holding.tv.art_mode is False
        assert len(holding.tv.uploaded) == before

    def test_the_page_is_told_why(self, holding):
        holding.tv.art_mode = False
        holding._tick()
        status = holding.status()
        assert status["off_reason"] == "switched off at the TV"
        assert status["last_error"] is None  # this is not a fault

    def test_it_does_not_then_tell_the_tv_to_turn_off(self, holding):
        """Art mode is already off. Sending set_artmode(False) after the user
        switched to a TV input is a stray command at best."""
        holding.tv.art_mode = False
        holding._tick()
        holding._tick()
        assert holding.tv.art_mode_calls.count(False) == 0

    def test_the_switch_stays_off_across_a_restart(self, holding, tv_settings):
        holding.tv.art_mode = False
        holding._tick()
        revived = ArtDriver(tv_settings, holding.gallery, holding.index)
        assert revived.enabled is False

    def test_turning_it_back_on_clears_the_reason(self, holding):
        holding.tv.art_mode = False
        holding._tick()
        holding.set_enabled(True)
        assert holding.status()["off_reason"] is None

    def test_an_unreachable_tv_does_not_flip_the_switch(self, holding):
        """A Frame switched off at the wall, or a dropped websocket, is an
        outage to retry - not an instruction to give up."""
        holding.tv.fail_on = "art_mode_on"
        holding._tick()
        assert holding.enabled is True
        assert holding.status()["off_reason"] is None

    def test_art_mode_still_on_is_left_alone(self, holding):
        holding._tick()
        assert holding.enabled is True

    def test_nothing_is_checked_before_the_first_push(self, driver):
        """Art mode reads "off" until we put something up. Treating that as a
        takeover would turn the switch off the instant it was turned on."""
        driver.tv.art_mode = False
        driver.set_enabled(True)
        assert driver._surrendered() is False

    def test_the_grace_period_covers_a_slow_panel(self, driver):
        """The TV does not report the new mode instantly."""
        heard(driver)
        driver.set_enabled(True)
        driver._tick()
        driver.tv.art_mode = False  # as if the panel had not caught up yet
        assert driver._surrendered() is False

    def test_no_tv_configured_never_surrenders(self, tmp_path):
        settings = Settings(cache_dir=tmp_path / "c", frame_size=(64, 36))
        index = PlateIndex(settings)
        gallery = Gallery(settings, index)
        try:
            d = ArtDriver(settings, gallery, index)
            d._holding = True  # cannot actually happen without a TV
            assert d._surrendered() is False
        finally:
            gallery.shutdown()


class TestChangeDetection:
    """The wall changes when the gallery does, never on a timer. Every upload is
    a ~1.5MB write to the TV's flash, so sending an identical image is not a
    harmless no-op."""

    def test_it_wakes_often_enough_to_notice_the_remote(self, driver):
        heard(driver)
        driver.set_enabled(True)
        delay = driver._tick()
        assert delay <= driver.settings.art_check_seconds

    def test_an_unchanged_gallery_does_not_re_upload(self, driver):
        heard(driver)
        driver.set_enabled(True)
        driver._tick()
        driver._tick()  # nothing new heard, so this is only a TV check
        assert len(driver.tv.uploaded) == 1

    def test_the_same_bird_heard_again_does_not_re_upload(self, driver):
        """One loud bird near the microphone is heard over and over. Each is a
        separate detection with the same plate, so gating on "did the gallery
        change" alone would rewrite the identical image every few minutes."""
        heard(driver, *CARDINAL, when="t1")
        driver.set_enabled(True)
        driver._tick()
        heard(driver, *CARDINAL, when="t2")  # same species, new detection
        driver._tick()
        assert len(driver.tv.uploaded) == 1

    def test_a_new_bird_hangs_straight_away(self, driver):
        heard(driver, *CARDINAL, when="t1")
        driver.set_enabled(True)
        driver._tick()
        heard(driver, *BLUEJAY, when="t2")
        driver._tick()
        assert len(driver.tv.uploaded) == 2

    def test_a_bare_mat_is_hung_only_once(self, driver):
        """Nothing heard yet. "The mat is up" and "nothing has been hung" have to
        be different states, or an empty gallery re-uploads board forever."""
        driver.set_enabled(True)
        for _ in range(3):
            driver._tick()
        assert len(driver.tv.uploaded) == 1

    def test_an_unfetchable_plate_is_retried_not_latched(self, driver, monkeypatch):
        """A cold cache after a restart must not leave the wall bare until the
        next bird sings -- that is the very thing restoring history prevents."""
        heard(driver)
        driver.set_enabled(True)

        # Fail the fetch by pointing the driver's own resolver at nothing, rather
        # than re-patching ensure_cached: undoing that would also undo the
        # fixture's patch and send the retry at the real network.
        working, driver._path_for = driver._path_for, lambda detection: None
        assert driver._tick() == pytest.approx(RETRY_SECONDS, abs=2)
        assert driver.tv.uploaded == []

        driver._path_for = working
        driver._tick()
        assert len(driver.tv.uploaded) == 1
        assert driver.status()["showing"][0]["common_name"] == "Northern Cardinal"

    def test_turning_it_on_hangs_something_immediately(self, driver):
        heard(driver)
        driver._tick()  # disabled; goes dark
        driver.set_enabled(True)
        driver._tick()
        assert driver.tv.uploaded

    def test_toggling_off_and_on_hangs_again_with_no_new_bird(self, driver):
        """Going dark takes the TV out of art mode, so what we last hung is no
        longer on the panel. Without forgetting it, the switch would appear to do
        nothing at all -- and the switch is the whole UI."""
        heard(driver)
        driver.set_enabled(True)
        driver._tick()
        assert len(driver.tv.uploaded) == 1

        driver.set_enabled(False)
        driver._tick()
        driver.set_enabled(True)
        driver._tick()
        assert len(driver.tv.uploaded) == 2
        assert driver.tv.art_mode is True

    def test_a_bird_heard_while_off_hangs_when_turned_on(self, driver):
        driver.set_enabled(False)
        heard(driver)
        driver._tick()
        assert driver.tv.uploaded == []
        driver.set_enabled(True)
        driver._tick()
        assert len(driver.tv.uploaded) == 1


class TestGoingDark:
    def test_turning_off_leaves_art_mode(self, driver):
        heard(driver)
        driver.set_enabled(True)
        driver._tick()
        driver.set_enabled(False)
        driver._tick()
        assert driver.tv.art_mode is False

    def test_it_only_says_so_once(self, driver):
        """A disabled driver still wakes on every detection; it must not send
        set_artmode(off) to the TV each time."""
        driver.set_enabled(True)
        driver._tick()
        driver.set_enabled(False)
        for _ in range(3):
            driver._tick()
        assert driver.tv.art_mode_calls.count(False) == 1

    def test_a_tv_that_is_already_off_is_not_an_error_loop(self, driver):
        driver.set_enabled(True)
        driver._tick()
        driver.tv.fail_on = "set_art_mode"
        driver.set_enabled(False)
        driver._tick()
        driver._tick()
        assert driver._settled is True


class TestWithoutATV:
    """TV_HOST unset: the composition still runs so the preview page works."""

    @pytest.fixture
    def headless(self, tmp_path, monkeypatch):
        from PIL import Image

        plate = tmp_path / "plate.jpg"
        Image.new("RGB", (800, 1000), (170, 130, 80)).save(plate, format="JPEG")
        monkeypatch.setattr(PlateIndex, "ensure_cached", lambda self, n, name: plate)

        settings = Settings(cache_dir=tmp_path / "cache", frame_size=(384, 216))
        index = PlateIndex(settings)
        gallery = Gallery(settings, index)
        d = ArtDriver(settings, gallery, index)
        d.tv = cast(FrameTV, FakeTV())
        yield d
        gallery.shutdown()

    def test_nothing_is_sent(self, headless):
        headless.gallery.record(
            "Cardinalis cardinalis", "Northern Cardinal", 0.94, "t", "test"
        )
        headless.set_enabled(True)
        headless._tick()
        assert headless.tv.uploaded == []

    def test_but_the_wall_is_still_composed(self, headless):
        headless.gallery.record(
            "Cardinalis cardinalis", "Northern Cardinal", 0.94, "t", "test"
        )
        headless.set_enabled(True)
        headless._tick()
        assert headless.preview().startswith(b"\xff\xd8\xff")

    def test_a_new_bird_is_visible_to_the_page(self, headless):
        """last_push never moves without a TV, so the preview has to be keyed on
        the compose instead or the page shows one image forever."""
        headless.set_enabled(True)
        headless._tick()  # bare mat
        first = headless.status()["generation"]
        headless.gallery.record(
            "Cardinalis cardinalis", "Northern Cardinal", 0.94, "t", "test"
        )
        headless._tick()
        assert first > 0
        assert headless.status()["last_push"] is None
        assert headless.status()["generation"] > first

    def test_status_says_it_is_unconfigured(self, headless):
        assert headless.status()["configured"] is False
        assert headless.status()["host"] is None


class FakeArtChannel:
    """The bit of samsungtvws' SamsungTVArt that FrameTV actually calls."""

    def __init__(self, *, frame=True, raises=None):
        self._frame = frame
        self._raises = raises
        self.closed = 0
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def supported(self):
        return self._frame

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        if self._raises:
            raise self._raises

    def upload(self, data, **kwargs):
        self._record("upload", data, **kwargs)
        return "content-1"

    def select_image(self, content_id, **kwargs):
        self._record("select_image", content_id, **kwargs)

    def delete_list(self, ids):
        self._record("delete_list", ids)

    def set_artmode(self, mode):
        self._record("set_artmode", mode)

    def get_artmode(self):
        self._record("get_artmode")
        return "on"

    def close(self):
        self.closed += 1


@pytest.fixture
def wired(tv_settings, monkeypatch):
    """A real FrameTV with samsungtvws swapped for a fake, and the channel it got."""
    import samsungtvws

    channels: list[FakeArtChannel] = []

    def install(channel):
        class FakeTVWS:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def art(self):
                channels.append(channel)
                return channel

        monkeypatch.setattr(samsungtvws, "SamsungTVWS", FakeTVWS)
        return FrameTV(tv_settings), channels

    return install


class TestFrameTV:
    """The only class that touches the network. Everything above it is a fake,
    so this is the one place the samsungtvws call shapes are pinned."""

    def test_upload_passes_jpeg_and_no_matte(self, wired):
        """We drew our own mat; a Samsung matte on top would double it up."""
        tv, _ = wired(FakeArtChannel())
        assert tv.upload(b"\xff\xd8jpeg") == "content-1"
        _, args, kwargs = tv._art.calls[0]
        assert args[0] == b"\xff\xd8jpeg"
        assert kwargs["file_type"] == "jpg"
        assert kwargs["matte"] == "none"
        assert kwargs["portrait_matte"] == "none"

    def test_select_shows_immediately(self, wired):
        tv, _ = wired(FakeArtChannel())
        tv.select("content-1")
        assert tv._art.calls[0] == ("select_image", ("content-1",), {"show": True})

    def test_art_mode_reads_the_on_off_string(self, wired):
        """get_artmode returns "on"/"off", not a bool."""
        tv, _ = wired(FakeArtChannel())
        assert tv.art_mode_on() is True

    def test_a_non_frame_tv_is_refused_with_a_readable_reason(self, wired):
        tv, _ = wired(FakeArtChannel(frame=False))
        with pytest.raises(TVError, match="does not support Art Mode"):
            tv.set_art_mode(True)

    def test_the_token_file_directory_is_created(self, wired, tv_settings):
        """The cache volume is empty on a first run, and samsungtvws will not
        make the directory for us."""
        tv, _ = wired(FakeArtChannel())
        tv.art_mode_on()
        assert tv_settings.tv_token_file.parent.is_dir()

    def test_a_library_error_becomes_a_tv_error(self, wired):
        """Callers only ever catch TVError; a raw socket error escaping here
        would take the driver thread down."""
        tv, _ = wired(FakeArtChannel(raises=OSError("connection reset")))
        with pytest.raises(TVError, match="connection reset"):
            tv.select("content-1")

    def test_a_failure_drops_the_connection_so_the_next_call_reconnects(self, wired):
        """Retrying down a half-dead websocket fails forever."""
        tv, channels = wired(FakeArtChannel(raises=OSError("boom")))
        with pytest.raises(TVError):
            tv.select("x")
        assert tv._art is None
        with pytest.raises(TVError):
            tv.select("x")
        assert len(channels) == 2  # it asked for a fresh channel

    def test_a_healthy_connection_is_reused(self, wired):
        tv, channels = wired(FakeArtChannel())
        tv.art_mode_on()
        tv.art_mode_on()
        assert len(channels) == 1

    def test_an_upload_with_no_content_id_is_an_error(self, wired, monkeypatch):
        channel = FakeArtChannel()
        monkeypatch.setattr(channel, "upload", lambda data, **kw: None)
        tv, _ = wired(channel)
        with pytest.raises(TVError, match="no content id"):
            tv.upload(b"x")

    def test_deleting_nothing_does_not_call_the_tv(self, wired):
        tv, channels = wired(FakeArtChannel())
        tv.delete([])
        assert channels == []

    def test_close_is_safe_when_never_connected(self, wired):
        tv, _ = wired(FakeArtChannel())
        tv.close()
        tv.close()


class TestThread:
    def test_start_and_stop(self, driver):
        driver.start()
        assert driver._thread is not None
        driver.stop(timeout=5)
        assert driver._thread is None

    def test_starting_twice_makes_one_thread(self, driver):
        driver.start()
        first = driver._thread
        driver.start()
        try:
            assert driver._thread is first
        finally:
            driver.stop(timeout=5)

    def test_the_thread_hangs_the_wall_when_enabled(self, driver):
        """End to end through the real loop rather than a hand-called _tick."""
        heard(driver)
        driver.start()
        try:
            driver.set_enabled(True)
            for _ in range(100):
                if driver.tv.uploaded:
                    break
                time.sleep(0.05)
            assert driver.tv.uploaded
        finally:
            driver.stop(timeout=5)

    def test_the_thread_does_not_re_upload_while_idle(self, driver):
        """The guarantee, through the real loop: with nothing new heard, the wall
        is left alone. Any tick path that uploads unconditionally shows up here as
        a second write to the TV's flash."""
        heard(driver)
        driver.start()
        try:
            driver.set_enabled(True)
            for _ in range(100):
                if driver.tv.uploaded:
                    break
                time.sleep(0.05)
            assert len(driver.tv.uploaded) == 1

            # Poke the loop repeatedly. Waking is not a reason to re-hang.
            for _ in range(5):
                driver.wake()
                time.sleep(0.05)
            assert len(driver.tv.uploaded) == 1
        finally:
            driver.stop(timeout=5)


class TestGalleryHook:
    def test_a_new_bird_wakes_the_driver(self, driver):
        """Otherwise a bird heard at 06:02 waits out the art-mode check interval
        before it reaches the wall."""
        driver.gallery.on_change = driver.wake
        driver._wake.clear()
        heard(driver)
        assert driver._wake.is_set()

    def test_a_listener_that_raises_does_not_break_the_webhook(self, driver):
        def boom():
            raise RuntimeError("nope")

        driver.gallery.on_change = boom
        assert heard(driver).displayed is True

    def test_an_unmatched_bird_does_not_wake_it(self, driver):
        driver.gallery.on_change = driver.wake
        driver._wake.clear()
        driver.gallery.record("Fakeus fakeus", "Fake Bird", 0.99, "t", "test")
        assert not driver._wake.is_set()
