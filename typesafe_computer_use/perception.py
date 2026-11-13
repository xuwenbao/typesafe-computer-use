"""Turn the display into clickable items: text blocks, icons, and accessibility controls.

One pipeline on every desktop: RapidOCR reads the lines and YOLOv9 finds the icons over the
frontmost window's region, the two are fused line by line, the remaining lines join into blocks,
Florence-2 describes the icons that carry no text, and the result merges with the app's own
controls. The models are in `parse/`; this module decides what to read and what to keep.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

from .config import MAX_OPTIONS, MIN_OCR_CONFIDENCE
from .models import MENU_BAR_PT, AxNode, Box, Item, Screen
from .parse import caption as parse_caption
from .parse import detect as parse_detect
from .parse import ocr as parse_ocr
from .parse import weights as parse_weights
from .parse.fuse import fuse
from .platform_adapter import desktop
from .timing import OCR_AHEAD, OCR_RECTS, OCR_REGION_PCT, phase

Line = tuple[str, float, Box]
Detection = tuple[Box, float]  # an icon box in capture pixels, and the detector's score
PIPELINE_VERSION = "omniparser-1"  # names this pipeline in an evaluation report and a run's record
ECHO_CHARS = 24
MIN_BOX_OVERLAP = 0.5  # intersection over the smaller box
MIN_TOKEN_OVERLAP = 0.5
TEXT_SOURCES = ("ocr",)
ICON_SOURCES = ("icon", "icon+ocr")
# An icon box whose gray levels span less than this is flat: the detector's guess at a patch of
# background, which the captioner still describes, as an SVG path or a word it makes up.
ICON_MIN_CONTRAST = 32

# OCR costs about two thirds of a step, and it scales with the amount of text, so the way to make it
# cheaper is to read less of the screen: the frontmost window's own columns instead of the display,
# and within them only the blobs of tiles that changed since the previous capture, one crop each.
REGION_MARGIN_PT = 8.0  # slack around the window, for the shadow and a clipped glyph
THUMB_DIVISOR = 8  # the change detector works on a 1/8 scale grayscale copy
TILE_PX = 256.0  # tile side in capture pixels
TILE_DIFF = 6.0  # mean absolute 8-bit difference that counts a tile as changed
PIXEL_DIFF = 32  # one thumbnail pixel this far off reads as drawn or erased, not as noise
CHANGED_SHARE = 0.01  # this share of such pixels marks a tile changed even when the mean stays low
REOCR_FRACTION = 0.6  # above this share of changed tiles, reading the whole region is cheaper
MAX_REOCR_RECTS = 4  # past this, the per-call overhead outweighs the pixels another rectangle saves
BLANK_INSET_PX = 4  # how far in from a control's edge its inside is read, past a neighbour's border
BLANK_RANGE = 12  # 8-bit gray spread at or below which a patch is one flat color


def capture(
    image_path: Path | None = None,
    app: str | None = None,
    url: str | None = None,
    browser: str = "",
    timing: dict[str, float] | None = None,
    ahead: ParseCache | None = None,
) -> Screen:
    """Capture the main display, or load a saved capture for replay (then app/url are taken as given).

    Each query below is a round trip to the window server, AX, or AppleScript. Pass `timing` to
    record the seconds each one costs under "screenshot", "app", "window", "field", and "url".
    Pass `ahead`, the run's OCR cache, to start reading the capture while those queries run (see
    `ParseCache.read_ahead`).
    """
    replay = image_path is not None and app is not None
    with phase(timing, "screenshot"):
        image = Image.open(image_path).convert("RGB") if image_path else desktop.screenshot()
    scale = desktop.display_scale(image)
    if ahead is not None:
        ahead.read_ahead(image, scale)
    with phase(timing, "app"):
        if replay:
            frontmost, pid = app, None
        else:
            frontmost, pid = desktop.frontmost_app_and_pid()
            frontmost = app or frontmost
    with phase(timing, "window"):
        window = None if replay else desktop.frontmost_window_bounds(pid)
    with phase(timing, "field"):
        field = None if replay else desktop.focused_field()
    with phase(timing, "url"):
        page_url = url if url is not None else (None if replay else desktop.browser_url(browser))
    return Screen(image=image, scale=scale, app=frontmost, field=field, url=page_url, pid=pid, window=window)


def goal_echoes(goal: str) -> set[str]:
    """Substrings that identify a screen line as the command that launched this run. None without a
    goal, as an evaluation reads a capture for no task, and an empty substring is in every line."""
    norm = " ".join(goal.lower().split())
    if not norm:
        return set()
    return {norm[:ECHO_CHARS], norm[-ECHO_CHARS:]} if len(norm) >= ECHO_CHARS else {norm}


def is_echo(text: str, echoes: set[str]) -> bool:
    norm = " ".join(text.lower().split())
    return any(e in norm for e in echoes)


def perceive(
    screen: Screen,
    budget: int,
    goal: str,
    timing: dict[str, float] | None = None,
    cache: ParseCache | None = None,
) -> list[Item]:
    """Everything worth clicking on this screen: text blocks and icons, plus the app's own controls.

    Fills `screen.ax_refs` on the way, so an item that came from the accessibility tree can be
    pressed through it later. The merge renumbers everything, hence the side table over the
    final indices rather than a handle on the item itself, which has to stay printable.
    `screen.covered` is a side table the same way: the items under a popup, and which popup.

    Fills `screen.offscreen` too: labelled controls the app exposes but does not show. They are
    offered on their own, never as items, because nothing on the capture points at them.

    A `cache` carries the previous capture's reading, so only the tiles that changed are read again.
    Pass None to read the whole region every time, which is what a replay and an inspection do.
    """
    found = ocr(screen, budget, goal, cache, timing)
    with phase(timing, "ax"):
        nodes, hidden = ax_nodes(screen, budget)
        gray = screen.image.convert("L")
        blank = [node for node in nodes if not drawn(gray, node, screen.scale)]
        nodes = [node for node in nodes if node not in blank]
        hidden = hidden + blank
        controls = to_ax_items(nodes, screen.scale)
    merged = merge_with_origins(found, controls, budget)
    screen.ax_refs.clear()
    screen.ax_refs.update({it.index: nodes[origin].ref for it, origin in merged if origin is not None and nodes[origin].ref})
    screen.covered.clear()
    screen.covered.update(
        {it.index: nodes[origin].covered_by for it, origin in merged if origin is not None and nodes[origin].covered_by}
    )
    items = [it for it, _ in merged]
    screen.offscreen.clear()
    screen.offscreen.extend(offscreen_controls(hidden, items))
    return items


def ocr(
    screen: Screen,
    budget: int,
    goal: str,
    cache: ParseCache | None = None,
    timing: dict[str, float] | None = None,
) -> list[Item]:
    """The screen's text blocks and icons as items, without the accessibility tree.

    The lines are filtered first: too faint, the goal echoed back, or inside the focused one-line
    field, whose value is in the state already. The rest fuse with the icon boxes (see
    `parse.fuse`); the lines no icon took join into blocks, an icon that holds text takes it, and
    an icon with none is described by the captioner, unless its box is flat.

    The filter runs over the raw lines every step, including the reused ones, so a cached line is
    treated exactly as a freshly read one. A read the cache made ahead of the step counts as its
    own when it read this screen (see `ParseCache.read_ahead`); the step's "ocr" phase is then the
    wait for it, and the seconds it took go under "ocr_ahead".
    """
    if cache is not None:
        with phase(timing, "ocr"):
            ahead = cache.take_ahead(screen)
    else:
        ahead = None
    reading = ahead.reading if ahead is not None else read_screen(screen, cache, timing)
    if timing is not None:
        timing[OCR_REGION_PCT] = round(reading.read_pct, 1)
        timing[OCR_RECTS] = reading.rects
        if ahead is not None:
            timing[OCR_AHEAD] = round(ahead.seconds, 3)
    echoes = goal_echoes(goal)
    kept: list[Line] = [
        (t.strip(), c, b)
        for t, c, b in reading.lines
        if t.strip() and c >= MIN_OCR_CONFIDENCE and not is_echo(t, echoes) and not in_field(Item(0, t, c, *b), screen)
    ]
    texts, icons = fuse(kept, reading.icons)
    items = [Item(0, t, c, *b) for t, c, b in merge_blocks(texts)]
    items += [Item(0, i.text, i.text_confidence or 0.0, *i.box, source="icon+ocr") for i in icons if i.text is not None]
    gray = screen.image.convert("L")
    bare = [i for i in icons if i.text is None and contrast(gray, i.box) >= ICON_MIN_CONTRAST]
    if bare:
        with phase(timing, "caption"):
            captions = parse_caption.caption(screen.image, [i.box for i in bare], cache.captions if cache is not None else None)
        items += [
            Item(0, text, i.score, *i.box, role="icon", source="icon") for i, text in zip(bare, captions, strict=True) if text
        ]
    items = order_items(items)
    return order_items([items[i] for i in kept_by_budget(items, budget)])


def contrast(gray: Image.Image, box: Box) -> int:
    """The spread of gray levels inside `box`, 0 for a box with no pixel on the capture."""
    x1, y1 = max(0, math.floor(box[0])), max(0, math.floor(box[1]))
    x2, y2 = min(gray.width, math.ceil(box[2])), min(gray.height, math.ceil(box[3]))
    if x2 <= x1 or y2 <= y1:
        return 0
    low, high = gray.crop((x1, y1, x2, y2)).getextrema()
    return high - low


# ------------------------------------------------------------------ reading less of the screen


class ParseCache:
    """The previous capture's reading, and what makes it reusable.

    Holds a reduced grayscale copy of the capture, to find what moved; the raw OCR lines before
    merging and filtering, and the icon boxes, both in full-capture pixels; and the icon captions
    by crop. One cache belongs to one run.
    """

    def __init__(self) -> None:
        self.app: str | None = None
        self.window: tuple[float, float, float, float] | None = None
        self.region: Box | None = None
        self.thumb: Image.Image | None = None
        self.lines: list[Line] = []
        self.icons: list[Detection] | None = None  # None until a detection ran over this region
        self.unchanged = False  # whether the last read found no tile changed, so its icons still hold
        self.captions = parse_caption.CaptionCache()
        self._ahead: ReadAhead | None = None

    def reusable(self, screen: Screen, region: Box, thumb: Image.Image) -> bool:
        """Never across a different app, a moved or resized window, or a different read region."""
        return (
            self.thumb is not None
            and self.thumb.size == thumb.size
            and self.app == screen.app
            and self.window == screen.window
            and self.region == region
        )

    def store(self, screen: Screen, region: Box, thumb: Image.Image, lines: list[Line], unchanged: bool = False) -> None:
        self.app, self.window, self.region, self.thumb, self.lines = screen.app, screen.window, region, thumb, list(lines)
        self.unchanged = unchanged

    def adopt(self, other: ParseCache) -> None:
        """Take what `other` remembers of the previous capture as this cache's own. The captions are
        shared, not copied: they hold for any capture."""
        self.app, self.window, self.region, self.thumb = other.app, other.window, other.region, other.thumb
        self.lines = list(other.lines)
        self.icons = None if other.icons is None else list(other.icons)
        self.unchanged = other.unchanged
        self.captions = other.captions

    def copy(self) -> ParseCache:
        other = ParseCache()
        other.adopt(self)
        return other

    def read_ahead(self, image: Image.Image, scale: float) -> None:
        """Start reading `image` on a thread of its own, while the capture asks which app and window it
        shows. Where those queries are slow, as a WAA VM's tree is, the read overlaps them.

        The read is `read_screen` over the previous capture's app and window, into a copy of this
        cache, so it is the very read `read_screen` makes once the capture finds them unchanged, as a
        page that stays in one window does; `ocr` takes it then, and reads the screen itself
        otherwise. The first capture has nothing to go on, so nothing is read ahead of it. A read
        still running from an earlier capture is waited out first, and every model call takes the
        one inference lock, so two reads never run at once.
        """
        self.drop_ahead()
        if self.thumb is None:
            return
        guess = Screen(image=image, scale=scale, app=self.app or "", field=None, url=None, window=self.window)
        self._ahead = ReadAhead(guess, self.copy())

    def take_ahead(self, screen: Screen) -> ReadAhead | None:
        """The read made ahead of `screen`, once it is done, when it read this capture over this app
        and window; its cache becomes this one. None when there is none, or it read another."""
        ahead, self._ahead = self._ahead, None
        if ahead is None:
            return None
        ahead.wait()
        if ahead.reading is None or not ahead.fits(screen):
            return None
        self.adopt(ahead.cache)
        return ahead

    def drop_ahead(self) -> None:
        """Wait out a read made ahead of a capture that never came to be read, and forget it."""
        if self._ahead is not None:
            self._ahead.wait()
            self._ahead = None


@dataclass(frozen=True)
class Reading:
    """One capture's raw OCR lines and icon boxes over its region, in full-capture pixels."""

    lines: list[Line]
    icons: list[Detection]
    read_pct: float  # share of the capture OCR read
    rects: int  # how many rectangles it read; 0 for the whole region or for nothing
    detected: bool  # whether the icons were detected for this capture, not reused


class ReadAhead:
    """One capture's reading, made on a thread of its own over the app and window it guessed."""

    def __init__(self, screen: Screen, cache: ParseCache) -> None:
        self.screen = screen  # the capture, and the app and window the read bet on
        self.cache = cache  # a copy of the run's cache, which the read updates
        self.reading: Reading | None = None  # None if the read raised
        self.seconds = 0.0
        self._thread = threading.Thread(target=self._read, name="jev-read-ahead", daemon=True)
        self._thread.start()

    def _read(self) -> None:
        started = time.perf_counter()
        try:
            self.reading = read_screen(self.screen, self.cache)
        except Exception:  # the step reads the screen itself, and raises what this one swallowed
            self.reading = None
        self.seconds = time.perf_counter() - started

    def wait(self) -> None:
        self._thread.join()

    def fits(self, screen: Screen) -> bool:
        """Whether this read is the one `read_screen` would make of `screen`."""
        guess = self.screen
        return screen.image is guess.image and (screen.scale, screen.app, screen.window) == (guess.scale, guess.app, guess.window)


def pipeline_record() -> dict:
    """The pipeline and the model files it reads, for a run's or an evaluation's record."""
    return {"pipeline": PIPELINE_VERSION, "weights": parse_weights.record()}


def read_screen(screen: Screen, cache: ParseCache | None = None, timing: dict[str, float] | None = None) -> Reading:
    """The capture's OCR lines and icon boxes over its region.

    The text is read incrementally (see `ocr_lines`). The icons are detected over the whole region
    again whenever any tile changed, and reused only when none did: the detector finds other boxes
    on a crop than on the whole, so stitching crops together would trade accuracy for little time.
    """
    with phase(timing, "ocr"):
        lines, read_pct, rects = ocr_lines(screen, cache)
    if cache is not None and cache.unchanged and cache.icons is not None:
        return Reading(lines, list(cache.icons), read_pct, rects, detected=False)
    with phase(timing, "detect"):
        icons = detect_region(screen.image, ocr_region(screen))
    if cache is not None:
        cache.icons = list(icons)
    return Reading(lines, icons, read_pct, rects, detected=True)


def detect_region(image: Image.Image, region: Box) -> list[Detection]:
    """The icon boxes in one rectangle of the capture, in full-capture pixels."""
    x1, y1, x2, y2 = (round(v) for v in region)
    crop = image if (x1, y1, x2, y2) == (0, 0, image.width, image.height) else image.crop((x1, y1, x2, y2))
    return [((b[0] + x1, b[1] + y1, b[2] + x1, b[3] + y1), score) for b, score in parse_detect.detect(crop)]


def ocr_lines(screen: Screen, cache: ParseCache | None = None) -> tuple[list[Line], float, int]:
    """Raw OCR lines for this capture in full-capture pixels, the share of it read, and how many crops.

    Without a cache this reads the region once. With one it reads only the rectangles covering the
    tiles that changed, one OCR call each, unless too much of the screen moved, in which case the
    whole region is cheaper than stitching. A line a re-read rectangle touches is dropped and read
    again whole, because OCR segments a crop slightly differently from the full image.
    """
    region = ocr_region(screen)
    area = float(max(1, screen.image.width * screen.image.height))

    def read_pct(rects: list[Box]) -> float:
        return 100.0 * sum(area_of(rect) for rect in rects) / area

    if cache is None:
        return ocr_crop(screen.image, region), read_pct([region]), 0

    thumb = thumbnail(screen.image)
    if not cache.reusable(screen, region, thumb):
        return _read_region(screen, region, thumb, cache), read_pct([region]), 0
    tiles = tiles_in(region)
    changed = changed_tiles(thumb, cache.thumb, tiles)
    if len(changed) > REOCR_FRACTION * len(tiles):
        return _read_region(screen, region, thumb, cache), read_pct([region]), 0
    rects = reocr_rects(changed, region, cache.lines)
    if not rects:
        cache.store(screen, region, thumb, cache.lines, unchanged=True)
        return cache.lines, 0.0, 0
    if sum(area_of(rect) for rect in rects) > REOCR_FRACTION * area_of(region):
        return _read_region(screen, region, thumb, cache), read_pct([region]), 0
    fresh = [ln for rect in rects for ln in ocr_crop(screen.image, rect)]
    lines = merge_reocr(cache.lines, fresh, rects)
    cache.store(screen, region, thumb, lines)
    return lines, read_pct(rects), len(rects)


def _read_region(screen: Screen, region: Box, thumb: Image.Image, cache: ParseCache) -> list[Line]:
    lines = ocr_crop(screen.image, region)
    cache.store(screen, region, thumb, lines)
    return lines


def ocr_region(screen: Screen) -> Box:
    """The part of the capture worth reading, in capture pixels.

    The frontmost window with a margin, joined with the menu bar strip over the same columns and
    clamped to the display. Text on the desktop and in background windows is noise to the decision,
    so it is left unread. Clipping the strip to the window's x-range is what makes the crop worth
    anything on a full-height window, whose own rectangle already reaches the bottom of the display.

    The cost is that status items to the right of the window, the clock and the menu extras, go
    unread. They stay clickable: the accessibility tree lists them as AXMenuBarItem controls.
    """
    width, height = float(screen.image.width), float(screen.image.height)
    if screen.window is None:
        return (0.0, 0.0, width, height)
    x, y, w, h = screen.window
    scale, margin = screen.scale, REGION_MARGIN_PT
    window = ((x - margin) * scale, (y - margin) * scale, (x + w + margin) * scale, (y + h + margin) * scale)
    joined = (window[0], min(window[1], 0.0), window[2], max(window[3], MENU_BAR_PT * scale))
    clamped = (max(0.0, joined[0]), max(0.0, joined[1]), min(width, joined[2]), min(height, joined[3]))
    return clamped if clamped[2] > clamped[0] and clamped[3] > clamped[1] else (0.0, 0.0, width, height)


def ocr_crop(image: Image.Image, rect: Box) -> list[Line]:
    """OCR one rectangle of the capture. Boxes come back in full-capture pixels, so nothing downstream
    knows a crop happened."""
    x1, y1, x2, y2 = (round(v) for v in rect)
    crop = image if (x1, y1, x2, y2) == (0, 0, image.width, image.height) else image.crop((x1, y1, x2, y2))
    raw = parse_ocr.recognize_text(crop)
    return [(text, conf, (b[0] + x1, b[1] + y1, b[2] + x1, b[3] + y1)) for text, conf, b in raw]


def thumbnail(image: Image.Image, divisor: int = THUMB_DIVISOR) -> Image.Image:
    """A grayscale copy at 1/divisor scale. Box-averaged, so it costs a few milliseconds and smooths
    away the compression noise that would otherwise read as a change."""
    return image.convert("L").reduce(divisor)


def tiles_in(region: Box, tile: float = TILE_PX) -> list[Box]:
    """The region cut into tiles aligned to its own origin. The last row and column are short."""
    x1, y1, x2, y2 = region
    out: list[Box] = []
    y = y1
    while y < y2:
        x = x1
        while x < x2:
            out.append((x, y, min(x + tile, x2), min(y + tile, y2)))
            x += tile
        y += tile
    return out


def tile_changed(
    thumb: Image.Image,
    previous: Image.Image,
    tile: Box,
    divisor: int = THUMB_DIVISOR,
    threshold: float = TILE_DIFF,
) -> bool:
    """True when the tile's mean absolute difference clears the threshold, or when more than
    `CHANGED_SHARE` of its pixels differ by more than `PIXEL_DIFF`.

    The mean misses a pale popup that fills little of the tile: the tip is gone and the words stay.
    A pointer is smaller than that share, so it does not force a re-read. A tile whose patch cannot
    be compared counts as changed, so a doubt is always paid for with a re-read."""
    a, b = _patch(thumb, tile, divisor), _patch(previous, tile, divisor)
    if a.size != b.size or not a.width or not a.height:
        return True
    diff = ImageChops.difference(a, b)
    if ImageStat.Stat(diff).mean[0] > threshold:
        return True
    strong = sum(diff.histogram()[PIXEL_DIFF + 1 :])
    return strong > CHANGED_SHARE * diff.width * diff.height


def _patch(thumb: Image.Image, tile: Box, divisor: int) -> Image.Image:
    x1, y1, x2, y2 = (round(v / divisor) for v in tile)
    return thumb.crop((x1, y1, max(x2, x1 + 1), max(y2, y1 + 1)))


def changed_tiles(
    thumb: Image.Image,
    previous: Image.Image,
    tiles: list[Box],
    divisor: int = THUMB_DIVISOR,
    threshold: float = TILE_DIFF,
) -> list[Box]:
    return [tile for tile in tiles if tile_changed(thumb, previous, tile, divisor, threshold)]


def tile_clusters(changed: list[Box], tile: float = TILE_PX) -> list[list[Box]]:
    """The changed tiles grouped into blobs that touch along a side or at a corner.

    Scattered change is the ordinary case: the menu bar clock ticks a digit while one panel
    repaints. A single bounding box around both spans the display and forces a full read, where
    two boxes leave everything between them alone.
    """
    parent = list(range(len(changed)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, a in enumerate(changed):
        for j, b in enumerate(changed[i + 1 :], start=i + 1):
            if abs(a[0] - b[0]) <= 1.5 * tile and abs(a[1] - b[1]) <= 1.5 * tile:
                parent[root(i)] = root(j)
    blobs: dict[int, list[Box]] = {}
    for i, box in enumerate(changed):
        blobs.setdefault(root(i), []).append(box)
    return list(blobs.values())


def reocr_rects(
    changed: list[Box],
    region: Box,
    lines: list[Line],
    limit: int = MAX_REOCR_RECTS,
    tile: float = TILE_PX,
) -> list[Box]:
    """The rectangles to read again, one Vision call each. Empty when nothing changed.

    One rectangle per blob of changed tiles, each grown until it cuts no known line, the ones that
    end up touching merged, and the count brought down to `limit` by merging the closest pairs.
    """
    rects = settled([blob_rect(blob, region, tile) for blob in tile_clusters(changed, tile)], lines, region)
    while len(rects) > limit:
        i, j = closest_pair(rects)
        rects = settled([union_box(rects[i], rects[j])] + [r for k, r in enumerate(rects) if k not in (i, j)], lines, region)
    return rects


def blob_rect(blob: list[Box], region: Box, tile: float = TILE_PX) -> Box:
    """One blob's bounding box, padded by a tile so a line crossing the edge is read whole, clamped."""
    x1 = min(b[0] for b in blob) - tile
    y1 = min(b[1] for b in blob) - tile
    x2 = max(b[2] for b in blob) + tile
    y2 = max(b[3] for b in blob) + tile
    return (max(region[0], x1), max(region[1], y1), min(region[2], x2), min(region[3], y2))


def settled(rects: list[Box], lines: list[Line], region: Box) -> list[Box]:
    """Grow every rectangle past the lines it would cut, merge the ones that meet, until neither moves.

    Growing can push two rectangles together, and a merged rectangle has edges neither original had,
    which can cut a line neither of them cut. So the two steps run to a fixed point, which they reach:
    a rectangle only ever grows, bounded by the region, and a merge only ever removes one.
    """
    while True:
        grown = [grown_for_lines(rect, lines, region) for rect in rects]
        merged = merge_touching(grown)
        if grown == rects and merged == grown:
            return merged
        rects = merged


def merge_touching(rects: list[Box]) -> list[Box]:
    """The rectangles with every overlapping or touching pair replaced by the box around both."""
    out = list(rects)
    merged = True
    while merged:
        merged = False
        for i, a in enumerate(out):
            for j, b in enumerate(out[i + 1 :], start=i + 1):
                if boxes_touch(a, b):
                    out = [union_box(a, b)] + [r for k, r in enumerate(out) if k not in (i, j)]
                    merged = True
                    break
            if merged:
                break
    return out


def closest_pair(rects: list[Box]) -> tuple[int, int]:
    """Positions of the two rectangles with the smallest gap between them."""
    _, i, j = min((gap_between(rects[i], rects[j]), i, j) for i in range(len(rects)) for j in range(i + 1, len(rects)))
    return i, j


def gap_between(a: Box, b: Box) -> float:
    """Distance between two rectangles, zero when they overlap or touch."""
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return math.hypot(dx, dy)


def union_box(a: Box, b: Box) -> Box:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def boxes_touch(a: Box, b: Box) -> bool:
    """Overlapping, or meeting along an edge or at a corner: worth reading as one rectangle."""
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def area_of(box: Box) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def grown_for_lines(rect: Box, lines: list[Line], region: Box) -> Box:
    """The rectangle grown until no known line straddles its edge, clamped to the region.

    A crop cuts a line in half, and Vision reads the visible half as its own line, so a rectangle
    that ends mid-line would trade a whole headline for a fragment. Reading a larger rectangle is
    the cheaper mistake.
    """
    x1, y1, x2, y2 = rect
    growing = True
    while growing:
        growing = False
        for _, _, box in lines:
            if not boxes_intersect(box, (x1, y1, x2, y2)):
                continue
            grown = (
                max(region[0], min(x1, box[0])),
                max(region[1], min(y1, box[1])),
                min(region[2], max(x2, box[2])),
                min(region[3], max(y2, box[3])),
            )
            if grown != (x1, y1, x2, y2):
                x1, y1, x2, y2 = grown
                growing = True
    return (x1, y1, x2, y2)


def boxes_intersect(a: Box, b: Box) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def merge_reocr(previous: list[Line], fresh: list[Line], rects: list[Box]) -> list[Line]:
    """Previous lines no re-read rectangle touches, plus every line just read inside them."""
    return [ln for ln in previous if not any(boxes_intersect(ln[2], rect) for rect in rects)] + list(fresh)


def ax_nodes(screen: Screen, budget: int) -> tuple[list[AxNode], list[AxNode]]:
    """The frontmost app's on-screen controls, in screen points, and the labelled off-screen ones it
    still exposes.

    An on-screen control without a label is kept: it can take its name from the icon drawn on it
    (see `merge_with_origins`), and is dropped there when no icon covers it. Accessibility is best
    effort: a missing pid, a refusing app, or a raising bridge all mean the capture carries the
    step alone.
    """
    if screen.pid is None:
        return [], []
    width_pt, height_pt = screen.size_pt
    try:
        nodes, hidden, _capped = desktop.actionable_elements(screen.pid, width_pt, height_pt)
    except Exception:
        return [], []
    return list(nodes[:budget]), [node for node in hidden if node.label]


def drawn(image: Image.Image, node: AxNode, scale: float) -> bool:
    """Whether the capture shows anything where the control says it is.

    A page can hide an element with CSS clipping and still report it to accessibility as showing:
    Chosen, a common dropdown widget, keeps its search box in a clipped panel under the closed
    control, so a click on it lands on empty page. Such a box is one flat color inside, and has no
    border down either side. An empty field has its border, and a control with a label or icon has
    ink inside, so both count as drawn. Every part is read a few pixels in from the top and bottom
    edges, since a neighbour's border can reach over them, and the inside a few pixels in from the
    sides as well.
    """
    x1, y1 = max(0, round(node.x * scale)), max(0, round(node.y * scale))
    x2, y2 = min(image.width, round((node.x + node.w) * scale)), min(image.height, round((node.y + node.h) * scale))
    if x2 - x1 < 2 or y2 - y1 < 2:
        return True  # nothing to judge: too small, or off the capture, which is not this check's call
    inset_x, inset_y = min(BLANK_INSET_PX, (x2 - x1) // 4), min(BLANK_INSET_PX, (y2 - y1) // 4)
    gray = image if image.mode == "L" else image.convert("L")  # perceive converts once for every control
    parts = [
        (x1 + inset_x, y1 + inset_y, x2 - inset_x, y2 - inset_y),  # the inside
        (x1, y1 + inset_y, x1 + inset_x, y2 - inset_y),  # the left side
        (x2 - inset_x, y1 + inset_y, x2, y2 - inset_y),  # the right side
    ]
    return any(_ink(gray.crop(part)) for part in parts if part[2] > part[0] and part[3] > part[1])


def _ink(patch: Image.Image) -> bool:
    low, high = patch.getextrema()
    return high - low > BLANK_RANGE


def offscreen_controls(nodes: list[AxNode], items: list[Item]) -> list[AxNode]:
    """The off-screen controls worth offering: one per role and label, minus anything already on screen.

    An app repeats a label across a scrolled list and across the copies of a view it keeps alive, and
    the first one is as good as any since the press goes to the element. A label the visible list
    already carries is dropped outright: the item on screen is the better way to reach it.
    """
    visible = {it.text for it in items}
    seen: set[tuple[str, str]] = set()
    out: list[AxNode] = []
    for node in nodes:
        key = (node.role, node.label)
        if key in seen or node.label in visible:
            continue
        seen.add(key)
        out.append(node)
    return out


def in_field(block: Item, screen: Screen) -> bool:
    """Whether an OCR block sits inside the focused one-line text field: its value, its placeholder,
    or an icon drawn in it.

    The field is an item already when the app declares it, and its value is in the state. Read again
    as a block of its own, the text just typed becomes something to click. A text area is left
    alone: its lines are content, not one value.
    """
    field = screen.field
    if field is None or not field.is_text or field.role == "AXTextArea":
        return False
    x, y = screen.to_points(block)
    return field.x < x < field.x + field.w and field.y < y < field.y + field.h


def to_ax_items(nodes: list[AxNode], scale: float) -> list[Item]:
    """Controls as items, converted from screen points to capture pixels."""
    return [
        Item(
            index=i,
            text=node.label,
            ocr_confidence=1.0,
            x1=node.x * scale,
            y1=node.y * scale,
            x2=(node.x + node.w) * scale,
            y2=(node.y + node.h) * scale,
            role=node.role_word,
            source="ax",
        )
        for i, node in enumerate(nodes)
    ]


def ax_items(screen: Screen, budget: int) -> list[Item]:
    """The frontmost app's labelled on-screen controls as items on the capture."""
    return [it for it in to_ax_items(ax_nodes(screen, budget)[0], screen.scale) if it.text]


def merge_sources(found: list[Item], ax_items: list[Item], budget: int = MAX_OPTIONS) -> list[Item]:
    """One item per thing: a control that sits on the text block naming it, or on an icon, replaces both."""
    return [it for it, _ in merge_with_origins(found, ax_items, budget)]


def merge_with_origins(found: list[Item], ax_items: list[Item], budget: int = MAX_OPTIONS) -> list[tuple[Item, int | None]]:
    """The merge, each item paired with the position of the control it came from, or None.

    A control pairs first with the text block it overlaps that names it ("ax+ocr"), and failing
    that with the icon it overlaps most ("ax+icon"), whatever a caption or a symbol says: a caption
    is English and a label may not be, and a label may be empty, when the icon's text or caption
    names the item instead. An icon that holds words read off the screen pairs only with a control
    that has no label or one those words match: a popup menu the tree does not list sits over
    other controls, and its row must keep its own name. A control with no label and no icon under
    it names nothing, and goes.

    The pairing survives the budget cut and the renumbering, which is the only way back from a
    final item to the accessibility element behind it.
    """
    taken: set[int] = set()
    merged: list[tuple[Item, int | None]] = []
    for origin, control in enumerate(ax_items):
        block = _best(control, found, taken, TEXT_SOURCES, named=True)
        if block is not None:
            taken.add(block)
            text = control.text if len(control.text) >= len(found[block].text) else found[block].text
            merged.append((replace(found[block], text=text, role=control.role, source="ax+ocr"), origin))
            continue
        icon = _best_icon(control, found, taken)
        if icon is not None and (control.text or found[icon].text):
            taken.add(icon)
            merged.append(
                (replace(found[icon], text=control.text or found[icon].text, role=control.role, source="ax+icon"), origin)
            )
        elif control.text:
            merged.append((control, origin))
    merged += [(it, None) for i, it in enumerate(found) if i not in taken]
    kept = [merged[i] for i in kept_by_budget([it for it, _ in merged], budget)]
    order = reading_order([it for it, _ in kept])
    return [(replace(kept[j][0], index=i), kept[j][1]) for i, j in enumerate(order)]


def _best(control: Item, found: list[Item], taken: set[int], sources: tuple[str, ...], named: bool) -> int | None:
    """The position of the untaken item of `sources` that overlaps `control` most, at least
    `MIN_BOX_OVERLAP`, and when `named` also matches its label; None when there is none."""
    best, best_overlap = None, MIN_BOX_OVERLAP
    for i, it in enumerate(found):
        if i in taken or it.source not in sources:
            continue
        overlap = box_overlap(control, it)
        if overlap >= best_overlap and (not named or texts_match(control.text, it.text)):
            best, best_overlap = i, overlap
    return best


def _best_icon(control: Item, found: list[Item], taken: set[int]) -> int | None:
    """The icon `_best` pairs with `control`, passing over one whose words name something else."""
    skip = set(taken)
    if control.text:
        skip.update(
            i
            for i, it in enumerate(found)
            if it.source == "icon+ocr" and worded(it.text) and not texts_match(control.text, it.text)
        )
    return _best(control, found, skip, ICON_SOURCES, named=False)


def worded(text: str) -> bool:
    """Whether OCR read a letter or a digit here, as opposed to a glyph such as a close box."""
    return any(ch.isalnum() for ch in text)


def box_overlap(a: Item, b: Item) -> float:
    """Intersection over the smaller box, so a tight control inside a wide text line still counts."""
    wide = min(a.x2, b.x2) - max(a.x1, b.x1)
    tall = min(a.y2, b.y2) - max(a.y1, b.y1)
    smaller = min((a.x2 - a.x1) * (a.y2 - a.y1), (b.x2 - b.x1) * (b.y2 - b.y1))
    return wide * tall / smaller if wide > 0 and tall > 0 and smaller > 0 else 0.0


def texts_match(a: str, b: str) -> bool:
    """One label contains the other, or they share half their words."""
    x, y = " ".join(a.lower().split()), " ".join(b.lower().split())
    if not x or not y:
        return False
    if x in y or y in x:
        return True
    words_x, words_y = set(x.split()), set(y.split())
    return len(words_x & words_y) / min(len(words_x), len(words_y)) >= MIN_TOKEN_OVERLAP


def kept_by_budget(items: list[Item], budget: int) -> list[int]:
    """Which items survive the Choice ceiling: the faintest text blocks go first, then the icons the
    detector was least sure of, and a control is never dropped for either. Their positions, in the
    order given."""
    if len(items) <= budget:
        return list(range(len(items)))
    ranked = sorted(
        range(len(items)), key=lambda i: (items[i].from_ax, items[i].source not in TEXT_SOURCES, items[i].ocr_confidence)
    )
    dropped = set(ranked[: len(items) - budget])
    return [i for i in range(len(items)) if i not in dropped]


def to_items(lines: list[Line], budget: int) -> list[Item]:
    return order_items([Item(0, t, c, *b) for t, c, b in lines])[:budget]


def order_items(items: list[Item]) -> list[Item]:
    """Number items in reading order: rows by the median item height, then left to right."""
    return [replace(items[j], index=i) for i, j in enumerate(reading_order(items))]


def reading_order(items: list[Item]) -> list[int]:
    """Positions of the items in reading order: rows by the median item height, then left to right."""
    heights = sorted(it.y2 - it.y1 for it in items) or [1.0]
    row_h = max(1.0, heights[len(heights) // 2])
    return sorted(range(len(items)), key=lambda i: (round((items[i].y1 + items[i].y2) / 2 / row_h), items[i].x1))


def merge_blocks(lines: list[Line]) -> list[Line]:
    """Join lines that continue a block above them: aligned left edge, small gap, similar height."""
    blocks: list[list] = []  # [text, conf, box, last_line_height]
    for text, conf, (x1, y1, x2, y2) in sorted(lines, key=lambda r: (r[2][1], r[2][0])):
        h = y2 - y1
        best = None
        for block in blocks:
            bx1, _, _, by2 = block[2]
            bh = block[3]
            gap = y1 - by2
            continues = abs(x1 - bx1) < 0.6 * bh and -0.2 * bh < gap < 0.8 * bh and 0.7 < h / max(bh, 1) < 1.4
            if continues and (best is None or gap < best[0]):
                best = (gap, block)
        if best is None:
            blocks.append([text, conf, (x1, y1, x2, y2), h])
            continue
        block = best[1]
        bx1, by1, bx2, _ = block[2]
        block[0] = f"{block[0]} {text}"
        block[1] = min(block[1], conf)
        block[2] = (min(bx1, x1), by1, max(bx2, x2), y2)
        block[3] = h
    return [(t, c, b) for t, c, b, _ in blocks]
