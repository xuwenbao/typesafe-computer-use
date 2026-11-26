"""Turn the display into clickable items: text blocks, icons, and accessibility controls.

One pipeline on every desktop: RapidOCR reads the lines and YOLOv9 finds the icons over the
frontmost window's region, the two are fused line by line, the remaining lines join into blocks,
Florence-2 describes the icons that carry no text, and the result merges with the app's own
controls. The models are in `parse/`; this module decides what to read and what to keep.
"""

from __future__ import annotations

import contextlib
import math
import os
import sys
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

from ..config import Config
from ..models import MENU_BAR_PT, AxNode, Box, Item, Screen
from ..parse import caption as parse_caption
from ..parse import detect as parse_detect
from ..parse import ocr as parse_ocr
from ..parse import weights as parse_weights
from ..parse.fuse import Fuser
from ..platforms.adapter import desktop
from ..platforms.bound import CaptureTimeout, call_within
from ..timing import (
    FIELD_TIMEOUT,
    OCR_AHEAD,
    OCR_RECTS,
    OCR_REGION_PCT,
    SCREENSHOT_TIMEOUT,
    URL_TIMEOUT,
    Timing,
)

# A control that does not answer is treated as absent. A screenshot that does not return ends the step.
FIELD_WAIT = 2.0
URL_WAIT = 2.0
SCREENSHOT_WAIT = 3.0
_MISSED = object()
_TIMEOUT = {"field": FIELD_TIMEOUT, "url": URL_TIMEOUT, "screenshot": SCREENSHOT_TIMEOUT}

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
SETTLE_POLL = 0.2  # seconds between two looks while the screen settles after an action
SETTLE_QUIET = 0.5  # seconds a changed screen must hold still before the next step reads it

_warming: list[threading.Thread] = []
_warm_lock = threading.Lock()
_warm_done = [0]
# perf_counter reading at which this process is taken to have started. The session launcher
# moves it back to its own first line; otherwise it is when this module was imported.
_origin = time.perf_counter()
_marks: dict[str, float] = {}  # seconds from _origin, in the order they were taken
_stages: dict[str, float] = {}  # seconds a warm-up stage took
_session_cpu: dict[str, dict] = {}


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
            self.reading = Perceiver.read_screen(self.screen, self.cache)
        except Exception:  # the step reads the screen itself, and raises what this one swallowed
            self.reading = None
        self.seconds = time.perf_counter() - started

    def wait(self) -> None:
        self._thread.join()

    def fits(self, screen: Screen) -> bool:
        """Whether this read is the one `read_screen` would make of `screen`."""
        guess = self.screen
        return screen.image is guess.image and (screen.scale, screen.app, screen.window) == (guess.scale, guess.app, guess.window)


class Capture:
    """截屏、等画面停稳，并记下启动和每一步的耗时。"""

    @staticmethod
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

        A field or address that does not answer counts as absent. A screenshot that does not answer
        within SCREENSHOT_WAIT seconds raises CaptureTimeout. On Windows those three reads run in a
        process of their own, so a stuck call can be killed instead of landing back on this thread.
        """
        replay = image_path is not None and app is not None
        brought = None
        with Timing.phase(timing, "screenshot"):
            if image_path:
                image = Image.open(image_path).convert("RGB")
            else:
                read_capture = getattr(desktop, "read_capture", None)
                if read_capture is None:
                    # A desktop whose screenshot waits out the previous action, such as the benchmark
                    # VM, cannot be cut off here: that wait is the step, and it outlasts SCREENSHOT_WAIT.
                    if getattr(desktop, "screenshot_waits", False):
                        image = desktop.screenshot()
                    else:
                        image = Capture._bounded(timing, "screenshot", SCREENSHOT_WAIT, desktop.screenshot)
                else:
                    try:
                        brought = read_capture(browser, SCREENSHOT_WAIT, FIELD_WAIT, timing)
                    except CaptureTimeout:
                        if timing is not None:
                            timing[_TIMEOUT["screenshot"]] = 1.0
                        raise
                    image = brought[0]
        scale = desktop.display_scale(image)
        if ahead is not None:
            ahead.read_ahead(image, scale)
        with Timing.phase(timing, "app"):
            if replay:
                frontmost, pid = app, None
            else:
                frontmost, pid = desktop.frontmost_app_and_pid()
                frontmost = app or frontmost
        with Timing.phase(timing, "window"):
            window = None if replay else desktop.frontmost_window_bounds(pid)
            popups = () if replay else desktop.owned_popup_bounds(pid)
        with Timing.phase(timing, "field"):
            if replay or brought is not None:
                field = None if brought is None else brought[1]
            else:
                field = Capture._bounded(timing, "field", FIELD_WAIT, desktop.focused_field, absent=None)
        with Timing.phase(timing, "url"):
            if url is not None:
                page_url = url
            elif replay or brought is not None:
                page_url = None if brought is None else brought[2]
            else:
                page_url = Capture._bounded(timing, "url", URL_WAIT, desktop.browser_url, browser, absent=None)
        return Screen(image=image, scale=scale, app=frontmost, field=field, url=page_url, pid=pid, window=window, popups=popups)

    @staticmethod
    def _bounded(timing: dict[str, float] | None, name: str, seconds: float, fn, *args, absent=None):
        """What `fn` returns, or `absent` when it is still running after `seconds`.

        A screenshot that misses raises CaptureTimeout after the timing flag is set. The sentinel is
        not None: a focused control that is genuinely absent must not look like a timeout.
        """
        found = call_within(seconds, lambda: fn(*args), _MISSED)
        if found is not _MISSED:
            return found
        if timing is not None:
            timing[_TIMEOUT[name]] = 1.0
        if name == "screenshot":
            raise CaptureTimeout(name)
        return absent

    @staticmethod
    def settle(before: Image.Image | None, seconds: float) -> None:
        """Wait after an action until the screen has changed from `before` and held still, `seconds` at most.

        A screen that never changes waits the whole `seconds`: an action whose effect comes late, such
        as a dialog that takes a second to open, still gets all of it. A changed screen counts as settled
        once looks `SETTLE_POLL` apart have agreed for `SETTLE_QUIET`. A desktop whose screenshot waits
        out the action itself, as the benchmark VM's does, only sleeps, and so does one whose screenshot
        fails or comes back another size.
        """
        if before is None or seconds <= SETTLE_QUIET or getattr(desktop, "screenshot_waits", False):
            desktop.sleep_watching(seconds)
            return
        end = time.monotonic() + seconds
        tiles = Capture.tiles_in((0.0, 0.0, float(before.width), float(before.height)))
        first = Capture.thumbnail(before)
        previous: Image.Image | None = None
        still_since = time.monotonic()
        while True:
            try:
                shot = desktop.screenshot()
            except Exception:
                shot = None
            if shot is None or shot.size != before.size:
                desktop.sleep_watching(seconds if previous is None else max(0.0, end - time.monotonic()))
                return
            now = time.monotonic()
            thumb = Capture.thumbnail(shot)
            if previous is None or Capture.changed_tiles(thumb, previous, tiles):
                still_since = now
            elif now - still_since >= SETTLE_QUIET and Capture.changed_tiles(thumb, first, tiles):
                return
            previous = thumb
            left = end - now
            if left <= 0:
                return
            desktop.sleep_watching(min(SETTLE_POLL, left))

    @staticmethod
    def pipeline_record() -> dict:
        """The pipeline and the model files it reads, for a run's or an evaluation's record."""
        return {"pipeline": PIPELINE_VERSION, "weights": parse_weights.WeightStore.record()}

    @staticmethod
    def adopt_start(origin: float, earlier: dict[str, float]) -> None:
        """Take `origin` as this process's start, and keep marks already taken against it.

        The session launcher calls this. Its clock is this process's clock: the launcher is the process.
        """
        global _origin
        _origin = origin
        _marks.update(earlier)

    @staticmethod
    def mark(name: str) -> None:
        """Seconds since the process start, under `name`. A later call with the same name replaces it."""
        _marks[name] = round(time.perf_counter() - _origin, 3)

    @staticmethod
    def startup_report() -> dict:
        """What the run records: the startup marks, the warm-up stages, and the session CPU snapshots."""
        return {"marks": dict(_marks), "stages": dict(_stages), "session_cpu": {k: dict(v) for k, v in _session_cpu.items()}}

    @staticmethod
    def _stage(name: str, fn) -> None:
        """Run `fn` and record how long it took. An error is swallowed: the step reports it when it retries."""
        started = time.perf_counter()
        with contextlib.suppress(Exception):
            fn()
        _stages[name] = round(time.perf_counter() - started, 3)

    @staticmethod
    def snapshot_session_cpu(name: str) -> None:
        """CPU time already used by this process and by the other processes in its session.

        Only Windows has sessions. A process that cannot be read is skipped. The two snapshots, one
        when the child starts and one when the run starts, show whether other programs in the new
        session were busy while jev loaded.
        """
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes

            import psutil

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.ProcessIdToSessionId.argtypes = (wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
            own = wintypes.DWORD()
            kernel.ProcessIdToSessionId(kernel.GetCurrentProcessId(), ctypes.byref(own))
            me, others, count = 0.0, 0.0, 0
            for proc in psutil.process_iter(["pid"]):
                try:
                    sid = wintypes.DWORD()
                    if not kernel.ProcessIdToSessionId(proc.info["pid"], ctypes.byref(sid)) or sid.value != own.value:
                        continue
                    cpu = proc.cpu_times()
                    used = cpu.user + cpu.system
                except (psutil.Error, OSError):
                    continue
                if proc.pid == os.getpid():
                    me += used
                else:
                    others += used
                    count += 1
            _session_cpu[name] = {"self_cpu_s": round(me, 3), "others_cpu_s": round(others, 3), "others": count}
        except Exception:
            return

    @staticmethod
    def start_warm_up() -> None:
        """Load the three models and run each once, while the run starts.

        OCR and the torch models load on different threads: onnxruntime and PyTorch both spend their
        load in native code, so the two do not queue on the GIL. The torch models stay on one thread
        and run one after another, under the one inference lock. Calling this again does nothing.
        """
        if not _warming:
            _warm_done[0] = 0
            Capture.mark("warm_started")
            _warming.extend(
                (
                    threading.Thread(target=Capture._warm_ocr, name="jev-warm-ocr", daemon=True),
                    threading.Thread(target=Capture._warm_torch, name="jev-warm-torch", daemon=True),
                )
            )
            for thread in _warming:
                thread.start()

    @staticmethod
    def _warm_ocr() -> None:
        """OCR on its own. When it is done, and the torch thread is too, the warm-up is finished."""
        page = Image.new("RGB", (parse_detect.IMAGE_SIZE, parse_detect.IMAGE_SIZE), "white")
        Capture._stage("ocr", lambda: parse_ocr.Ocr.recognize_text(page))
        Capture._finish_warm()

    @staticmethod
    def _warm_torch() -> None:
        """PyTorch, then the detector, then the captioner. One thread, so the two models never load at once."""
        page = Image.new("RGB", (parse_detect.IMAGE_SIZE, parse_detect.IMAGE_SIZE), "white")
        icon = page.resize((parse_caption.CROP_PX, parse_caption.CROP_PX))
        Capture._stage("import_torch", Capture._import_torch)
        Capture._stage("import_transformers", Capture._import_transformers)
        Capture._stage("detect", lambda: parse_detect.Detector.detect(page))
        Capture._stage("caption", lambda: parse_caption.Captioner.generate_captions([icon]))
        Capture._finish_warm()

    @staticmethod
    def _finish_warm() -> None:
        """Mark the warm-up finished once both threads have returned."""
        with _warm_lock:
            _warm_done[0] += 1
            if _warm_done[0] == 2:
                Capture.mark("warm_finished")

    @staticmethod
    def _warm_up() -> None:
        """Both halves, on this thread. A model that fails here fails again in the step, which reports it."""
        _warm_done[0] = 0
        Capture._warm_ocr()
        Capture._warm_torch()

    @staticmethod
    def _import_torch() -> None:
        import torch  # noqa: F401

    @staticmethod
    def _import_transformers() -> None:
        import transformers  # noqa: F401

    @staticmethod
    def _await_warm_up(timing: dict[str, float] | None) -> None:
        """Let a warm-up still loading finish first: two threads loading one model would load it twice."""
        pending = [thread for thread in _warming if thread.is_alive()]
        if pending:
            with Timing.phase(timing, "warm_up"):
                for thread in pending:
                    thread.join()

    @staticmethod
    def thumbnail(image: Image.Image, divisor: int = THUMB_DIVISOR) -> Image.Image:
        """A grayscale copy at 1/divisor scale. Box-averaged, so it costs a few milliseconds and smooths
        away the compression noise that would otherwise read as a change."""
        return image.convert("L").reduce(divisor)

    @staticmethod
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

    @staticmethod
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
        a, b = Capture._patch(thumb, tile, divisor), Capture._patch(previous, tile, divisor)
        if a.size != b.size or not a.width or not a.height:
            return True
        diff = ImageChops.difference(a, b)
        if ImageStat.Stat(diff).mean[0] > threshold:
            return True
        strong = sum(diff.histogram()[PIXEL_DIFF + 1 :])
        return strong > CHANGED_SHARE * diff.width * diff.height

    @staticmethod
    def _patch(thumb: Image.Image, tile: Box, divisor: int) -> Image.Image:
        x1, y1, x2, y2 = (round(v / divisor) for v in tile)
        return thumb.crop((x1, y1, max(x2, x1 + 1), max(y2, y1 + 1)))

    @staticmethod
    def changed_tiles(
        thumb: Image.Image,
        previous: Image.Image,
        tiles: list[Box],
        divisor: int = THUMB_DIVISOR,
        threshold: float = TILE_DIFF,
    ) -> list[Box]:
        return [tile for tile in tiles if Capture.tile_changed(thumb, previous, tile, divisor, threshold)]

    @staticmethod
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

    @staticmethod
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
        rects = Capture.settled(
            [Capture.blob_rect(blob, region, tile) for blob in Capture.tile_clusters(changed, tile)], lines, region
        )
        while len(rects) > limit:
            i, j = Capture.closest_pair(rects)
            rects = Capture.settled(
                [Capture.union_box(rects[i], rects[j])] + [r for k, r in enumerate(rects) if k not in (i, j)], lines, region
            )
        return rects

    @staticmethod
    def blob_rect(blob: list[Box], region: Box, tile: float = TILE_PX) -> Box:
        """One blob's bounding box, padded by a tile so a line crossing the edge is read whole, clamped."""
        x1 = min(b[0] for b in blob) - tile
        y1 = min(b[1] for b in blob) - tile
        x2 = max(b[2] for b in blob) + tile
        y2 = max(b[3] for b in blob) + tile
        return (max(region[0], x1), max(region[1], y1), min(region[2], x2), min(region[3], y2))

    @staticmethod
    def settled(rects: list[Box], lines: list[Line], region: Box) -> list[Box]:
        """Grow every rectangle past the lines it would cut, merge the ones that meet, until neither moves.

        Growing can push two rectangles together, and a merged rectangle has edges neither original had,
        which can cut a line neither of them cut. So the two steps run to a fixed point, which they reach:
        a rectangle only ever grows, bounded by the region, and a merge only ever removes one.
        """
        while True:
            grown = [Capture.grown_for_lines(rect, lines, region) for rect in rects]
            merged = Capture.merge_touching(grown)
            if grown == rects and merged == grown:
                return merged
            rects = merged

    @staticmethod
    def merge_touching(rects: list[Box]) -> list[Box]:
        """The rectangles with every overlapping or touching pair replaced by the box around both."""
        out = list(rects)
        merged = True
        while merged:
            merged = False
            for i, a in enumerate(out):
                for j, b in enumerate(out[i + 1 :], start=i + 1):
                    if Capture.boxes_touch(a, b):
                        out = [Capture.union_box(a, b)] + [r for k, r in enumerate(out) if k not in (i, j)]
                        merged = True
                        break
                if merged:
                    break
        return out

    @staticmethod
    def closest_pair(rects: list[Box]) -> tuple[int, int]:
        """Positions of the two rectangles with the smallest gap between them."""
        _, i, j = min(
            (Capture.gap_between(rects[i], rects[j]), i, j) for i in range(len(rects)) for j in range(i + 1, len(rects))
        )
        return i, j

    @staticmethod
    def gap_between(a: Box, b: Box) -> float:
        """Distance between two rectangles, zero when they overlap or touch."""
        dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
        dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
        return math.hypot(dx, dy)

    @staticmethod
    def union_box(a: Box, b: Box) -> Box:
        return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))

    @staticmethod
    def boxes_touch(a: Box, b: Box) -> bool:
        """Overlapping, or meeting along an edge or at a corner: worth reading as one rectangle."""
        return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]

    @staticmethod
    def area_of(box: Box) -> float:
        return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])

    @staticmethod
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
                if not Capture.boxes_intersect(box, (x1, y1, x2, y2)):
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

    @staticmethod
    def boxes_intersect(a: Box, b: Box) -> bool:
        return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]

    @staticmethod
    def merge_reocr(previous: list[Line], fresh: list[Line], rects: list[Box]) -> list[Line]:
        """Previous lines no re-read rectangle touches, plus every line just read inside them."""
        return [ln for ln in previous if not any(Capture.boxes_intersect(ln[2], rect) for rect in rects)] + list(fresh)


class Perceiver:
    """从截图和辅助功能树里认出屏幕上的元素。"""

    @staticmethod
    def goal_echoes(goal: str) -> set[str]:
        """Substrings that identify a screen line as the command that launched this run. None without a
        goal, as an evaluation reads a capture for no task, and an empty substring is in every line."""
        norm = " ".join(goal.lower().split())
        if not norm:
            return set()
        return {norm[:ECHO_CHARS], norm[-ECHO_CHARS:]} if len(norm) >= ECHO_CHARS else {norm}

    @staticmethod
    def is_echo(text: str, echoes: set[str]) -> bool:
        norm = " ".join(text.lower().split())
        return any(e in norm for e in echoes)

    @staticmethod
    def perceive(
        screen: Screen,
        budget: int,
        goal: str,
        timing: dict[str, float] | None = None,
        cache: ParseCache | None = None,
    ) -> list[Item]:
        """Everything worth clicking on this screen: text blocks and icons, plus the app's own controls.

        Fills `screen.ax_refs` on the way, so an item that came from the accessibility tree can be
        pressed through it later. A text block inside a text area gets that control's ref too, so a
        read can ask the document for its text; a click on the block still goes to the pixel.
        The merge renumbers everything, hence the side table over the
        final indices rather than a handle on the item itself, which has to stay printable.
        `screen.covered` is a side table the same way: the items under a popup, and which popup.

        Fills `screen.offscreen` too: labelled controls the app exposes but does not show. They are
        offered on their own, never as items, because nothing on the capture points at them.

        A `cache` carries the previous capture's reading, so only the tiles that changed are read again.
        Pass None to read the whole region every time, which is what a replay and an inspection do.
        """
        found = Perceiver.ocr(screen, budget, goal, cache, timing)
        with Timing.phase(timing, "ax"):
            nodes, hidden = Perceiver.ax_nodes(screen, budget)
            gray = screen.image.convert("L")
            blank = [node for node in nodes if not Perceiver.drawn(gray, node, screen.scale)]
            nodes = [node for node in nodes if node not in blank]
            hidden = hidden + blank
            controls = Perceiver.to_ax_items(nodes, screen.scale)
        merged = ItemFilter.merge_with_origins(found, controls, budget)
        screen.ax_refs.clear()
        screen.ax_refs.update({it.index: nodes[origin].ref for it, origin in merged if origin is not None and nodes[origin].ref})
        Perceiver._attach_document_text(screen, nodes, merged)
        screen.covered.clear()
        screen.covered.update(
            {it.index: nodes[origin].covered_by for it, origin in merged if origin is not None and nodes[origin].covered_by}
        )
        items = [it for it, _ in merged]
        screen.offscreen.clear()
        screen.offscreen.extend(Perceiver.offscreen_controls(hidden, items))
        return items

    @staticmethod
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
            with Timing.phase(timing, "ocr"):
                ahead = cache.take_ahead(screen)
        else:
            ahead = None
        reading = ahead.reading if ahead is not None else Perceiver.read_screen(screen, cache, timing)
        if timing is not None:
            timing[OCR_REGION_PCT] = round(reading.read_pct, 1)
            timing[OCR_RECTS] = reading.rects
            if ahead is not None:
                timing[OCR_AHEAD] = round(ahead.seconds, 3)
        echoes = Perceiver.goal_echoes(goal)
        floor = Config.settings().min_ocr_confidence
        kept: list[Line] = [
            (t.strip(), c, b)
            for t, c, b in reading.lines
            if t.strip()
            and c >= floor
            and not Perceiver.is_echo(t, echoes)
            and not ItemFilter.in_field(Item(0, t, c, *b), screen)
        ]
        texts, icons = Fuser.fuse(kept, reading.icons)
        items = [Item(0, t, c, *b) for t, c, b in ItemFilter.merge_blocks(texts)]
        items += [Item(0, i.text, i.text_confidence or 0.0, *i.box, source="icon+ocr") for i in icons if i.text is not None]
        gray = screen.image.convert("L")
        bare = [i for i in icons if i.text is None and Perceiver.contrast(gray, i.box) >= ICON_MIN_CONTRAST]
        if bare:
            with Timing.phase(timing, "caption"):
                captions = parse_caption.Captioner.caption(
                    screen.image, [i.box for i in bare], cache.captions if cache is not None else None
                )
            items += [
                Item(0, text, i.score, *i.box, role="icon", source="icon") for i, text in zip(bare, captions, strict=True) if text
            ]
        items = ItemFilter.order_items(items)
        return ItemFilter.order_items([items[i] for i in ItemFilter.kept_by_budget(items, budget)])

    @staticmethod
    def contrast(gray: Image.Image, box: Box) -> int:
        """The spread of gray levels inside `box`, 0 for a box with no pixel on the capture."""
        x1, y1 = max(0, math.floor(box[0])), max(0, math.floor(box[1]))
        x2, y2 = min(gray.width, math.ceil(box[2])), min(gray.height, math.ceil(box[3]))
        if x2 <= x1 or y2 <= y1:
            return 0
        low, high = gray.crop((x1, y1, x2, y2)).getextrema()
        return high - low

    @staticmethod
    def read_screen(screen: Screen, cache: ParseCache | None = None, timing: dict[str, float] | None = None) -> Reading:
        """The capture's OCR lines and icon boxes over its region.

        The text is read incrementally (see `ocr_lines`). The icons are detected over the whole region
        again whenever any tile changed, and reused only when none did: the detector finds other boxes
        on a crop than on the whole, so stitching crops together would trade accuracy for little time.
        """
        Capture._await_warm_up(timing)
        with Timing.phase(timing, "ocr"):
            lines, read_pct, rects = Perceiver.ocr_lines(screen, cache)
        if cache is not None and cache.unchanged and cache.icons is not None:
            return Reading(lines, list(cache.icons), read_pct, rects, detected=False)
        with Timing.phase(timing, "detect"):
            icons = Perceiver.detect_region(screen.image, Perceiver.ocr_region(screen))
        if cache is not None:
            cache.icons = list(icons)
        return Reading(lines, icons, read_pct, rects, detected=True)

    @staticmethod
    def detect_region(image: Image.Image, region: Box) -> list[Detection]:
        """The icon boxes in one rectangle of the capture, in full-capture pixels."""
        x1, y1, x2, y2 = (round(v) for v in region)
        crop = image if (x1, y1, x2, y2) == (0, 0, image.width, image.height) else image.crop((x1, y1, x2, y2))
        return [((b[0] + x1, b[1] + y1, b[2] + x1, b[3] + y1), score) for b, score in parse_detect.Detector.detect(crop)]

    @staticmethod
    def ocr_lines(screen: Screen, cache: ParseCache | None = None) -> tuple[list[Line], float, int]:
        """Raw OCR lines for this capture in full-capture pixels, the share of it read, and how many crops.

        Without a cache this reads the region once. With one it reads only the rectangles covering the
        tiles that changed, one OCR call each, unless too much of the screen moved, in which case the
        whole region is cheaper than stitching. A line a re-read rectangle touches is dropped and read
        again whole, because OCR segments a crop slightly differently from the full image.
        """
        region = Perceiver.ocr_region(screen)
        area = float(max(1, screen.image.width * screen.image.height))

        def read_pct(rects: list[Box]) -> float:
            return 100.0 * sum(Capture.area_of(rect) for rect in rects) / area

        if cache is None:
            return Perceiver.ocr_crop(screen.image, region), read_pct([region]), 0

        thumb = Capture.thumbnail(screen.image)
        if not cache.reusable(screen, region, thumb):
            return Perceiver._read_region(screen, region, thumb, cache), read_pct([region]), 0
        tiles = Capture.tiles_in(region)
        changed = Capture.changed_tiles(thumb, cache.thumb, tiles)
        if len(changed) > REOCR_FRACTION * len(tiles):
            return Perceiver._read_region(screen, region, thumb, cache), read_pct([region]), 0
        rects = Capture.reocr_rects(changed, region, cache.lines)
        if not rects:
            cache.store(screen, region, thumb, cache.lines, unchanged=True)
            return cache.lines, 0.0, 0
        if sum(Capture.area_of(rect) for rect in rects) > REOCR_FRACTION * Capture.area_of(region):
            return Perceiver._read_region(screen, region, thumb, cache), read_pct([region]), 0
        fresh = [ln for rect in rects for ln in Perceiver.ocr_crop(screen.image, rect)]
        lines = Capture.merge_reocr(cache.lines, fresh, rects)
        cache.store(screen, region, thumb, lines)
        return lines, read_pct(rects), len(rects)

    @staticmethod
    def _read_region(screen: Screen, region: Box, thumb: Image.Image, cache: ParseCache) -> list[Line]:
        lines = Perceiver.ocr_crop(screen.image, region)
        cache.store(screen, region, thumb, lines)
        return lines

    @staticmethod
    def ocr_region(screen: Screen) -> Box:
        """The part of the capture worth reading, in capture pixels.

        The frontmost window with a margin, joined with the menu bar strip over the same columns and
        clamped to the display. A popup owned by that window, or the focused control's own window, is
        joined in too, so a menu that opens beside the window stays in the picture. Text on the desktop
        and in background windows is noise to the decision, so it is left unread. Clipping the strip to
        the window's x-range is what makes the crop worth anything on a full-height window, whose own
        rectangle already reaches the bottom of the display.

        The cost is that status items to the right of the window, the clock and the menu extras, go
        unread. They stay clickable: the accessibility tree lists them as AXMenuBarItem controls.
        """
        width, height = float(screen.image.width), float(screen.image.height)
        if screen.window is None:
            return (0.0, 0.0, width, height)
        x, y, w, h = screen.window
        scale, margin = screen.scale, REGION_MARGIN_PT
        window = ((x - margin) * scale, (y - margin) * scale, (x + w + margin) * scale, (y + h + margin) * scale)
        left, top, right, bottom = window[0], min(window[1], 0.0), window[2], max(window[3], MENU_BAR_PT * scale)
        for px, py, pw, ph in screen.popups:
            left = min(left, (px - margin) * scale)
            top = min(top, (py - margin) * scale)
            right = max(right, (px + pw + margin) * scale)
            bottom = max(bottom, (py + ph + margin) * scale)
        clamped = (max(0.0, left), max(0.0, top), min(width, right), min(height, bottom))
        return clamped if clamped[2] > clamped[0] and clamped[3] > clamped[1] else (0.0, 0.0, width, height)

    @staticmethod
    def ocr_crop(image: Image.Image, rect: Box) -> list[Line]:
        """OCR one rectangle of the capture. Boxes come back in full-capture pixels, so nothing downstream
        knows a crop happened."""
        x1, y1, x2, y2 = (round(v) for v in rect)
        crop = image if (x1, y1, x2, y2) == (0, 0, image.width, image.height) else image.crop((x1, y1, x2, y2))
        raw = parse_ocr.Ocr.recognize_text(crop)
        return [(text, conf, (b[0] + x1, b[1] + y1, b[2] + x1, b[3] + y1)) for text, conf, b in raw]

    @staticmethod
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

    @staticmethod
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
        return any(Perceiver._ink(gray.crop(part)) for part in parts if part[2] > part[0] and part[3] > part[1])

    @staticmethod
    def _ink(patch: Image.Image) -> bool:
        low, high = patch.getextrema()
        return high - low > BLANK_RANGE

    @staticmethod
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

    @staticmethod
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
                **ItemFilter._carried(node),
            )
            for i, node in enumerate(nodes)
        ]

    @staticmethod
    def _attach_document_text(screen: Screen, nodes: list[AxNode], merged: list[tuple[Item, int | None]]) -> None:
        """Point the text block inside a text area at that control, so a read asks the document.

        The block stays an OCR item. A click on the words still goes to the pixel under them. A block
        that is only a slice of the control, such as a status line, is left on the picture.
        """
        scale = screen.scale
        for node in nodes:
            if node.role != "AXTextArea" or not node.ref:
                continue
            box = Item(0, "", 1.0, node.x * scale, node.y * scale, (node.x + node.w) * scale, (node.y + node.h) * scale)
            control_area = max(box.x2 - box.x1, 0.0) * max(box.y2 - box.y1, 0.0)
            if control_area <= 0:
                continue
            body, body_area = None, 0.0
            for item, origin in merged:
                if origin is not None or item.source != "ocr" or ItemFilter.box_overlap(box, item) < MIN_BOX_OVERLAP:
                    continue
                area = (item.x2 - item.x1) * (item.y2 - item.y1)
                if area > body_area:
                    body, body_area = item, area
            if body is None:
                continue
            tall = (body.y2 - body.y1) >= 0.4 * (box.y2 - box.y1)
            if body_area < 0.2 * control_area and not tall:
                continue
            screen.ax_refs.setdefault(body.index, node.ref)

    @staticmethod
    def ax_items(screen: Screen, budget: int) -> list[Item]:
        """The frontmost app's labelled on-screen controls as items on the capture."""
        return [it for it in Perceiver.to_ax_items(Perceiver.ax_nodes(screen, budget)[0], screen.scale) if it.text]


class ItemFilter:
    """过滤、合并并排序认出的元素。"""

    @staticmethod
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

    @staticmethod
    def _carried_item(item: Item) -> dict:
        """The same facts, copied from the control item onto the text or icon it replaces."""
        return {
            "controls": item.controls,
            "enabled": item.enabled,
            "checked": item.checked,
            "expanded": item.expanded,
            "selected": item.selected,
            "current": item.current,
            "low": item.low,
            "high": item.high,
            "writable": item.writable,
        }

    @staticmethod
    def _carried(node: AxNode) -> dict:
        """The control facts an item keeps when it came from the accessibility tree."""
        return {
            "controls": node.patterns,
            "enabled": node.enabled,
            "checked": node.checked,
            "expanded": node.expanded,
            "selected": node.selected,
            "current": node.current,
            "low": node.low,
            "high": node.high,
            "writable": node.writable,
        }

    @staticmethod
    def merge_sources(found: list[Item], ax_items: list[Item], budget: int | None = None) -> list[Item]:
        """One item per thing: a control that sits on the text block naming it, or on an icon, replaces both."""
        return [it for it, _ in ItemFilter.merge_with_origins(found, ax_items, budget)]

    @staticmethod
    def merge_with_origins(found: list[Item], ax_items: list[Item], budget: int | None = None) -> list[tuple[Item, int | None]]:
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
        if budget is None:
            budget = Config.settings().max_options
        taken: set[int] = set()
        merged: list[tuple[Item, int | None]] = []
        for origin, control in enumerate(ax_items):
            block = ItemFilter._best(control, found, taken, TEXT_SOURCES, named=True)
            if block is not None:
                taken.add(block)
                text = control.text if len(control.text) >= len(found[block].text) else found[block].text
                merged.append(
                    (
                        replace(found[block], text=text, role=control.role, source="ax+ocr", **ItemFilter._carried_item(control)),
                        origin,
                    )
                )
                continue
            icon = ItemFilter._best_icon(control, found, taken)
            if icon is not None and (control.text or found[icon].text):
                taken.add(icon)
                merged.append(
                    (
                        replace(
                            found[icon],
                            text=control.text or found[icon].text,
                            role=control.role,
                            source="ax+icon",
                            **ItemFilter._carried_item(control),
                        ),
                        origin,
                    )
                )
            elif control.text:
                merged.append((control, origin))
        merged += [(it, None) for i, it in enumerate(found) if i not in taken]
        kept = [merged[i] for i in ItemFilter.kept_by_budget([it for it, _ in merged], budget)]
        order = ItemFilter.reading_order([it for it, _ in kept])
        return [(replace(kept[j][0], index=i), kept[j][1]) for i, j in enumerate(order)]

    @staticmethod
    def _best(control: Item, found: list[Item], taken: set[int], sources: tuple[str, ...], named: bool) -> int | None:
        """The position of the untaken item of `sources` that overlaps `control` most, at least
        `MIN_BOX_OVERLAP`, and when `named` also matches its label; None when there is none."""
        best, best_overlap = None, MIN_BOX_OVERLAP
        for i, it in enumerate(found):
            if i in taken or it.source not in sources:
                continue
            overlap = ItemFilter.box_overlap(control, it)
            if overlap >= best_overlap and (not named or ItemFilter.texts_match(control.text, it.text)):
                best, best_overlap = i, overlap
        return best

    @staticmethod
    def _best_icon(control: Item, found: list[Item], taken: set[int]) -> int | None:
        """The icon `_best` pairs with `control`, passing over one whose words name something else."""
        skip = set(taken)
        if control.text:
            skip.update(
                i
                for i, it in enumerate(found)
                if it.source == "icon+ocr" and ItemFilter.worded(it.text) and not ItemFilter.texts_match(control.text, it.text)
            )
        return ItemFilter._best(control, found, skip, ICON_SOURCES, named=False)

    @staticmethod
    def worded(text: str) -> bool:
        """Whether OCR read a letter or a digit here, as opposed to a glyph such as a close box."""
        return any(ch.isalnum() for ch in text)

    @staticmethod
    def box_overlap(a: Item, b: Item) -> float:
        """Intersection over the smaller box, so a tight control inside a wide text line still counts."""
        wide = min(a.x2, b.x2) - max(a.x1, b.x1)
        tall = min(a.y2, b.y2) - max(a.y1, b.y1)
        smaller = min((a.x2 - a.x1) * (a.y2 - a.y1), (b.x2 - b.x1) * (b.y2 - b.y1))
        return wide * tall / smaller if wide > 0 and tall > 0 and smaller > 0 else 0.0

    @staticmethod
    def texts_match(a: str, b: str) -> bool:
        """One label contains the other, or they share half their words."""
        x, y = " ".join(a.lower().split()), " ".join(b.lower().split())
        if not x or not y:
            return False
        if x in y or y in x:
            return True
        words_x, words_y = set(x.split()), set(y.split())
        return len(words_x & words_y) / min(len(words_x), len(words_y)) >= MIN_TOKEN_OVERLAP

    @staticmethod
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

    @staticmethod
    def to_items(lines: list[Line], budget: int) -> list[Item]:
        return ItemFilter.order_items([Item(0, t, c, *b) for t, c, b in lines])[:budget]

    @staticmethod
    def order_items(items: list[Item]) -> list[Item]:
        """Number items in reading order: rows by the median item height, then left to right."""
        return [replace(items[j], index=i) for i, j in enumerate(ItemFilter.reading_order(items))]

    @staticmethod
    def reading_order(items: list[Item]) -> list[int]:
        """Positions of the items in reading order: rows by the median item height, then left to right."""
        heights = sorted(it.y2 - it.y1 for it in items) or [1.0]
        row_h = max(1.0, heights[len(heights) // 2])
        return sorted(range(len(items)), key=lambda i: (round((items[i].y1 + items[i].y2) / 2 / row_h), items[i].x1))

    @staticmethod
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
