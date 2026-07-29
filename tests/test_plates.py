"""Plate resolution.

The project's core promise is that it never puts the wrong bird on the wall, so
the negative cases here matter more than the positive ones.
"""

from __future__ import annotations

import itertools
import json
import re

import pytest
import requests
import responses

from birdframe.plates import PlateIndex, bucket_for, normalise

# Slug-shaped folding, for comparing a plate's `name` against its own `slug`.
# Deliberately separate from plates.normalise(), which folds to spaces for name
# matching; here the target is the hyphenated slug the filename is built from.
_STOPWORDS = ("of", "the", "and", "or")


def _slugify(s: str) -> str:
    s = s.lower().replace("’", "").replace("'", "").replace("&", "and")
    return "-".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def _soften(s: str) -> str:
    """Drop the words the upstream slugs lose, so both sides compare alike.

    The source slugs omit stopwords the display name keeps ("Bird of Washington"
    -> bird-washington, "Cock of the Plains" -> cock-plains). Applied to the name
    and the slug both, this leaves only genuine name/image disagreements.
    """
    return "-".join(p for p in s.split("-") if p not in _STOPWORDS)


# Upstream spelling drift between a plate's name and its own slug. Both are the
# right bird -- the slug is what the filename uses, so it cannot be corrected
# here without breaking the download URL.
_SLUG_TYPOS = frozenset(
    {
        15,  # 'Blue Yellow back Warbler'      vs blue-yellow-backed-warbler
        353,  # 'Chestnut-backed Titmouse, ...' vs chesnut-backed-titmouse
    }
)

# Curated species that legitimately do not match their plate's slug: a plate
# often depicts several birds and the slug names only the first, plus a few where
# Audubon's label is another vernacular for the same bird.
#
# Held as an explicit list rather than inferred from the plate's `name`, because
# `name` is exactly the field that was corrupt -- deriving the exemption from it
# let a shifted row excuse itself, and this check passed on the broken data.
_MULTI_SPECIES_PLATES = frozenset(
    {
        "Setophaga americana",
        "Vireo solitarius",
        "Petrochelidon pyrrhonota",
        "Pandion haliaetus",
        "Coragyps atratus",
        "Setophaga magnolia",
        "Accipiter gentilis",
        "Lanius borealis",
        "Egretta caerulea",
        "Poecile atricapillus",
        "Poecile rufescens",
        "Piranga olivacea",
        "Tyrannus forficatus",
        "Sayornis saya",
        "Cyanocitta stelleri",
        "Nucifraga columbiana",
        "Ixoreus naevius",
        "Tachycineta thalassina",
        "Xanthocephalus xanthocephalus",
        "Sialia mexicana",
        "Sialia currucoides",
        "Setophaga coronata",
        "Setophaga occidentalis",
        "Setophaga nigrescens",
        "Acanthis flammea",
        "Setophaga tigrina",
        "Melanerpes carolinus",
        "Haemorhous mexicanus",
        "Coccothraustes vespertinus",
        "Asio flammeus",
        "Parkesia noveboracensis",
    }
)


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

    def test_every_row_names_its_own_image(self, index):
        """The invariant that catches a shifted `name` column.

        plates.json rows once had `name` lagging one row behind the other four
        fields for plates 361-399, so a Rufous Hummingbird resolved to
        plate-380-tengmalms-owl.jpg -- an owl on the wall, under a hummingbird's
        name in /api/current. `plate`, `slug`, `fileName` and `download` all
        agreed with each other, so only a name-against-slug check finds it.

        Compared by prefix because a slug names only the first species of a
        multi-species plate ("Bank Swallow and Violet-green Swallow" ->
        bank-swallow), and with the slug's own punctuation losses folded out.
        """
        offenders = {
            p["plate"]: (p["name"], p["slug"])
            for p in index.plates.values()
            if p["plate"] not in _SLUG_TYPOS
            and not _soften(_slugify(p["name"])).startswith(_soften(p["slug"]))
        }
        assert not offenders

    def test_no_row_names_the_previous_rows_bird(self, index):
        """The shift itself, stated directly.

        Distinct from the check above: a plate whose slug is truncated fails
        that one harmlessly, whereas naming the *previous* plate's bird is only
        ever the off-by-one, and is what hangs a confidently wrong species.
        """
        rows = [index.plates[n] for n in sorted(index.plates)]
        shifted = [
            row["plate"]
            for prev, row in itertools.pairwise(rows)
            if _soften(_slugify(row["name"])).startswith(_soften(prev["slug"]))
            and not _soften(_slugify(row["name"])).startswith(_soften(row["slug"]))
        ]
        assert not shifted

    def test_curated_entries_resolve_to_their_own_image(self, index):
        """No curated species may hang a picture of a different bird.

        The plate numbers in curated_map.json were derived from the shifted
        names, so they were off by one over the same range and 53 of 219
        mappings rendered the wrong species. Checks the Audubon label against
        the filename actually fetched.

        Compared against the plate's `slug`, never its `name`: the slug is what
        the fetched filename is built from, so it is the only trustworthy record
        of which bird the image shows.
        """
        exempt = {sci.lower() for sci in _MULTI_SPECIES_PLATES}
        wrong = {}
        for sci, entry in index.by_scientific.items():
            plate = index.plates.get(entry["plate"])
            if plate is None:
                continue  # covered by test_every_curated_plate_exists
            if sci.lower() in exempt:
                continue
            label = (entry.get("audubon") or plate["name"]).split(" / ")[0]
            if not _soften(_slugify(label)).startswith(_soften(plate["slug"])):
                wrong[sci] = (label, plate["fileName"])
        assert not wrong

    def test_rufous_hummingbird_is_not_an_owl(self, index):
        """The reported bug, pinned as its own case.

        Selasphorus rufus resolved to plate 380, whose image is Tengmalm's Owl.
        """
        m = index.resolve("Selasphorus rufus", "Rufous Hummingbird")
        assert m is not None
        plate = index.plates[m.plate]
        assert "owl" not in plate["fileName"]
        assert "humming" in plate["fileName"]

    def test_no_species_resolves_to_an_owl_unless_it_is_one(self, index):
        """Sweeps both match paths, not just the curated one.

        Three species reached an owl image through the common-name fallback
        rather than through curated_map.json, so checking only the curated table
        would have left them on the wall.
        """
        offenders = {}
        for name, plate_no in index.by_common.items():
            plate = index.plates[plate_no]
            if "owl" in plate["fileName"] and "owl" not in name:
                offenders[name] = plate["fileName"]
        assert not offenders


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
