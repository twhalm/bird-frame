"""The webhook and poller parsers.

These are the tolerance-to-payload-shape rules, which are otherwise only
verifiable by pointing a real BirdNET-Go at the thing.
"""

from __future__ import annotations

import math

import pytest

from birdframe.payload import (
    extract_rows,
    normalise_confidence,
    parse_poll_row,
    parse_webhook,
)


class TestNormaliseConfidence:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (0.93, 0.93),
            (93, 0.93),
            ("0.93", 0.93),
            ("93", 0.93),
            ("93%", 0.93),
            (1, 1.0),
            (1.0, 1.0),
            (0, 0.0),
            (100, 1.0),
        ],
    )
    def test_ratio_or_percentage(self, raw, expected):
        assert normalise_confidence(raw) == pytest.approx(expected)

    @pytest.mark.parametrize("raw", [None, "", "  ", "high", {}, [], object()])
    def test_missing_or_junk_is_none(self, raw):
        # None, not 0.0: "absent" must stay distinguishable from "very unsure",
        # because the confidence filter treats them the same but the log does not.
        assert normalise_confidence(raw) is None

    def test_bool_is_not_a_confidence(self):
        assert normalise_confidence(True) is None

    def test_nan_is_rejected(self):
        assert normalise_confidence(math.nan) is None

    def test_clamped_to_unit_range(self):
        assert normalise_confidence(-5) == 0.0
        assert normalise_confidence(400) == 1.0


class TestParseWebhook:
    def test_flat_snake_case(self):
        p = parse_webhook(
            {
                "scientific_name": "Cardinalis cardinalis",
                "species": "Northern Cardinal",
                "confidence": 0.94,
                "timestamp": "2026-07-27T10:00:00Z",
            }
        )
        assert p.scientific == "Cardinalis cardinalis"
        assert p.common == "Northern Cardinal"
        assert p.confidence == pytest.approx(0.94)
        assert p.when == "2026-07-27T10:00:00Z"

    def test_birdnet_go_default_title_and_metadata(self):
        p = parse_webhook(
            {
                "Title": "Blue Jay",
                "Metadata": {
                    "scientificName": "Cyanocitta cristata",
                    "Confidence": 91,
                    "Timestamp": "2026-07-27T11:00:00Z",
                },
            }
        )
        assert p.scientific == "Cyanocitta cristata"
        assert p.common == "Blue Jay"
        assert p.confidence == pytest.approx(0.91)

    def test_top_level_wins_over_metadata(self):
        p = parse_webhook(
            {"species": "Northern Cardinal", "metadata": {"species": "Blue Jay"}}
        )
        assert p.common == "Northern Cardinal"

    def test_empty_string_falls_through_to_metadata(self):
        p = parse_webhook({"species": "", "metadata": {"species": "Blue Jay"}})
        assert p.common == "Blue Jay"

    def test_non_dict_metadata_is_ignored(self):
        p = parse_webhook({"species": "Blue Jay", "metadata": "nope"})
        assert p.common == "Blue Jay"

    def test_no_species_is_detectable(self):
        assert not parse_webhook({"confidence": 0.9}).has_species

    def test_missing_timestamp_is_none(self):
        assert parse_webhook({"species": "Blue Jay"}).when is None


class TestParsePollRow:
    def test_camel_case_row(self):
        p = parse_poll_row(
            {
                "scientificName": "Colaptes auratus",
                "commonName": "Northern Flicker",
                "confidence": 0.88,
                "timestamp": "2026-07-27T09:00:00Z",
            }
        )
        assert p.scientific == "Colaptes auratus"
        assert p.confidence == pytest.approx(0.88)
        assert p.when == "2026-07-27T09:00:00Z"

    def test_split_date_and_time_columns(self):
        p = parse_poll_row(
            {"commonName": "Blue Jay", "date": "2026-07-27", "time": "09:14:00"}
        )
        assert p.when == "2026-07-27 09:14:00"

    def test_no_time_fields_at_all(self):
        assert parse_poll_row({"commonName": "Blue Jay"}).when is None


class TestExtractRows:
    @pytest.mark.parametrize("key", ["data", "detections", "results", "items"])
    def test_wrapped_lists(self, key):
        assert extract_rows({key: [{"a": 1}]}) == [{"a": 1}]

    def test_bare_list(self):
        assert extract_rows([{"a": 1}]) == [{"a": 1}]

    def test_non_dict_rows_dropped(self):
        assert extract_rows([{"a": 1}, "junk", None, 7]) == [{"a": 1}]

    @pytest.mark.parametrize("data", [{}, {"nope": 1}, None, "text", 7])
    def test_unrecognised_shapes_are_empty(self, data):
        assert extract_rows(data) == []
