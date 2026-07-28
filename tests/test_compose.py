"""The renderer: bevel shading, layout, and what hangs next to what.

These are the numbers that used to be CSS. They are pinned here because the
whole look of the wall is in them and a silent drift is not something you would
notice from across a room.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from birdframe.compose import (
    BEVEL,
    LANDSCAPE,
    MAT_MARGIN,
    MAT_RGB,
    aspect_ratio,
    bevel_tones,
    choose,
    layout,
    render,
    render_jpeg,
)


class TestBevelTones:
    """The four faces under one light, ported from the stylesheet's relight()."""

    def test_matches_the_stylesheet_values(self):
        """The CSS shipped these four hex values for az -35, el 40.

        They are the reference implementation: if the port drifts, the mat
        stops looking like the mat.
        """
        tones = bevel_tones(-35, 40)
        assert tones["top"] == (0xA0, 0x9B, 0x8E)
        assert tones["left"] == (0xAD, 0xA7, 0x99)
        assert tones["right"] == (0xEA, 0xE2, 0xCF)
        assert tones["bottom"] == (0xF7, 0xEE, 0xDA)

    def test_the_cut_flares_toward_the_viewer(self):
        """Light from above leaves the TOP face darkest and the BOTTOM lightest.

        This inverts the naive intuition, and getting it backwards is the single
        most likely way to make the bevel look wrong, so it is asserted rather
        than left to the eye.
        """
        tones = bevel_tones(-35, 40)
        assert sum(tones["top"]) < sum(MAT_RGB) < sum(tones["bottom"])

    def test_light_from_the_right_swaps_the_side_faces(self):
        left_lit = bevel_tones(-35, 40)
        right_lit = bevel_tones(35, 40)
        assert sum(left_lit["right"]) > sum(left_lit["left"])
        assert sum(right_lit["left"]) > sum(right_lit["right"])

    def test_light_straight_on_leaves_the_faces_even(self):
        """Elevation 90 is light down the viewer's own axis: no raking, so the
        four faces have nothing to distinguish them."""
        tones = bevel_tones(0, 90)
        assert len({tones["top"], tones["bottom"], tones["left"], tones["right"]}) == 1

    def test_tones_stay_in_gamut(self):
        for az in (-180, -35, 0, 35, 180):
            for el in (0, 40, 90):
                for tone in bevel_tones(az, el).values():
                    assert all(0 <= c <= 255 for c in tone)


class TestLayout:
    def test_no_plates_is_no_boxes(self):
        assert layout([], 3840, 2160) == []

    def test_one_portrait_is_centred(self):
        (box,) = layout([0.82], 3840, 2160)
        assert box.x + box.w // 2 == pytest.approx(1920, abs=2)
        assert box.y + box.h // 2 == pytest.approx(1080, abs=2)

    def test_margin_is_honoured(self):
        (box,) = layout([0.82], 3840, 2160)
        margin = round(2160 * MAT_MARGIN)
        assert box.y == margin
        assert box.h == 2160 - 2 * margin

    def test_a_pair_shares_a_height_and_does_not_overlap(self):
        first, second = layout([0.82, 0.78], 3840, 2160)
        assert first.h == second.h
        assert first.x + first.w < second.x

    def test_the_pair_is_centred_as_a_unit(self):
        first, second = layout([0.82, 0.78], 3840, 2160)
        left_board = first.x
        right_board = 3840 - (second.x + second.w)
        assert left_board == pytest.approx(right_board, abs=2)

    def test_wide_plates_are_shrunk_to_fit_rather_than_overflowing(self):
        """The stylesheet used `flex: 0 0 auto`, which happily ran two wide
        plates off the edge of a narrow screen. The canvas here is whatever the
        panel is, so the height is solved instead."""
        margin = round(2160 * MAT_MARGIN)
        boxes = layout([1.7, 1.7], 3840, 2160)
        assert boxes[0].x >= margin
        assert boxes[-1].x + boxes[-1].w <= 3840 - margin
        assert boxes[0].h < 2160 - 2 * margin  # it had to give up height

    def test_everything_stays_on_the_canvas(self):
        for ratios in ([0.82], [1.7], [0.82, 0.82], [1.7, 1.7], [0.6, 1.6]):
            for box in layout(ratios, 3840, 2160):
                assert box.x >= 0
                assert box.y >= 0
                assert box.x + box.w <= 3840
                assert box.y + box.h <= 2160


class TestRender:
    LIGHT = (-35.0, 40.0)

    def test_an_empty_wall_is_bare_board(self, plate_file):
        img = render([], size=(384, 216), light=self.LIGHT)
        assert img.size == (384, 216)
        assert img.getpixel((0, 0)) == MAT_RGB
        assert img.getpixel((192, 108)) == MAT_RGB

    def test_the_board_runs_edge_to_edge(self, plate_file):
        """No frame is drawn: the TV is the frame."""
        img = render([plate_file(800, 1000)], size=(3840, 2160), light=self.LIGHT)
        for corner in ((0, 0), (3839, 0), (0, 2159), (3839, 2159)):
            assert img.getpixel(corner) == MAT_RGB

    def test_the_plate_lands_in_the_opening(self, plate_file):
        img = render([plate_file(800, 1000)], size=(3840, 2160), light=self.LIGHT)
        assert img.getpixel((1920, 1080)) != MAT_RGB

    def test_the_bevel_is_drawn_around_the_print(self, plate_file):
        """Walking in from the board edge should cross bevel before paper."""
        img = render([plate_file(800, 1000)], size=(3840, 2160), light=self.LIGHT)
        boxes = layout([0.8], 3840, 2160)
        box = boxes[0]
        depth = max(1, round(2160 * BEVEL))
        top_face = img.getpixel((box.x + box.w // 2, box.y + depth // 2))
        assert top_face == bevel_tones(*self.LIGHT)["top"]

    def test_two_portraits_hang_together(self, plate_file):
        img = render(
            [plate_file(800, 1000, "a.jpg"), plate_file(800, 1000, "b.jpg")],
            size=(3840, 2160),
            light=self.LIGHT,
        )
        # Board down the middle, paper either side of it.
        assert img.getpixel((1920, 1080)) == MAT_RGB
        first, second = layout([0.8, 0.8], 3840, 2160)
        assert img.getpixel((first.x + first.w // 2, 1080)) != MAT_RGB
        assert img.getpixel((second.x + second.w // 2, 1080)) != MAT_RGB

    def test_an_unreadable_plate_leaves_board_rather_than_raising(self, tmp_path):
        """A half-finished download must not take the whole wall down."""
        broken = tmp_path / "broken.jpg"
        broken.write_bytes(b"not a jpeg")
        img = render([broken], size=(384, 216), light=self.LIGHT)
        assert img.getpixel((192, 108)) == MAT_RGB

    def test_render_jpeg_is_a_decodable_jpeg_of_the_right_size(self, plate_file):
        body = render_jpeg([plate_file(800, 1000)], size=(3840, 2160), light=self.LIGHT)
        with Image.open(io.BytesIO(body)) as img:
            assert img.format == "JPEG"
            assert img.size == (3840, 2160)


class TestAspectRatio:
    def test_measures_the_file(self, plate_file):
        assert aspect_ratio(plate_file(1600, 1000)) == pytest.approx(1.6)

    def test_a_missing_file_falls_back_rather_than_raising(self, tmp_path):
        assert aspect_ratio(tmp_path / "nope.jpg") == pytest.approx(0.823)


class TestChoose:
    """Which plates hang together. Ported from the browser's compose()."""

    def test_nothing_to_hang(self, plate_file):
        assert choose([], 0, lambda item: None) == []

    def test_a_landscape_plate_hangs_alone(self, plate_file):
        wide = plate_file(1700, 1000)
        slots = choose(["a"], 0, lambda item: wide)
        assert len(slots) == 1

    def test_two_portraits_pair_up(self, plate_file):
        paths = {"a": plate_file(800, 1000, "a.jpg"), "b": plate_file(820, 1000, "b.jpg")}
        slots = choose(["a", "b"], 0, paths.get)
        assert [item for item, _ in slots] == ["a", "b"]

    def test_a_lone_portrait_hangs_alone(self, plate_file):
        tall = plate_file(800, 1000)
        slots = choose(["a"], 0, lambda item: tall)
        assert len(slots) == 1

    def test_a_portrait_skips_a_landscape_to_find_its_partner(self, plate_file):
        paths = {
            "tall": plate_file(800, 1000, "tall.jpg"),
            "wide": plate_file(1700, 1000, "wide.jpg"),
            "tall2": plate_file(790, 1000, "tall2.jpg"),
        }
        slots = choose(["tall", "wide", "tall2"], 0, paths.get)
        assert [item for item, _ in slots] == ["tall", "tall2"]

    def test_the_cursor_wraps(self, plate_file):
        wide = plate_file(1700, 1000)
        slots = choose(["a", "b"], 5, lambda item: wide)
        assert slots[0][0] == "b"

    def test_an_unfetchable_first_plate_hangs_nothing(self, plate_file):
        assert choose(["a"], 0, lambda item: None) == []

    def test_the_same_plate_is_never_hung_twice(self, plate_file):
        """Two detections of one species share a plate number, and hanging the
        same picture side by side looks like a bug rather than a pair."""
        same = plate_file(800, 1000)
        slots = choose(["a", "b"], 0, lambda item: same)
        assert len(slots) == 1

    def test_the_landscape_threshold(self, plate_file):
        assert pytest.approx(1.15) == LANDSCAPE
        just_wide = plate_file(1160, 1000)
        assert len(choose(["a", "b"], 0, lambda item: just_wide)) == 1
