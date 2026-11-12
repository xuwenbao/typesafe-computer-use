import pytest
from PIL import Image, ImageDraw

from typesafe_computer_use.models import Item
from typesafe_computer_use.parse.marks import (
    AX_COLOR,
    ICON_COLOR,
    LABEL_PX,
    MAX_SIDE,
    MIN_SIDE,
    TEXT_COLOR,
    color_of,
    fitted_scale,
    label_font,
    marks,
)


def item(index, x1, y1, x2, y2, source="ocr"):
    return Item(index, f"item {index}", 0.9, float(x1), float(y1), float(x2), float(y2), source=source)


def white(width, height):
    return Image.new("RGB", (width, height), "white")


def colors_in(image: Image.Image) -> set:
    return {color for _, color in image.getcolors(maxcolors=1 << 20)}


def test_a_retina_region_comes_down_to_the_longest_side_allowed():
    marked = marks(white(2880, 1800), [], (0.0, 0.0, 2880.0, 1800.0))
    assert (marked.width, marked.height) == (1568, 980)


def test_a_narrow_region_is_scaled_up_without_stretching():
    marked = marks(white(1000, 200), [], (0.0, 0.0, 1000.0, 200.0))
    assert min(marked.width, marked.height) >= MIN_SIDE and max(marked.width, marked.height) <= MAX_SIDE
    assert marked.width / marked.height == pytest.approx(1000 / 200)


def test_a_strip_too_long_to_scale_up_is_padded_instead():
    marked = marks(white(4000, 100), [], (0.0, 0.0, 4000.0, 100.0))
    assert (marked.width, marked.height) == (MAX_SIDE, MIN_SIDE)
    image = marked.image()
    assert image.getpixel((10, 10)) == (255, 255, 255) and image.getpixel((10, MIN_SIDE - 1)) != (255, 255, 255)


@pytest.mark.parametrize("size", [(2880, 1800), (1000, 200), (300, 300), (4000, 100), (100, 4000)])
def test_every_side_stays_within_system_ones_limits(size):
    scale = fitted_scale(*size)
    marked = marks(white(*size), [], (0.0, 0.0, float(size[0]), float(size[1])))
    assert MIN_SIDE <= marked.width <= MAX_SIDE and MIN_SIDE <= marked.height <= MAX_SIDE
    assert 78_400 <= marked.width * marked.height <= 2_458_624
    assert scale > 0


def test_the_picture_is_a_png_cut_to_the_region():
    image = white(1000, 800)
    ImageDraw.Draw(image).rectangle((0, 0, 99, 799), fill="black")  # outside the region
    marked = marks(image, [], (100.0, 0.0, 1000.0, 800.0))
    assert marked.png.startswith(b"\x89PNG") and (marked.width, marked.height) == (900, 800)
    assert (0, 0, 0) not in colors_in(marked.image())


def test_a_number_stands_at_least_the_label_height():
    top, bottom = ImageDraw.Draw(Image.new("L", (1, 1))).textbbox((0, 0), "0123456789", font=label_font())[1::2]
    assert bottom - top >= LABEL_PX


def test_each_item_is_boxed_in_the_color_of_its_source_and_only_the_items_given():
    items = [item(0, 100, 100, 200, 140), item(1, 300, 100, 340, 140, source="icon"), item(2, 500, 100, 600, 140, source="ax")]
    image = marks(white(800, 400), items, (0.0, 0.0, 800.0, 400.0)).image()
    assert {TEXT_COLOR, ICON_COLOR, AX_COLOR} <= colors_in(image)
    assert {TEXT_COLOR, AX_COLOR}.isdisjoint(colors_in(marks(white(800, 400), items[1:2], (0.0, 0.0, 800.0, 400.0)).image()))


def test_boxes_follow_the_region_and_the_scale():
    items = [item(7, 1440, 900, 1500, 960)]
    image = marks(white(2880, 1800), items, (0.0, 0.0, 2880.0, 1800.0)).image()
    scale = 1568 / 2880
    assert image.getpixel((round(1440 * scale), round(930 * scale))) == TEXT_COLOR  # the left edge, scaled
    shifted = marks(white(2880, 1800), items, (1000.0, 500.0, 2000.0, 1300.0)).image()
    assert shifted.getpixel((440, 430)) == TEXT_COLOR  # (1440 - 1000, 930 - 500), at full scale


def test_sources_map_to_three_colors():
    assert color_of(item(0, 0, 0, 1, 1, "ax+icon")) == AX_COLOR
    assert color_of(item(0, 0, 0, 1, 1, "icon+ocr")) == ICON_COLOR
    assert color_of(item(0, 0, 0, 1, 1, "ocr")) == TEXT_COLOR
