"""Plate resolution.

The project's core promise is that it never puts the wrong bird on the wall, so
the negative cases here matter more than the positive ones.
"""

from __future__ import annotations

import json

import pytest
import requests
import responses

from birdframe.plates import PlateIndex, bucket_for, normalise


class TestNormalise:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Northern Cardinal", "northern cardinal"),
            ("  Blue   Jay  ", "blue jay"),
            ("Bachman's Sparrow", "bachman s sparrow"),
            ("Bachman’s Sparrow", "bachman s sparrow"),  # curly apostrophe
            ("Black & White Warbler", "black and white warbler"),
            ("Red-winged Blackbird", "red winged blackbird"),
            ("Plate 42!", "plate"),
        ],
    )
    def test_folding(self, raw, expected):
        assert normalise(raw) == expected

    def test_curly_and_straight_apostrophes_agree(self):
        assert normalise("Bachman's") == normalise("Bachman’s")


class TestBucketFor:
    @pytest.mark.parametrize(
        ("plate", "expected"),
        [
            (1, "1-99"),
            (99, "1-99"),
            (100, "100-199"),
            (199, "100-199"),
            (200, "200-299"),
            (300, "300-399"),
            (399, "300-399"),
            (400, "400-435"),
            (435, "400-435"),
        ],
    )
    def test_boundaries(self, plate, expected):
        assert bucket_for(plate) == expected


class TestResolve:
    def test_curated_scientific_name(self, index):
        m = index.resolve("Sitta carolinensis", "White-breasted Nuthatch")
        assert m is not None
        assert m.match == "curated"
        assert m.plate == 152

    def test_curated_is_case_and_space_insensitive(self, index):
        m = index.resolve("  sitta CAROLINENSIS  ", None)
        assert m is not None
        assert m.plate == 152

    def test_curated_beats_common_name(self, index):
        """A curated scientific hit must win even if the common name also matches.

        Audubon's own name for the bird is the authoritative one, and the common
        name path is the looser of the two.
        """
        m = index.resolve("Sitta carolinensis", "Wild Turkey")
        assert m is not None
        assert m.match == "curated"
        assert m.plate == 152

    def test_common_name_fallback(self, index):
        m = index.resolve(None, "Wild Turkey")
        assert m is not None
        assert m.match == "name"
        assert m.plate == 1

    def test_common_name_normalised(self, index):
        m = index.resolve(None, "  wild   turkey ")
        assert m is not None
        assert m.plate == 1

    def test_audubon_name_comes_from_curated_entry(self, index):
        m = index.resolve("Sitta carolinensis", None)
        assert m is not None
        # The 1830s name, not the modern one -- that is the whole point of the map.
        assert m.audubon_name != "White-breasted Nuthatch"
        assert m.audubon_name

    # --- the negative cases: no guessing

    @pytest.mark.parametrize(
        ("scientific", "common"),
        [
            (None, None),
            ("", ""),
            ("Nonexistentus fakeus", "Totally Fake Bird"),
            # Near-miss on a real name: fuzzy matching would have taken this.
            (None, "Golden-winged Woodpecker Junior"),
            (None, "Northern Cardinals"),
        ],
    )
    def test_unmatched_returns_none(self, index, scientific, common):
        assert index.resolve(scientific, common) is None

    def test_no_fuzzy_match_on_golden_winged_woodpecker(self, index):
        """The documented failure that motivated dropping fuzzy matching.

        'Golden-winged Woodpecker' is Audubon's name for a Northern Flicker;
        fuzzy matching sent it to Golden-naped Woodpecker.
        """
        m = index.resolve(None, "Golden-naped Woodpecker")
        if m is not None:
            assert "golden-naped" in m.audubon_name.lower()

    def test_curated_entry_pointing_at_a_missing_plate(self, settings, tmp_path, caplog):
        data = tmp_path / "data"
        data.mkdir()
        (data / "plates.json").write_text(
            json.dumps(
                [
                    {
                        "plate": 1,
                        "name": "Wild Turkey",
                        "fileName": "plate-1-wild-turkey.jpg",
                    }
                ]
            ),
            encoding="utf-8",
        )
        (data / "curated_map.json").write_text(
            json.dumps({"map": {"Fakeus fakeus": {"plate": 9999, "audubon": "Nope"}}}),
            encoding="utf-8",
        )
        idx = PlateIndex(settings, data_dir=data)
        assert idx.resolve("Fakeus fakeus", None) is None
        assert "not in plates.json" in caplog.text


class TestIndexIntegrity:
    def test_every_curated_plate_exists(self, index):
        """Guards the hand-maintained map against typos.

        A bad plate number here is a silently blank wall, so it should fail here
        rather than at 6am when the bird sings.
        """
        missing = {
            sci: entry["plate"]
            for sci, entry in index.by_scientific.items()
            if entry["plate"] not in index.plates
        }
        assert not missing

    def test_plate_count(self, index):
        assert len(index.plates) == 435

    def test_curated_map_is_populated(self, index):
        assert len(index.by_scientific) > 200


class TestEnsureCached:
    @responses.activate
    def test_downloads_once_then_serves_from_disk(self, index):
        url = f"{index.settings.image_repo}/1-99/plate-1-wild-turkey.jpg"
        responses.add(responses.GET, url, body=b"jpegbytes", status=200)

        first = index.ensure_cached(1, "plate-1-wild-turkey.jpg")
        assert first is not None
        assert first.read_bytes() == b"jpegbytes"

        second = index.ensure_cached(1, "plate-1-wild-turkey.jpg")
        assert second == first
        assert len(responses.calls) == 1  # cache hit, no second request

    @responses.activate
    def test_failure_returns_none_and_leaves_no_partial(self, index):
        url = f"{index.settings.image_repo}/1-99/plate-1-wild-turkey.jpg"
        responses.add(responses.GET, url, status=404)

        assert index.ensure_cached(1, "plate-1-wild-turkey.jpg") is None
        # A .part file left behind would be served as a truncated image later.
        assert not list(index.settings.plate_cache_dir.rglob("*.part"))
        assert not list(index.settings.plate_cache_dir.rglob("*.jpg"))

    @responses.activate
    def test_network_error_is_swallowed(self, index):
        url = f"{index.settings.image_repo}/1-99/plate-1-wild-turkey.jpg"
        responses.add(responses.GET, url, body=requests.ConnectionError("boom"))
        assert index.ensure_cached(1, "plate-1-wild-turkey.jpg") is None

    @responses.activate
    def test_zero_byte_cache_file_is_refetched(self, index):
        path = index.local_path(1, "plate-1-wild-turkey.jpg")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")

        url = f"{index.settings.image_repo}/1-99/plate-1-wild-turkey.jpg"
        responses.add(responses.GET, url, body=b"real", status=200)

        assert index.ensure_cached(1, "plate-1-wild-turkey.jpg") is not None
        assert path.read_bytes() == b"real"

    def test_cached_count_ignores_missing_dir(self, index):
        assert index.cached_count() == 0
