"""The picture the classifier sees: the read region, every final item boxed and numbered.

The numbers are the item question's choice keys, so the picture is drawn from the final items,
after the merge, the budget, and the renumbering, never from the detector's own boxes. It is cut
to the read region, the frontmost window and the menu bar strip, which holds every item, so the
same pixel budget buys a sharper picture than the whole display would.

System One takes an image whose sides are each between 280 and 1568 pixels. The region is scaled
down to fit the longer side, scaled up when a side is short, and padded when scaling up alone
cannot reach 280 without breaking the 1568 limit. Boxes and numbers are drawn after scaling, so a
number is at least `LABEL_PX` tall in the picture that is sent, whatever the scale.
"""

from __future__ import annotations

import functools
import io
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

from ..models import Box, Item

MAX_SIDE = 1568
MIN_SIDE = 280
LABEL_PX = 14  # the height of a number's digits in the picture as sent
PAD_COLOR = (128, 128, 128)

# One color per kind of source, the same in the picture sent and in the run folder's annotation.
AX_COLOR = (255, 140, 0)  # orange: the app's own controls
TEXT_COLOR = (0, 120, 255)  # blue: text read off the screen
ICON_COLOR = (200, 0, 200)  # magenta: detected icons, with text or a caption


def color_of(item: Item) -> tuple[int, int, int]:
    if item.from_ax:
        return AX_COLOR
    if item.source.startswith("icon"):
        return ICON_COLOR
    return TEXT_COLOR


@dataclass(frozen=True)
class Marked:
    """The picture as sent: PNG bytes, and its size."""

    png: bytes
    width: int
    height: int

    def image(self) -> Image.Image:
        return Image.open(io.BytesIO(self.png))


def fitted_scale(width: int, height: int) -> float:
    """The factor that brings the longer side to at most `MAX_SIDE`, and the shorter one up to
    `MIN_SIDE` where the longer side allows."""
    longer, shorter = max(width, height), min(width, height)
    scale = min(1.0, MAX_SIDE / longer)
    if shorter * scale < MIN_SIDE:
        scale = min(MIN_SIDE / shorter, MAX_SIDE / longer)
    return scale


@functools.cache
def label_font(px: int = LABEL_PX) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    """The smallest size of Pillow's own font whose digits stand at least `px` tall."""
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    for size in range(px, 4 * px):
        font = ImageFont.load_default(size=size)
        top, bottom = probe.textbbox((0, 0), "0123456789", font=font)[1::2]
        if bottom - top >= px:
            return font
    return ImageFont.load_default(size=4 * px)


def marks(image: Image.Image, items: list[Item], region: Box) -> Marked:
    """`image` cut to `region`, fitted to System One's limits, with each item boxed and numbered."""
    x1, y1, x2, y2 = (round(v) for v in region)
    crop = image.convert("RGB").crop((x1, y1, x2, y2))
    scale = fitted_scale(crop.width, crop.height)
    size = (max(1, round(crop.width * scale)), max(1, round(crop.height * scale)))
    scaled = crop.resize(size, Image.Resampling.LANCZOS) if size != crop.size else crop
    canvas = Image.new("RGB", (max(size[0], MIN_SIDE), max(size[1], MIN_SIDE)), PAD_COLOR)
    canvas.paste(scaled, (0, 0))
    draw = ImageDraw.Draw(canvas)
    font = label_font()
    width = max(2, round(LABEL_PX / 7))
    for it in items:
        box = ((it.x1 - x1) * scale, (it.y1 - y1) * scale, (it.x2 - x1) * scale, (it.y2 - y1) * scale)
        color = color_of(it)
        draw.rectangle(box, outline=color, width=width)
        label = str(it.index)
        left, top, right, bottom = draw.textbbox((0, 0), label, font=font)
        tag_w, tag_h = right - left + 4, bottom - top + 4
        tx = min(max(0.0, box[0]), canvas.width - tag_w)
        ty = box[1] - tag_h if box[1] - tag_h >= 0 else min(box[3], canvas.height - tag_h)
        draw.rectangle((tx, ty, tx + tag_w, ty + tag_h), fill=color)
        draw.text((tx + 2 - left, ty + 2 - top), label, fill=(255, 255, 255), font=font)
    out = io.BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    return Marked(out.getvalue(), canvas.width, canvas.height)
