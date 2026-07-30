"""The gallery: filtering, de-duplication, health and the disk mirror."""

from __future__ import annotations

import json
import time

import pytest

from birdframe.gallery import Detection, Gallery, PollHealth

CARDINAL = ("Cardinalis cardinalis", "Northern Cardinal")


@pytest.mark.usefixtures("no_warm")
class TestRecordConfidence:
    def test_confident_detection_is_displayed(self, gallery):
        result = gallery.record(*CARDINAL, 0.94, "2026-07-27T10:00:00Z", "test")
        assert result.displayed
        assert gallery.stats.matched == 1

    def test_below_threshold_is_rejected(self, gallery):
        result = gallery.record(*CARDINAL, 0.2, "2026-07-27T10:00:00Z", "test")
        assert not result.displayed
        assert result.reason == "below_confidence_threshold"
        assert gallery.stats.rejected == 1

    def test_missing_confidence_is_rejected(self, gallery):
        """A payload with no confidence used to bypass the filter entirely.

        The old check was `if confidence and confidence < MIN`, so a parsed 0.0
        was falsy and went straight onto the wall unvetted.
        """
        result = gallery.record(*CARDINAL, None, "2026-07-27T10:00:00Z", "test")
        assert not result.displayed
        assert result.reason == "below_confidence_threshold"
        assert gallery.stats.rejected == 1

    def test_zero_confidence_is_rejected(self, gallery):
        result = gallery.record(*CARDINAL, 0.0, "2026-07-27T10:00:00Z", "test")
        assert not result.displayed
        assert gallery.stats.rejected == 1

    def test_exactly_at_threshold_is_accepted(self, gallery):
        result = gallery.record(*CARDINAL, 0.65, "2026-07-27T10:00:00Z", "test")
        assert result.displayed

    def test_received_counts_everything(self, gallery):
        gallery.record(*CARDINAL, 0.94, "a", "test")
        gallery.record(*CARDINAL, 0.10, "b", "test")
        gallery.record("Fakeus fakeus", "Fake Bird", 0.99, "c", "test")
        assert gallery.stats.received == 3
        assert gallery.stats.matched == 1
        assert gallery.stats.rejected == 1
        assert gallery.stats.unmatched == 1


@pytest.mark.usefixtures("no_warm")
class TestRecordMatching:
    def test_unmatched_species_is_not_displayed(self, gallery):
        result = gallery.record("Fakeus fakeus", "Fake Bird", 0.99, "t", "test")
        assert not result.displayed
        assert result.reason == "no_plate"
        assert gallery.snapshot() == []

    def test_unmatched_species_are_named_in_health(self, gallery):
        gallery.record("Fakeus fakeus", "Fake Bird", 0.99, "t", "test")
        assert "Fakeus fakeus" in gallery.health()["unmatched_species"]

    def test_unmatched_list_does_not_repeat(self, gallery):
        for i in range(5):
            gallery.record("Fakeus fakeus", "Fake Bird", 0.99, f"t{i}", "test")
        assert gallery.health()["unmatched_species"].count("Fakeus fakeus") == 1

    def test_common_name_used_when_scientific_is_blank(self, gallery):
        result = gallery.record("", "Wild Turkey", 0.9, "t", "test")
        assert result.displayed
        assert gallery.snapshot()[0].common_name == "Wild Turkey"

    def test_entry_carries_plate_metadata(self, gallery):
        gallery.record(*CARDINAL, 0.94, "t", "test")
        entry = gallery.snapshot()[0]
        assert entry.plate is not None
        assert entry.file_name
        assert entry.audubon_name
        assert entry.match == "curated"
        assert entry.confidence == pytest.approx(0.94)


@pytest.mark.usefixtures("no_warm")
class TestHistory:
    def test_newest_first(self, gallery):
        gallery.record(*CARDINAL, 0.9, "first", "test")
        gallery.record("Cyanocitta cristata", "Blue Jay", 0.9, "second", "test")
        assert gallery.snapshot()[0].detected_at == "second"

    def test_bounded_by_history_size(self, gallery):
        # settings.history_size is 5 in the fixture.
        for i in range(12):
            gallery.record(*CARDINAL, 0.9, f"t{i}", "test")
        assert len(gallery.snapshot()) == 5

    def test_snapshot_limit(self, gallery):
        for i in range(5):
            gallery.record(*CARDINAL, 0.9, f"t{i}", "test")
        assert len(gallery.snapshot(limit=2)) == 2


class TestDedupe:
    def test_first_sighting_is_new(self, gallery):
        key = gallery.dedupe_key("Cardinalis cardinalis", "Northern Cardinal", "t")
        assert not gallery.seen_before(key)

    def test_second_sighting_is_seen(self, gallery):
        key = gallery.dedupe_key("Cardinalis cardinalis", "Northern Cardinal", "t")
        gallery.seen_before(key)
        assert gallery.seen_before(key)

    def test_different_timestamp_is_new(self, gallery):
        a = gallery.dedupe_key("Cardinalis cardinalis", "", "t1")
        b = gallery.dedupe_key("Cardinalis cardinalis", "", "t2")
        gallery.seen_before(a)
        assert not gallery.seen_before(b)

    def test_set_stays_in_step_with_the_deque(self, gallery):
        """The lookup set must evict alongside the bounded deque.

        If it does not, it grows without limit for the life of the process.
        """
        maxlen = gallery._seen.maxlen
        assert maxlen is not None
        for i in range(maxlen + 50):
            gallery.seen_before(f"key-{i}")
        assert len(gallery._seen) == maxlen
        assert len(gallery._seen_set) == maxlen
        assert not gallery.seen_before("key-0")  # long since evicted


@pytest.mark.usefixtures("no_warm")
class TestPersistence:
    def test_round_trip(self, settings, index, gallery):
        gallery.record(*CARDINAL, 0.94, "2026-07-27T10:00:00Z", "test")
        gallery.record(
            "Cyanocitta cristata", "Blue Jay", 0.91, "2026-07-27T11:00:00Z", "test"
        )

        restored = Gallery(settings, index)
        restored.load()
        try:
            items = restored.snapshot()
            assert len(items) == 2
            # Order survives: newest first.
            assert items[0].common_name == "Blue Jay"
            assert items[1].common_name == "Northern Cardinal"
            assert items[0].plate is not None
        finally:
            restored.shutdown()

    def test_missing_file_is_fine(self, gallery):
        gallery.load()
        assert gallery.snapshot() == []

    def test_corrupt_json_is_survivable(self, settings, gallery, caplog):
        settings.history_file.parent.mkdir(parents=True, exist_ok=True)
        settings.history_file.write_text("{not json", encoding="utf-8")
        gallery.load()
        assert gallery.snapshot() == []
        assert "could not restore history" in caplog.text

    def test_non_list_payload_is_ignored(self, settings, gallery, caplog):
        settings.history_file.parent.mkdir(parents=True, exist_ok=True)
        settings.history_file.write_text('{"a": 1}', encoding="utf-8")
        gallery.load()
        assert gallery.snapshot() == []
        assert "not a list" in caplog.text

    def test_junk_entries_are_skipped(self, settings, gallery):
        settings.history_file.parent.mkdir(parents=True, exist_ok=True)
        settings.history_file.write_text(
            json.dumps(
                [
                    {"plate": 1, "common_name": "Wild Turkey", "file_name": "x.jpg"},
                    {"common_name": "No Plate"},  # unplated: not displayable
                    "junk",
                    None,
                    {"plate": "not an int"},
                ]
            ),
            encoding="utf-8",
        )
        gallery.load()
        items = gallery.snapshot()
        assert len(items) == 1
        assert items[0].plate == 1

    def test_load_respects_history_size(self, settings, gallery):
        settings.history_file.parent.mkdir(parents=True, exist_ok=True)
        settings.history_file.write_text(
            json.dumps(
                [
                    {"plate": 1, "common_name": f"B{i}", "file_name": "x.jpg"}
                    for i in range(30)
                ]
            ),
            encoding="utf-8",
        )
        gallery.load()
        assert len(gallery.snapshot()) == settings.history_size


class TestSeededBirdsAreDropped:
    """Demo seeding is gone, but birds it wrote are still on the cache volume and
    would otherwise restore and hang forever -- they round-trip faithfully."""

    def _write(self, settings, entries):
        settings.history_file.parent.mkdir(parents=True, exist_ok=True)
        settings.history_file.write_text(json.dumps(entries), encoding="utf-8")

    def test_demo_entries_do_not_come_back(self, settings, gallery):
        self._write(
            settings,
            [
                {
                    "plate": 1,
                    "common_name": "Real Bird",
                    "file_name": "x.jpg",
                    "source": "webhook",
                },
                {
                    "plate": 2,
                    "common_name": "Seeded Bird",
                    "file_name": "y.jpg",
                    "source": "demo",
                },
            ],
        )
        gallery.load()
        assert [d.common_name for d in gallery.snapshot()] == ["Real Bird"]

    def test_it_says_how_many_were_discarded(self, settings, gallery, caplog):
        caplog.set_level("INFO", logger="birdframe.gallery")
        self._write(
            settings,
            [
                {
                    "plate": i,
                    "common_name": f"Seeded {i}",
                    "file_name": "y.jpg",
                    "source": "demo",
                }
                for i in range(1, 4)
            ],
        )
        gallery.load()
        assert gallery.snapshot() == []
        assert "discarded 3 seeded" in caplog.text

    def test_other_sources_are_kept(self, settings, gallery):
        """Only "demo" goes. A missing source restores as "restored", which is a
        legacy file rather than a seeded bird."""
        self._write(
            settings,
            [
                {"plate": 1, "common_name": "A", "file_name": "x.jpg", "source": "poll"},
                {"plate": 2, "common_name": "B", "file_name": "y.jpg"},
            ],
        )
        gallery.load()
        assert len(gallery.snapshot()) == 2


class TestRevision:
    """The art driver has no rotation timer, so this counter is what tells it
    there is anything new to look at."""

    def test_it_starts_at_zero(self, gallery):
        assert gallery.revision == 0

    def test_a_recorded_bird_moves_it(self, gallery, no_warm):
        before = gallery.revision
        assert gallery.record(*CARDINAL, 0.94, "t", "test").displayed
        assert gallery.revision > before

    def test_an_unmatched_bird_does_not_move_it(self, gallery, no_warm):
        """Nothing was added to hang, so waking the driver would be a wasted
        compose -- and on a cold cache, a wasted download."""
        before = gallery.revision
        gallery.record("Nonexistentus fakeus", "Totally Fake Bird", 0.99, "t", "test")
        assert gallery.revision == before

    def test_a_rejected_bird_does_not_move_it(self, gallery, no_warm):
        before = gallery.revision
        gallery.record(*CARDINAL, 0.10, "t", "test")  # below the threshold
        assert gallery.revision == before

    def test_restoring_history_moves_it_once(self, settings, gallery):
        settings.history_file.parent.mkdir(parents=True, exist_ok=True)
        settings.history_file.write_text(
            json.dumps(
                [
                    {"plate": 1, "common_name": "A", "file_name": "x.jpg"},
                    {"plate": 2, "common_name": "B", "file_name": "y.jpg"},
                ]
            ),
            encoding="utf-8",
        )
        before = gallery.revision
        gallery.load()
        assert gallery.revision == before + 1

    def test_it_only_ever_increases(self, gallery, no_warm):
        seen = [gallery.revision]
        for i in range(6):  # more than history_size, so the deque drops entries
            gallery.record(*CARDINAL, 0.94, f"t{i}", "test")
            seen.append(gallery.revision)
        assert seen == sorted(seen)
        assert len(set(seen)) == len(seen)


class TestDetectionSerialisation:
    def test_unplated_entry_omits_plate_keys(self):
        d = Detection("Sci", "Common", 0.9, "t", "test")
        assert "plate" not in d.as_dict()
        assert not d.displayable

    def test_plated_entry_round_trips(self):
        d = Detection(
            "Sci", "Common", 0.9, "t", "test", 1, "x.jpg", "Old Name", "curated"
        )
        rebuilt = Detection.from_dict(d.as_dict())
        assert rebuilt == d

    def test_from_dict_rejects_unplated(self):
        assert Detection.from_dict({"common_name": "x"}) is None

    def test_from_dict_drops_non_numeric_confidence(self):
        d = Detection.from_dict({"plate": 1, "confidence": "high"})
        assert d is not None
        assert d.confidence is None


class TestPollHealth:
    STALE = 180.0

    def test_disabled_poller_is_always_healthy(self):
        assert PollHealth(enabled=False).is_healthy(self.STALE)

    def test_enabled_but_not_yet_run_is_healthy(self):
        """Startup grace: the first poll has not happened yet."""
        assert PollHealth(enabled=True).is_healthy(self.STALE)

    def test_failing_before_any_success_is_unhealthy(self):
        h = PollHealth(enabled=True, consecutive_failures=1)
        assert not h.is_healthy(self.STALE)

    def test_recent_success_is_healthy(self):
        h = PollHealth(enabled=True, last_ok=time.monotonic())
        assert h.is_healthy(self.STALE)

    def test_stale_success_is_unhealthy(self):
        h = PollHealth(enabled=True, last_ok=time.monotonic() - 600)
        assert not h.is_healthy(self.STALE)

    def test_age_is_none_before_first_success(self):
        assert PollHealth(enabled=True).age() is None
