"""The renderer: bevel shading, layout, and what hangs next to what.

These are the numbers that used to be CSS. They are pinned here because the
whole look of the wall is in them and a silent drift is not something you would
notice from across a room.
"""

from __future__ import annotations

import io
import itertools
import statistics

import pytest
from PIL import Image, ImageStat

from birdframe.compose import (
    DEFAULT_BEVEL_PX,
    DEFAULT_TEXTURE,
    LANDSCAPE,
    MAT_MARGIN,
    MAT_RGB,
    aspect_ratio,
    bevel_tones,
    choose,
    layout,
    paper_grain,
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
        assert boxes[0].x > 0
        assert boxes[-1].x + boxes[-1].w < 3840
        assert boxes[0].h < 2160 - 2 * margin  # it had to give up height

    @staticmethod
    def gaps(boxes, width):
        """Board at the left edge, between each pair of prints, and at the right."""
        out = [boxes[0].x]
        for previous, box in itertools.pairwise(boxes):
            out.append(box.x - (previous.x + previous.w))
        out.append(width - (boxes[-1].x + boxes[-1].w))
        return out

    def test_a_pair_gets_the_same_board_between_it_as_around_it(self):
        """The stylesheet centred the row and used a fixed gap, so every spare
        pixel went to the outside: 322px of board at the edges against a 112px
        strip in the middle. Two prints spaced like that read as a mistake."""
        gaps = self.gaps(layout([0.82, 0.82], 3840, 2160), 3840)
        assert max(gaps) - min(gaps) <= 1

    def test_uneven_ratios_still_get_even_board(self):
        """Audubon cut the plates to the bird, so a pair is rarely two identical
        shapes. The board between them must not depend on that."""
        gaps = self.gaps(layout([0.82, 0.70], 3840, 2160), 3840)
        assert max(gaps) - min(gaps) <= 1

    def test_the_shrunk_case_is_also_even(self):
        gaps = self.gaps(layout([1.7, 1.7], 3840, 2160), 3840)
        assert max(gaps) - min(gaps) <= 1

    def test_a_single_plate_is_unaffected(self):
        """Centring one print already put equal board either side; the fix was
        only ever about the pair."""
        for ratio in (0.82, 1.6):
            left, right = self.gaps(layout([ratio], 3840, 2160), 3840)
            assert abs(left - right) <= 1

    def test_the_prints_still_get_as_much_height_as_the_board_allows(self):
        """Evening out the width must not come out of the pictures."""
        margin = round(2160 * MAT_MARGIN)
        assert layout([0.82, 0.82], 3840, 2160)[0].h == 2160 - 2 * margin

    def test_everything_stays_on_the_canvas(self):
        for ratios in ([0.82], [1.7], [0.82, 0.82], [1.7, 1.7], [0.6, 1.6]):
            for box in layout(ratios, 3840, 2160):
                assert box.x >= 0
                assert box.y >= 0
                assert box.x + box.w <= 3840
                assert box.y + box.h <= 2160


class TestRender:
    """Geometry and shading. Texture is off throughout: these assert exact
    pixel values, and mottling the board is precisely what perturbs them.
    TestTexture covers the mottling itself."""

    LIGHT = (-35.0, 40.0)

    def flat(self, plates, size=(3840, 2160)):
        return render(plates, size=size, light=self.LIGHT, texture=0.0)

    def test_an_empty_wall_is_bare_board(self, plate_file):
        img = self.flat([], size=(384, 216))
        assert img.size == (384, 216)
        assert img.getpixel((0, 0)) == MAT_RGB
        assert img.getpixel((192, 108)) == MAT_RGB

    def test_the_board_runs_edge_to_edge(self, plate_file):
        """No frame is drawn: the TV is the frame."""
        img = self.flat([plate_file(800, 1000)])
        for corner in ((0, 0), (3839, 0), (0, 2159), (3839, 2159)):
            assert img.getpixel(corner) == MAT_RGB

    def test_the_plate_lands_in_the_opening(self, plate_file):
        assert self.flat([plate_file(800, 1000)]).getpixel((1920, 1080)) != MAT_RGB

    def test_the_bevel_is_drawn_around_the_print(self, plate_file):
        """Walking in from the board edge should cross bevel before paper."""
        img = self.flat([plate_file(800, 1000)])
        box = layout([0.8], 3840, 2160)[0]
        top_face = img.getpixel((box.x + box.w // 2, box.y + DEFAULT_BEVEL_PX // 2))
        assert top_face == bevel_tones(*self.LIGHT)["top"]

    def test_two_portraits_hang_together(self, plate_file):
        img = self.flat([plate_file(800, 1000, "a.jpg"), plate_file(800, 1000, "b.jpg")])
        # Board down the middle, paper either side of it.
        assert img.getpixel((1920, 1080)) == MAT_RGB
        first, second = layout([0.8, 0.8], 3840, 2160)
        assert img.getpixel((first.x + first.w // 2, 1080)) != MAT_RGB
        assert img.getpixel((second.x + second.w // 2, 1080)) != MAT_RGB

    def test_an_unreadable_plate_leaves_board_rather_than_raising(self, tmp_path):
        """A half-finished download must not take the whole wall down."""
        broken = tmp_path / "broken.jpg"
        broken.write_bytes(b"not a jpeg")
        assert self.flat([broken], size=(384, 216)).getpixel((192, 108)) == MAT_RGB

    def test_render_jpeg_is_a_decodable_jpeg_of_the_right_size(self, plate_file):
        body = render_jpeg([plate_file(800, 1000)], size=(3840, 2160), light=self.LIGHT)
        with Image.open(io.BytesIO(body)) as img:
            assert img.format == "JPEG"
            assert img.size == (3840, 2160)


class TestBevel:
    LIGHT = (-35.0, 40.0)

    def test_the_default_is_a_four_ply_cut(self):
        """A 45 degree cut through 4-ply rag board shows a face of about 2mm,
        which on a 4K panel is 5-6px. The old 15px worked out at 4mm of board -
        a fifteen-ply slab nobody has ever cut, and it read as drawn."""
        assert 4 <= DEFAULT_BEVEL_PX <= 6

    def test_it_is_pixels_not_a_fraction_of_the_canvas(self):
        """What matters is how many pixels of the panel the cut lands on."""
        for height in (2160, 1080):
            img = render([], size=(1920, height), light=self.LIGHT, bevel=8, texture=0.0)
            assert img.size == (1920, height)

    def test_a_wider_bevel_leaves_less_room_for_the_plate(self, plate_file):
        plate = plate_file(800, 1000)
        box = layout([0.8], 3840, 2160)[0]
        thin = render([plate], size=(3840, 2160), light=self.LIGHT, bevel=2, texture=0.0)
        thick = render(
            [plate], size=(3840, 2160), light=self.LIGHT, bevel=40, texture=0.0
        )
        # 6px in from the corner is paper under a thin cut and still board
        # under a thick one.
        probe = (box.x + 6, box.y + box.h // 2)
        assert thin.getpixel(probe) != thick.getpixel(probe)

    def test_it_never_vanishes_entirely(self, plate_file):
        """A zero bevel is a print floating on board with no cut at all."""
        img = render(
            [plate_file(800, 1000)],
            size=(3840, 2160),
            light=self.LIGHT,
            bevel=0,
            texture=0.0,
        )
        box = layout([0.8], 3840, 2160)[0]
        assert (
            img.getpixel((box.x, box.y + box.h // 2)) == bevel_tones(*self.LIGHT)["left"]
        )


class TestTexture:
    """Optional mottling for the board. Off by default - it was tried on a real
    panel and read as a texture rather than as paper."""

    LIGHT = (-35.0, 40.0)
    SUBTLE = 1.6  # the amount worth trying, if you want it at all

    def board(self, texture):
        return render([], size=(600, 600), light=self.LIGHT, texture=texture)

    def levels(self, img):
        return [p[0] for p in img.get_flattened_data()]

    def test_the_default_is_flat(self):
        """A constant is the one thing JPEG reproduces exactly, so a flat board
        has nothing to band and nothing to gain from noise."""
        assert DEFAULT_TEXTURE == 0.0
        assert set(self.levels(self.board(DEFAULT_TEXTURE))) == {MAT_RGB[0]}

    def test_off_is_perfectly_flat(self):
        assert set(self.levels(self.board(0.0))) == {MAT_RGB[0]}

    def test_on_breaks_up_the_flat_fill(self):
        assert statistics.pstdev(self.levels(self.board(self.SUBTLE))) > 0.8

    def test_it_stays_subtle(self):
        """Loud enough to see as noise and it is worse than the flat fill."""
        levels = self.levels(self.board(self.SUBTLE))
        assert statistics.pstdev(levels) < 4.0
        assert max(levels) - min(levels) < 32

    def test_it_keeps_the_board_colour(self):
        """Mottling is a perturbation of the rag board, not a different board."""
        mean = statistics.mean(self.levels(self.board(self.SUBTLE)))
        assert mean == pytest.approx(MAT_RGB[0], abs=2)

    def test_more_is_more(self):
        quiet = statistics.pstdev(self.levels(self.board(1.0)))
        loud = statistics.pstdev(self.levels(self.board(6.0)))
        assert loud > quiet

    def test_the_grain_is_coarser_than_a_single_pixel(self):
        """Per-pixel grain is invisible at three metres and costs about a
        megabyte a frame, so the octaves are deliberately soft. Neighbouring
        pixels should therefore track each other far better than pure noise."""
        row = self.levels(self.board(self.SUBTLE).crop((0, 300, 600, 301)))
        steps = [abs(b - a) for a, b in itertools.pairwise(row)]
        assert statistics.mean(steps) < statistics.pstdev(row)

    def test_no_grain_is_laid_over_the_artwork(self, plate_file):
        """Audubon's paper has a texture of its own; inventing a second one on
        top of the plate is just degrading the picture."""
        plate = plate_file(800, 1000)
        clean = render([plate], size=(3840, 2160), light=self.LIGHT, texture=0.0)
        mottled = render([plate], size=(3840, 2160), light=self.LIGHT, texture=6.0)
        assert clean.getpixel((1920, 1080)) == mottled.getpixel((1920, 1080))

    def test_the_bevel_is_textured_with_the_board(self, plate_file):
        """It is one sheet, and the cut goes through the same fibre."""
        box = layout([0.8], 3840, 2160)[0]
        strip = (box.x + 20, box.y, box.x + 300, box.y + DEFAULT_BEVEL_PX - 1)
        img = render(
            [plate_file(800, 1000)], size=(3840, 2160), light=self.LIGHT, texture=6.0
        )
        assert statistics.pstdev(self.levels(img.crop(strip))) > 0

    def test_paper_grain_is_skipped_when_there_is_none_to_add(self):
        assert paper_grain((100, 100), 0.0) is None
        assert paper_grain((100, 100), -1.0) is None

    def test_paper_grain_is_centred_so_it_does_not_shift_the_colour(self):
        grain = paper_grain((400, 400), 3.0)
        assert grain is not None
        assert ImageStat.Stat(grain).mean[0] == pytest.approx(128, abs=2)


class TestAspectRatio:
    def test_measures_the_file(self, plate_file):
        assert aspect_ratio(plate_file(1600, 1000)) == pytest.approx(1.6)

    def test_a_missing_file_falls_back_rather_than_raising(self, tmp_path):
        assert aspect_ratio(tmp_path / "nope.jpg") == pytest.approx(0.823)


class TestChoose:
    """Which plates hang together. Ported from the browser's compose()."""

    def test_nothing_to_hang(self, plate_file):
        assert choose([], lambda item: None) == []

    def test_a_landscape_plate_hangs_alone(self, plate_file):
        wide = plate_file(1700, 1000)
        slots = choose(["a"], lambda item: wide)
        assert len(slots) == 1

    def test_two_portraits_pair_up(self, plate_file):
        paths = {"a": plate_file(800, 1000, "a.jpg"), "b": plate_file(820, 1000, "b.jpg")}
        slots = choose(["a", "b"], paths.get)
        assert [item for item, _ in slots] == ["a", "b"]

    def test_a_lone_portrait_hangs_alone(self, plate_file):
        tall = plate_file(800, 1000)
        slots = choose(["a"], lambda item: tall)
        assert len(slots) == 1

    def test_a_portrait_skips_a_landscape_to_find_its_partner(self, plate_file):
        paths = {
            "tall": plate_file(800, 1000, "tall.jpg"),
            "wide": plate_file(1700, 1000, "wide.jpg"),
            "tall2": plate_file(790, 1000, "tall2.jpg"),
        }
        slots = choose(["tall", "wide", "tall2"], paths.get)
        assert [item for item, _ in slots] == ["tall", "tall2"]

    def test_the_newest_item_always_hangs_first(self, plate_file):
        """items[0] is the bird that was just heard, and it is the whole point of
        the wall -- so it must lead, not merely appear."""
        paths = {
            "newest": plate_file(800, 1000, "newest.jpg"),
            "older": plate_file(820, 1000, "older.jpg"),
        }
        slots = choose(["newest", "older"], paths.get)
        assert slots[0][0] == "newest"

    def test_an_unfetchable_first_plate_hangs_nothing(self, plate_file):
        assert choose(["a"], lambda item: None) == []

    def test_the_same_plate_is_never_hung_twice(self, plate_file):
        """Two detections of one species share a plate number, and hanging the
        same picture side by side looks like a bug rather than a pair."""
        same = plate_file(800, 1000)
        slots = choose(["a", "b"], lambda item: same)
        assert len(slots) == 1

    def test_the_landscape_threshold(self, plate_file):
        assert pytest.approx(1.15) == LANDSCAPE
        just_wide = plate_file(1160, 1000)
        assert len(choose(["a", "b"], lambda item: just_wide)) == 1
