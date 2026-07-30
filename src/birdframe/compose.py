"""Composing the wall into one flat image.

Art Mode takes a single uploaded picture, so the mat that used to be CSS in the
browser is drawn here instead. Everything in this module is a port of what
frame.html did, and the numbers are deliberately the same ones:

  * the whole canvas is rag board, edge to edge - the TV is the frame, so no
    frame is drawn
  * the only join is the 45 degree bevel where the board meets the print
  * a landscape plate hangs alone; portraits hang two-up, which is what makes a
    16:9 panel read as a deliberate pair rather than one tall picture stranded
    in the middle

No text anywhere. The page is the artwork.
"""

from __future__ import annotations

import io
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

# Conservation rag board. Warm, and clearly darker than the plates' own paper -
# the plates carry a white margin of their own, and if the board is too pale the
# bevel disappears into it and the mat stops reading as a mat. Must track the
# base[] used by bevel_tones().
MAT_RGB = (222, 214, 196)

# Fractions of the canvas HEIGHT, so a 16:9 panel and a 21:9 one get the same
# board in the same proportion. These were `vh` units in the stylesheet.
#
# MAT_MARGIN sets the board above and below the prints outright. Horizontally
# both values are only *minimums* used to work out how big the prints can be:
# whatever board is left over after that is shared out evenly by layout(), so
# the gap between two prints always matches the gap at the edges.
MAT_MARGIN = 0.065  # board around the outside
MAT_GUTTER = 0.052  # least board to leave between two hung prints

# Depth of the 45 degree cut, in pixels of the composed image rather than a
# fraction of it, because what matters is how many pixels of the panel it lands
# on. A 45 degree cut through 4-ply rag board (~1.4mm) presents a face of
# 1.4 * sqrt(2) = 2mm, which on a 4K panel is 6px at 55" and 5px at 65".
#
# It used to be 0.0068 of the height, or 15px, which works out at 4mm of board -
# a fifteen-ply slab that no framer has ever cut. That is what made it read as
# drawn rather than cut.
DEFAULT_BEVEL_PX = 5

# Optional mottling for the board, as standard deviations of luminance, in two
# octaves of soft noise.
#
# OFF by default, because it was tried on a real TV and looked like a texture
# rather than like paper. There is no technical reason to want it either: the
# board is one constant colour, and a constant is the one thing JPEG reproduces
# exactly, so a flat fill has nothing to band. Set MAT_TEXTURE to bring it back -
# around 1.0-1.6 is the range worth trying.
#
# The octave scale is the part that took looking at. Per-pixel grain is pointless
# - at 68 ppi and three metres the eye resolves about 2.5 pixels, so it is
# invisible from the sofa and still costs about a megabyte a frame to encode. Go
# too far the other way and blobs of 24px and up read as cloudy marble. Features
# of roughly 3-16px are the ones that read as fibre.
# (divisor of the canvas, weight applied to the amount)
TEXTURE_OCTAVES: tuple[tuple[int, float], ...] = ((3, 1.0), (8, 0.5))
DEFAULT_TEXTURE = 0.0

# Fill light. Keeps the shadowed bevel faces from going flat black.
AMBIENT = 0.62

# A plate wider than this hangs alone; anything taller pairs up.
LANDSCAPE = 1.15

# Most plates are portrait. Used only when a file cannot be measured.
FALLBACK_RATIO = 0.823

Rgb = tuple[int, int, int]
Vec = tuple[float, float, float]

# The four bevel faces. The cut flares toward the viewer, so each normal has a
# +z component plus a lean in one direction. This inverts the intuition: with
# light from the upper left the TOP face tilts away and darkens while the BOTTOM
# face tilts up into the light and brightens.
FACES: dict[str, Vec] = {
    "top": (0.0, 1.0, 1.0),  # flares down toward the viewer
    "bottom": (0.0, -1.0, 1.0),  # flares up
    "left": (1.0, 0.0, 1.0),  # flares right
    "right": (-1.0, 0.0, 1.0),  # flares left
}


def bevel_tones(
    azimuth: float, elevation: float, base: Rgb = MAT_RGB, ambient: float = AMBIENT
) -> dict[str, Rgb]:
    """Shade the four bevel faces for one light direction.

    Solid rag board is the same colour all the way through, so these are the
    board colour under different amounts of light - there is no white core.
    Each face is a flat plane with a fixed normal, so each takes ONE flat tone;
    a gradient across a flat face is not how light works.

    ``azimuth`` is degrees, 0 = light from straight above and negative = from
    the left; -35 is the conventional gallery raking light. ``elevation`` is
    degrees above the wall plane.
    """
    az = math.radians(azimuth)
    el = math.radians(elevation)
    light: Vec = (
        math.cos(el) * math.sin(az),
        -math.cos(el) * math.cos(az),  # y grows downward in image space
        math.sin(el),
    )

    def lambert(n: Vec) -> float:
        length = math.dist((0.0, 0.0, 0.0), n) or 1.0
        d = sum(a * b for a, b in zip(n, light, strict=True)) / length
        return ambient + (1 - ambient) * max(0.0, d)

    # Normalised against the board's own face, lit head-on, so the board itself
    # keeps exactly the colour it was given.
    surface = lambert((0.0, 0.0, 1.0)) or 1.0

    tones: dict[str, Rgb] = {}
    for name, normal in FACES.items():
        m = lambert(normal) / surface
        r, g, b = (min(255, max(0, round(c * m))) for c in base)
        tones[name] = (r, g, b)
    return tones


# --------------------------------------------------------------------- layout


@dataclass(frozen=True, slots=True)
class Box:
    """One hung print's border box, bevel included. Pixels, top-left origin."""

    x: int
    y: int
    w: int
    h: int


def aspect_ratio(path: Path) -> float:
    """Width over height of a plate, read from the file's header only."""
    try:
        with Image.open(path) as img:
            w, h = img.size
        return w / h if h else FALLBACK_RATIO
    except (OSError, ValueError):
        return FALLBACK_RATIO


def layout(ratios: Sequence[float], width: int, height: int) -> list[Box]:
    """Place one or two prints on the board, centred, all the same height.

    The stylesheet let the prints keep their full height and overflow a narrow
    screen (``flex: 0 0 auto`` never shrinks). Here the height is solved so the
    row always fits, which matters because the canvas is whatever panel size the
    TV reports rather than a browser window someone can widen.

    The horizontal board is then shared out evenly. This is the part the
    stylesheet got wrong: ``justify-content: center`` with a fixed ``gap`` puts
    every spare pixel on the outside, so a pair of portraits on a 16:9 panel sat
    in 322px of board at the edges with a 112px strip between them. Two prints
    that far apart at the sides and that close together in the middle read as a
    mistake rather than as a pair.

    Note the board cannot be uniform on all four sides as well. Two portraits at
    full height leave more width spare than height, and the arithmetic has no
    solution where the side board equals the top board - so the horizontal gaps
    are matched to each other, and the vertical margin is left as designed.
    """
    if not ratios:
        return []

    margin = round(height * MAT_MARGIN)
    gutter = round(height * MAT_GUTTER)

    # Solve for the tallest common height whose row still fits the opening.
    usable = width - 2 * margin - gutter * (len(ratios) - 1)
    total_ratio = sum(ratios) or 1.0
    box_h = max(1, min(height - 2 * margin, int(usable / total_ratio)))

    widths = [max(1, round(box_h * r)) for r in ratios]

    # Every gap the same: one at each edge and one between each pair of prints.
    # Kept as a float and rounded per box so the rounding cannot accumulate into
    # a visibly wider gap at one end.
    gap = max(0.0, (width - sum(widths)) / (len(widths) + 1))
    y = (height - box_h) // 2

    boxes: list[Box] = []
    x = gap
    for w in widths:
        boxes.append(Box(round(x), y, w, box_h))
        x += w + gap
    return boxes


# -------------------------------------------------------------------- drawing


def _draw_bevel(
    draw: ImageDraw.ImageDraw, box: Box, depth: int, tones: dict[str, Rgb]
) -> None:
    """Draw the four bevel faces as trapezoids.

    Drawing them as four quads from the outer corners to the inner ones puts a
    real 45 degree mitre in each corner - the same joint a mat cutter leaves,
    and the same one the browser produced from four differently coloured
    borders.
    """
    ox0, oy0 = box.x, box.y
    ox1, oy1 = box.x + box.w - 1, box.y + box.h - 1
    ix0, iy0 = ox0 + depth, oy0 + depth
    ix1, iy1 = ox1 - depth, oy1 - depth

    draw.polygon([(ox0, oy0), (ox1, oy0), (ix1, iy0), (ix0, iy0)], fill=tones["top"])
    draw.polygon([(ox0, oy1), (ox1, oy1), (ix1, iy1), (ix0, iy1)], fill=tones["bottom"])
    draw.polygon([(ox0, oy0), (ox0, oy1), (ix0, iy1), (ix0, iy0)], fill=tones["left"])
    draw.polygon([(ox1, oy0), (ox1, oy1), (ix1, iy1), (ix1, iy0)], fill=tones["right"])


def paper_grain(size: tuple[int, int], amount: float) -> Image.Image | None:
    """Soft mottling to stand in for cotton fibre, as an L image around 128.

    Returns None when there is nothing to add, so the caller can skip the work
    entirely rather than adding a field of zeroes.
    """
    if amount <= 0:
        return None
    width, height = size
    grain = Image.new("L", (width, height), 128)
    for divisor, weight in TEXTURE_OCTAVES:
        octave = Image.effect_noise(
            (max(1, width // divisor), max(1, height // divisor)), amount * weight
        )
        if divisor != 1:
            # Bicubic, so the octave is smooth mottling rather than visible
            # rectangles of noise.
            octave = octave.resize((width, height), Image.Resampling.BICUBIC)
        grain = ImageChops.add(grain, octave, scale=1.0, offset=-128)
    return grain


def render(
    plates: Sequence[Path],
    *,
    size: tuple[int, int],
    light: tuple[float, float],
    bevel: int = DEFAULT_BEVEL_PX,
    texture: float = DEFAULT_TEXTURE,
) -> Image.Image:
    """Draw the board, the bevels and the prints into one image.

    An empty ``plates`` gives a bare mat, which is the honest thing to hang when
    nothing has been heard yet.
    """
    width, height = size
    canvas = Image.new("RGB", (width, height), MAT_RGB)
    depth = max(1, bevel)
    boxes = layout([aspect_ratio(p) for p in plates], width, height)

    # Bevels first, then the texture over both board and bevel - it is one sheet
    # of board and the cut goes through the same fibre. The prints go on last so
    # the grain is never laid over Audubon's own paper, which has a texture of
    # its own already and does not need a second one invented on top.
    draw = ImageDraw.Draw(canvas)
    tones = bevel_tones(*light)
    for box in boxes:
        _draw_bevel(draw, box, depth, tones)

    grain = paper_grain(size, texture)
    if grain is not None:
        canvas = ImageChops.add(canvas, grain.convert("RGB"), scale=1.0, offset=-128)

    for path, box in zip(plates, boxes, strict=True):
        # The opening is cut to the plate, so the paper fills it exactly and
        # nothing is cropped or letterboxed. The board is flush against the
        # print - a window mat is cut to the same plane as the paper, so there
        # is nothing to cast a shadow onto it.
        inner = (max(1, box.w - 2 * depth), max(1, box.h - 2 * depth))
        try:
            with Image.open(path) as img:
                plate = img.convert("RGB").resize(inner, Image.Resampling.LANCZOS)
        except (OSError, ValueError):
            continue  # a truncated download; leave the opening as bare board
        canvas.paste(plate, (box.x + depth, box.y + depth))

    return canvas


def render_jpeg(
    plates: Sequence[Path],
    *,
    size: tuple[int, int],
    light: tuple[float, float],
    bevel: int = DEFAULT_BEVEL_PX,
    texture: float = DEFAULT_TEXTURE,
    quality: int = 90,
) -> bytes:
    """``render`` encoded for upload. JPEG because the Frame wants megabytes,
    not the ~25MB a 4K PNG of this costs."""
    buf = io.BytesIO()
    render(plates, size=size, light=light, bevel=bevel, texture=texture).save(
        buf, format="JPEG", quality=quality, subsampling=0, optimize=True
    )
    return buf.getvalue()


# ------------------------------------------------------------------ selection


def choose[T](
    items: Sequence[T],
    path_of: Callable[[T], Path | None],
) -> list[tuple[T, Path]]:
    """Pick what hangs: the newest item, plus a partner if it needs one.

    ``items`` is newest-first, so index 0 is the bird that was just heard. A
    single landscape plate hangs alone; a portrait wants a second portrait beside
    it. ``path_of`` resolves an item to a cached file and may return None when
    the download failed, in which case that item is skipped rather than hanging
    an empty opening.

    This measures the real files rather than guessing from the plate number,
    because roughly 40% of the plates are landscape and getting it wrong is the
    difference between a considered pair and one picture floating in space.
    """
    if not items:
        return []

    first = items[0]
    first_path = path_of(first)
    if first_path is None:
        return []

    if aspect_ratio(first_path) >= LANDSCAPE:
        return [(first, first_path)]

    # Walk back through the history for the most recent other portrait to hang
    # beside it. Every step may call path_of, which can go to the network, so on
    # a cold cache after a restart this can be slow for one pass - but detections
    # arriving live are pre-fetched by the gallery's warmers, so in practice this
    # only measures files that are already on disk.
    for candidate in items[1:]:
        candidate_path = path_of(candidate)
        # Same file means the same species heard twice: hanging it twice would
        # read as a mistake rather than as a pair.
        if candidate_path is None or candidate_path == first_path:
            continue
        if aspect_ratio(candidate_path) < LANDSCAPE:
            return [(first, first_path), (candidate, candidate_path)]

    # Only one portrait to show.
    return [(first, first_path)]
