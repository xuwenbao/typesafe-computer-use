"""Windows adapter: synthetic input, app control, screen capture, and the focused UI Automation element.

Experimental. This is the only module that touches pywin32, UI Automation, or Win32 SendInput;
macos.py provides the same functions for macOS, and platform_adapter.py picks one.
The bounded tree walk itself lives in ax_walk.py, shared by both.

Once the process is per-monitor DPI aware (set below), Windows has no Retina-style point and pixel
split, so a screen point here already is a capture pixel. The pure rules (the role names, what counts
as pressable, which window belongs to an app, the input events) are plain functions, tested off Windows.
"""

from __future__ import annotations

import ctypes
import functools
import os
import shutil
import subprocess
import time
import webbrowser
from contextlib import suppress
from pathlib import Path

import psutil
import pywintypes
import uiautomation as auto
import win32api
import win32con
import win32gui
import win32process
from PIL import Image, ImageGrab

from .ax_walk import AX_PRESS, AxAttrs, AxFacts, Frame, walk_actionable
from .config import settings
from .models import Abort, AxNode, ElementState, Field, Missed
from .uia_patterns import OPERATION_PATTERN, PATTERN_IDS
from .uia_roles import CONTROL_TYPE_TO_ROLE as CONTROL_TYPE_TO_ROLE
from .uia_roles import focused_role, pressable, role_for
from .wheel import LINES_PER_NOTCH as LINES_PER_NOTCH
from .wheel import WHEEL_DELTA as WHEEL_DELTA
from .wheel import wheel_delta

with suppress(AttributeError, OSError):  # pre-8.1 Windows without shcore, or awareness set by the host process
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE

MIN_WINDOW_SIDE_PT = 50.0  # anything smaller is a palette or a shadow, not the window being worked in
EVENT_GAP = 0.04  # seconds between synthetic events, as on macOS
CLICK_TOLERANCE_PX = 1  # how far the cursor may land from its target before a click is refused

# The role table itself lives in uia_roles.py, shared with the WAA VM walk. The names below are
# re-exported so this module still answers `role_for` and `pressable`.

# The browsers the site catalog can open, by the name CLICKER_BROWSER gives and their executable.
# The frontmost app is reported by the same name, so the classifier sees one browser, not two.
BROWSER_EXES = {"Google Chrome": "chrome", "Microsoft Edge": "msedge", "Firefox": "firefox", "Brave Browser": "brave"}

# Virtual keys for the keys the actions press. macOS's Command becomes Control, except Command-[,
# a browser's Back, which is Alt-Left on Windows.
VK = {
    "return": 0x0D,
    "tab": 0x09,
    "escape": 0x1B,
    "delete": 0x2E,
    "[": 0xDB,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "pageup": 0x21,
    "pagedown": 0x22,
    "home": 0x24,
    "end": 0x23,
    "a": 0x41,
    "c": 0x43,
    "f": 0x46,
    "s": 0x53,
    "v": 0x56,
    "x": 0x58,
    "z": 0x5A,
    "space": 0x20,
    "backspace": 0x08,
    "f2": 0x71,
    "f5": 0x74,
    "start": 0x5B,  # VK_LWIN: the Start menu
    **{ch: ord(ch.upper()) for ch in "bdefghijklmnopqrtuwy"},
    **{d: ord(d) for d in "0123456789"},
}
VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_ALT = 0x12
EXTENDED_KEYS = {0x2E, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x5B}  # Delete, the page keys, the arrows, and Start
COMMAND_CHORDS = {"[": (VK_ALT, VK["left"])}

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_WHEEL = 0x0800


# ------------------------------------------------------------------ pure rules


def exe_stem(name: str) -> str:
    """`Chrome.exe`, `chrome` and ` CHROME ` are one process name."""
    stem = name.strip().lower()
    return stem.removesuffix(".exe")


def app_matches(app: str, process_name: str) -> bool:
    """Whether a process is the app a caller named: the executable itself, or a known browser by
    its product name. Exact, never a substring, and never by window title, so a Chrome tab named
    after an app is not that app."""
    wanted = exe_stem(app)
    browsers = {name.lower(): exe for name, exe in BROWSER_EXES.items()}
    return exe_stem(process_name) in {wanted, browsers.get(wanted, wanted)}


def display_name(process_name: str) -> str:
    """The name to report for a process: a known browser by its product name, anything else as is."""
    stem = exe_stem(process_name)
    return next((name for name, exe in BROWSER_EXES.items() if exe == stem), process_name)


def key_events(key: str, command: bool = False) -> list[tuple[int, int]]:
    """(virtual key, flags) for pressing a key, with its modifiers held around it."""
    # `run` is the Run dialog: the Windows key held around R. It is not a key of its own.
    if key == "run" and not command:
        win = 0x5B  # VK_LWIN
        letter = VK["r"]
        return [
            (win, KEYEVENTF_EXTENDEDKEY),
            (letter, 0),
            (letter, KEYEVENTF_KEYUP),
            (win, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP),
        ]
    keys = COMMAND_CHORDS.get(key, (VK_CONTROL, VK[key])) if command else (VK[key],)
    down = [(vk, KEYEVENTF_EXTENDEDKEY if vk in EXTENDED_KEYS else 0) for vk in keys]
    up = [(vk, flags | KEYEVENTF_KEYUP) for vk, flags in reversed(down)]
    return down + up


def unicode_events(text: str) -> list[tuple[int, int]]:
    """(UTF-16 code unit, flags) for typing text as characters, not keys, so any script and any
    emoji arrive whatever the keyboard layout. A character outside the BMP is two units."""
    data = text.encode("utf-16-le")
    units = [int.from_bytes(data[i : i + 2], "little") for i in range(0, len(data), 2)]
    return [(unit, flags) for unit in units for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)]


def landed(target: tuple[int, int], actual: tuple[float, float]) -> bool:
    return abs(actual[0] - target[0]) <= CLICK_TOLERANCE_PX and abs(actual[1] - target[1]) <= CLICK_TOLERANCE_PX


def placed_origin(rect: tuple[float, float, float, float], work: tuple[float, float, float, float]) -> tuple[float, float] | None:
    """Where to move `rect` so it sits inside `work`, or None when it already does.

    Both are x, y, w, h. The size stays. A window larger than the work area keeps the work
    area's origin, so it still starts on the visible desktop instead of off to one side.
    """
    x, y, w, h = rect
    left, top, width, height = work
    nx = left if w > width else min(max(x, left), left + width - w)
    ny = top if h > height else min(max(y, top), top + height - h)
    if nx == x and ny == y:
        return None
    return nx, ny


# ------------------------------------------------------------------ SendInput


class _MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_int32),
        ("dy", ctypes.c_int32),
        ("mouseData", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _KeyboardInput(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_uint16),
        ("wScan", ctypes.c_uint16),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _InputUnion(ctypes.Union):
    _fields_ = [("mi", _MouseInput), ("ki", _KeyboardInput)]


class _Input(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("u", _InputUnion)]


@functools.cache
def _user32():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendInput.argtypes = (ctypes.c_uint, ctypes.POINTER(_Input), ctypes.c_int)
    user32.SendInput.restype = ctypes.c_uint
    return user32


def _send(event: _Input) -> None:
    """One synthetic event, then the same pause macOS leaves. A blocked event (a UAC prompt or the
    lock screen has the input desktop) raises rather than going missing."""
    if _user32().SendInput(1, ctypes.byref(event), ctypes.sizeof(_Input)) != 1:
        raise ctypes.WinError(ctypes.get_last_error())
    time.sleep(EVENT_GAP)


def _key(vk: int = 0, scan: int = 0, flags: int = 0) -> _Input:
    return _Input(type=INPUT_KEYBOARD, u=_InputUnion(ki=_KeyboardInput(wVk=vk, wScan=scan, dwFlags=flags)))


def _mouse(flags: int, data: int = 0) -> _Input:
    return _Input(type=INPUT_MOUSE, u=_InputUnion(mi=_MouseInput(mouseData=data & 0xFFFFFFFF, dwFlags=flags)))


# ------------------------------------------------------------------ escape hatch


def mouse_location() -> tuple[float, float]:
    x, y = win32api.GetCursorPos()
    return float(x), float(y)


def check_abort() -> None:
    if os.environ.get("JEV_SESSION") == "1":
        from .windows_session import session_cancelled

        if session_cancelled():
            raise Abort("the session was cancelled")
    corner = settings().abort_corner_px
    x, y = mouse_location()
    if x <= corner and y <= corner:
        raise Abort("mouse in top-left corner")


def abort_hint() -> str:
    return "Ctrl-C, or slam the mouse into the top-left corner"


def sleep_watching(seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        check_abort()
        time.sleep(0.1)


def accessibility_trusted() -> bool:
    """Windows has no Accessibility permission gate; confirm UI Automation can reach the desktop instead."""
    try:
        return auto.GetRootControl() is not None
    except Exception:
        return False


# ------------------------------------------------------------------ input


def _move(point: tuple[int, int]) -> None:
    win32api.SetCursorPos(point)
    time.sleep(EVENT_GAP)


def click_at(point: tuple[float, float], button: str = "left", count: int = 1, modifier: str | None = None) -> None:
    """Move, read back where the cursor landed, then press and release there.

    A cursor that did not reach the target (another desktop has the input, or the point is off
    every monitor) means the click would land somewhere unknown, so nothing is pressed. `button`
    is left or right, `count` is 2 for a double-click, and `modifier` is `add` (Control) or
    `range` (Shift), held around the click and released even when the click fails.
    """
    target = (round(point[0]), round(point[1]))
    _move(target)
    actual = mouse_location()
    if not landed(target, actual):
        raise Missed(f"the cursor went to {actual}, not {target}")
    if button == "left" and count == 1 and not modifier:
        _send(_mouse(MOUSEEVENTF_LEFTDOWN))
        _send(_mouse(MOUSEEVENTF_LEFTUP))
        return
    down, up = {
        "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    }[button]
    vk = {"add": VK_CONTROL, "range": VK_SHIFT}.get(modifier or "")
    if modifier and vk is None:
        raise ValueError(f"unknown modifier {modifier!r}")
    try:
        if vk is not None:
            _send(_key(vk=vk))
        for _ in range(count):
            _send(_mouse(down))
            _send(_mouse(up))
    finally:
        if vk is not None:
            _send(_key(vk=vk, flags=KEYEVENTF_KEYUP))


DRAG_STEPS = 8  # the button stays down across these moves, so a drop lands where the last one did


def drag(start: tuple[float, float], end: tuple[float, float]) -> None:
    """Press at `start`, move to `end`, and release, including when a move in between fails."""
    origin = (round(start[0]), round(start[1]))
    dest = (round(end[0]), round(end[1]))
    _move(origin)
    actual = mouse_location()
    if not landed(origin, actual):
        raise Missed(f"the cursor went to {actual}, not {origin}")
    _send(_mouse(MOUSEEVENTF_LEFTDOWN))
    try:
        for step in range(1, DRAG_STEPS + 1):
            x = round(origin[0] + (dest[0] - origin[0]) * step / DRAG_STEPS)
            y = round(origin[1] + (dest[1] - origin[1]) * step / DRAG_STEPS)
            _move((x, y))
    finally:
        _send(_mouse(MOUSEEVENTF_LEFTUP))


def press(key: str, command: bool = False) -> None:
    for vk, flags in key_events(key, command):
        _send(_key(vk=vk, flags=flags))


def clipboard_files() -> tuple[str, ...]:
    """File names on the clipboard, without their directories. Empty when it holds no files."""
    import win32clipboard

    try:
        win32clipboard.OpenClipboard()
    except Exception:
        return ()
    try:
        if not win32clipboard.IsClipboardFormatAvailable(win32con.CF_HDROP):
            return ()
        paths = win32clipboard.GetClipboardData(win32con.CF_HDROP)
    except Exception:
        return ()
    finally:
        win32clipboard.CloseClipboard()
    return tuple(Path(path).name for path in paths)


def type_text(text: str) -> None:
    for unit, flags in unicode_events(text):
        _send(_key(scan=unit, flags=flags))


def clear_field() -> None:
    press("a", command=True)
    press("delete")


def scroll(lines: int, at: tuple[float, float] | None = None) -> None:
    """Scroll the view under the cursor. Park it on `at`, or over the frontmost window."""
    center = at if at is not None else frontmost_window_center()
    if center is not None:
        _move((round(center[0]), round(center[1])))
    _send(_mouse(MOUSEEVENTF_WHEEL, wheel_delta(lines)))


# ------------------------------------------------------------------ apps and windows


def unsigned_pid(raw: int) -> int:
    """A process id from the window manager. The API returns a DWORD, which arrives signed."""
    return int(raw) & 0xFFFFFFFF


def _process_name(pid: int) -> str:
    if pid <= 0:
        return ""
    try:
        return psutil.Process(pid).name()
    except (psutil.Error, ValueError):
        return ""


def _window_pid(hwnd: int) -> int:
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    return unsigned_pid(pid)


def frontmost_app() -> str:
    name, _ = frontmost_app_and_pid()
    return name


def frontmost_app_and_pid() -> tuple[str, int]:
    """Name and pid of the foreground window's process."""
    pid = frontmost_pid()
    return display_name(_process_name(pid)), pid


def frontmost_pid() -> int:
    return _window_pid(win32gui.GetForegroundWindow())


def _find_window(app: str) -> int | None:
    """The topmost visible, titled top-level window whose process is `app` (see app_matches)."""
    found: list[int] = []

    def visit(hwnd, _):  # returns None, so the enumeration runs to the end without raising
        titled = not found and win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd)
        if titled and app_matches(app, _process_name(_window_pid(hwnd))):
            found.append(hwnd)

    win32gui.EnumWindows(visit, None)  # top of the z-order first
    return found[0] if found else None


def list_windows() -> list[tuple[str, str, str]]:
    """Visible titled top-level windows as (id, title, process), frontmost first."""
    found: list[tuple[str, str, str]] = []

    def visit(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = (win32gui.GetWindowText(hwnd) or "").strip()
        process = _process_name(_window_pid(hwnd))
        if title and process:
            found.append((str(len(found) + 1), title, process))

    win32gui.EnumWindows(visit, None)
    return found


def launch_app(name: str) -> None:
    """Start `name` with ShellExecute. No arguments and no shell, so the name cannot carry a command."""
    check_abort()
    started = int(win32api.ShellExecute(0, "open", name, None, None, win32con.SW_SHOWNORMAL))
    if started <= 32:
        raise OSError(f"could not start {name}")


def activate(app: str, timeout: float = 3.0) -> bool:
    """Bring an app's visible window to the front and confirm that window is the foreground one.

    A process name on the foreground window is not enough: the window may be restored off the
    desktop this session can show. A task-scheduler process is often refused `SetForegroundWindow`,
    so a momentary Alt is sent once to let this session accept the change.
    """
    hwnd = _find_window(app)
    if hwnd is None:
        return False
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    _move_into_work_area(hwnd)
    _claim_foreground(hwnd)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if win32gui.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.1)
    return win32gui.GetForegroundWindow() == hwnd


def _work_area() -> tuple[float, float, float, float]:
    """The desktop's work area as x, y, w, h. The full screen stands in when Windows will not say."""
    rect = ctypes.wintypes.RECT()
    if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):  # SPI_GETWORKAREA
        return float(rect.left), float(rect.top), float(rect.right - rect.left), float(rect.bottom - rect.top)
    return 0.0, 0.0, float(win32api.GetSystemMetrics(0)), float(win32api.GetSystemMetrics(1))


def _move_into_work_area(hwnd: int) -> None:
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    except pywintypes.error:
        return
    origin = placed_origin((float(left), float(top), float(right - left), float(bottom - top)), _work_area())
    if origin is None:
        return
    win32gui.SetWindowPos(hwnd, win32con.HWND_TOP, int(origin[0]), int(origin[1]), 0, 0, win32con.SWP_NOSIZE)


def _claim_foreground(hwnd: int) -> None:
    with suppress(Exception):
        win32gui.SetForegroundWindow(hwnd)
    if win32gui.GetForegroundWindow() == hwnd:
        return
    with suppress(Exception):
        _send(_key(VK_ALT))
    with suppress(Exception):
        win32gui.SetForegroundWindow(hwnd)
    with suppress(Exception):
        _send(_key(VK_ALT, flags=KEYEVENTF_KEYUP))


def open_url(browser: str, url: str) -> bool:
    exe = next((exe for name, exe in BROWSER_EXES.items() if name.lower() == browser.strip().lower()), None)
    if exe and shutil.which(exe):
        subprocess.Popen([exe, url])
    else:
        webbrowser.open(url)
    return activate(browser)


def browser_url(browser: str) -> str | None:
    """The address bar's text, read through UI Automation. No AppleScript-style API exists for this
    on Windows, so it depends on the browser exposing an Edit control named for the address bar,
    which Chrome, Edge, and Firefox do. Chrome and Edge show it without the scheme."""
    hwnd = _find_window(browser)
    if hwnd is None:
        return None
    try:
        window = auto.ControlFromHandle(hwnd)
        edit = window.EditControl(searchDepth=20, RegexName="(?i)address")
        if not edit.Exists(0, 0):
            return None
        return _ui_value(edit) or None
    except Exception:
        return None


def open_path(path: Path, as_text: bool = False) -> None:
    """Show a file to the user in its default app; a .txt already opens in the text editor."""
    os.startfile(path)


def _window_rect(hwnd: int) -> tuple[float, float, float, float] | None:
    """A visible window's bounds, or None when it is missing or only a shadow."""
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    except pywintypes.error:
        return None
    w, h = float(right - left), float(bottom - top)
    if w > MIN_WINDOW_SIDE_PT and h > MIN_WINDOW_SIDE_PT:
        return float(left), float(top), w, h
    return None


def frontmost_window_bounds(pid: int | None = None) -> tuple[float, float, float, float] | None:
    """The foreground window's bounds as x, y, w, h in screen pixels.

    Pass the pid when the caller already has it, to confirm the foreground window still belongs
    to that process rather than one that grabbed focus since.
    """
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd or (pid is not None and _window_pid(hwnd) != pid):
        return None
    return _window_rect(hwnd)


def _focused_root() -> int:
    """The top-level window that holds the focused control, or 0 when focus cannot be read."""
    try:
        element = auto.GetFocusedControl()
    except Exception:
        return 0
    if element is None:
        return 0
    try:
        handle = int(getattr(element, "NativeWindowHandle", 0) or 0)
    except (TypeError, ValueError):
        return 0
    if not handle:
        return 0
    try:
        return int(win32gui.GetAncestor(handle, win32con.GA_ROOT) or 0)
    except pywintypes.error:
        return 0


def _popup_hwnds(pid: int | None) -> list[int]:
    """Owned top-level windows of the foreground window, plus the focused control's own root.

    A menu that opens beside the window is a separate window. Both kinds have to be visible, belong
    to the same process, and be large enough not to be a shadow.
    """
    foreground = win32gui.GetForegroundWindow()
    if not foreground:
        return []
    owner_pid = _window_pid(foreground)
    if pid is not None and owner_pid != pid:
        return []
    found: list[int] = []
    seen = {foreground}

    def consider(hwnd: int) -> None:
        if not hwnd or hwnd in seen or not win32gui.IsWindowVisible(hwnd):
            return
        if _window_pid(hwnd) != owner_pid or _window_rect(hwnd) is None:
            return
        seen.add(hwnd)
        found.append(hwnd)

    def visit(hwnd, _lparam) -> bool:
        try:
            if win32gui.GetWindow(hwnd, win32con.GW_OWNER) == foreground:
                consider(hwnd)
        except pywintypes.error:
            pass
        return True

    win32gui.EnumWindows(visit, None)
    consider(_focused_root())
    return found


def owned_popup_bounds(pid: int | None = None) -> tuple[tuple[float, float, float, float], ...]:
    """Those popups as x, y, w, h in screen pixels. Empty when the foreground window is not this process."""
    return tuple(rect for hwnd in _popup_hwnds(pid) if (rect := _window_rect(hwnd)) is not None)


def frontmost_window_center(pid: int | None = None) -> tuple[float, float] | None:
    """Center of the frontmost app's topmost on-screen window, in screen pixels."""
    bounds = frontmost_window_bounds(pid)
    if bounds is None:
        return None
    x, y, w, h = bounds
    return x + w / 2, y + h / 2


# ------------------------------------------------------------------ capture, OCR, and accessibility


def screenshot() -> Image.Image:
    """The primary monitor, the one macOS calls the main display."""
    return ImageGrab.grab().convert("RGB")


def display_scale(image: Image.Image) -> float:
    """Per-monitor DPI awareness keeps every coordinate in physical pixels, so a screen point
    already is a capture pixel; unlike macOS, there is no separate points-vs-pixels scale."""
    return 1.0


def read_capture(browser: str, shot_wait: float, detail_wait: float, timing: dict | None = None):
    """Screenshot, focused control, and address bar from a process this one can kill.

    The screenshot is kept when the later read does not return. No screenshot raises CaptureTimeout.
    The call stays off this thread, so a stuck UI Automation call cannot be delivered back here.
    """
    import sys
    import tempfile
    import threading

    from .bound import CaptureLines, SilentEvent, assemble_capture, await_capture

    folder = tempfile.mkdtemp(prefix="jev-capture-")
    png = os.path.join(folder, "shot.png")
    shot, done = SilentEvent(), SilentEvent()
    lines = CaptureLines(shot, done)
    proc = subprocess.Popen(
        [sys.executable, "-m", "typesafe_computer_use.capture_proc", png, browser],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    def pump() -> None:
        if proc.stdout is None:
            return
        for line in proc.stdout:
            lines.feed(line)

    threading.Thread(target=pump, name="jev-capture", daemon=True).start()
    try:
        got_shot, got_detail = await_capture(shot, done, shot_wait, detail_wait, lambda: _stop_process(proc))
        image = None
        if got_shot and os.path.exists(png):
            with Image.open(png) as opened:
                image = opened.convert("RGB")
        found, field_data, url = assemble_capture(got_shot, lines.detail if got_detail else None, image, timing)
        field = None if field_data is None else Field(**field_data)
        return found, field, url
    finally:
        _stop_process(proc)
        shot.close()
        done.close()
        shutil.rmtree(folder, ignore_errors=True)


def _stop_process(proc) -> None:
    with suppress(OSError):
        proc.kill()
    with suppress(Exception):
        proc.wait(timeout=2)


def _pattern(element, pattern_id):
    """A UI Automation pattern, or None when the element does not support it or has died."""
    try:
        return element.GetPattern(pattern_id)
    except Exception:
        return None


def _ui_value(element) -> str | None:
    pattern = _pattern(element, auto.PatternId.ValuePattern)
    try:
        return pattern.Value if pattern is not None else None
    except Exception:
        return None


def _ui_placeholder(element) -> str:
    legacy = _pattern(element, auto.PatternId.LegacyIAccessiblePattern)
    try:
        return (legacy.Description or "") if legacy is not None else ""
    except Exception:
        return ""


def _ui_default_action(element) -> str:
    legacy = _pattern(element, auto.PatternId.LegacyIAccessiblePattern)
    try:
        return (legacy.DefaultAction or "") if legacy is not None else ""
    except Exception:
        return ""


def _legacy_state(element) -> int | None:
    """The LegacyIAccessible state, or None when the control does not say."""
    legacy = _pattern(element, auto.PatternId.LegacyIAccessiblePattern)
    if legacy is None:
        return None
    try:
        return int(legacy.State)
    except Exception:
        return None


def focused_field() -> Field | None:
    try:
        element = auto.GetFocusedControl()
    except Exception:
        return None
    if element is None:
        return None
    frame = _ui_frame(element)
    x, y, w, h = frame if frame is not None else (0.0, 0.0, 0.0, 0.0)
    return Field(
        role=focused_role(getattr(element, "ControlTypeName", ""), _legacy_state(element)),
        label=(getattr(element, "Name", "") or "").strip(),
        placeholder=_ui_placeholder(element),
        value=_ui_value(element) or "",
        x=x,
        y=y,
        w=w,
        h=h,
        ref=element,
    )


# ------------------------------------------------------------------ acting on an element

# An element accepts these directly, so a press lands on the control the app declared rather than
# on whatever pixel happens to sit at its center. Every one of them is best effort: the element may
# be dead, the app may refuse, and the bridge raises on both. False means "use synthetic input".


def ax_press(ref) -> bool:
    """Invoke an element, or run its named legacy default action: the same rule as `pressable`."""
    invoke = _pattern(ref, auto.PatternId.InvokePattern)
    try:
        if invoke is not None:
            invoke.Invoke()
            return True
        if _ui_default_action(ref).strip():
            _pattern(ref, auto.PatternId.LegacyIAccessiblePattern).DoDefaultAction()
            return True
    except Exception:
        pass
    return False


def ax_focus(ref) -> bool:
    """Give an element the keyboard focus."""
    try:
        return bool(ref.SetFocus())
    except Exception:
        return False


def ax_set_value(ref, text: str) -> bool:
    """Write an element's value. A read-only or unwilling element reports an error."""
    pattern = _pattern(ref, auto.PatternId.ValuePattern)
    try:
        return pattern is not None and bool(pattern.SetValue(text))
    except Exception:
        return False


def ax_value(ref) -> str | None:
    """An element's value, when it has a textual one."""
    return _ui_value(ref)


def ax_operate(ref, operation: str, value: str | None = None) -> bool:
    """Run one UIA operation. False when the element does not support it or refuses it."""
    check_abort()
    pattern_name = OPERATION_PATTERN.get(operation)
    if pattern_name is None:
        return False
    pattern = _pattern(ref, PATTERN_IDS[pattern_name])
    if pattern is None:
        return False
    try:
        if operation == "toggle":
            pattern.Toggle()
        elif operation == "select":
            pattern.Select()
        elif operation == "add_select":
            pattern.AddToSelection()
        elif operation == "expand_toggle":
            if int(getattr(pattern, "ExpandCollapseState", 0)) == 0:
                pattern.Expand()
            else:
                pattern.Collapse()
        elif operation == "scroll_into_view":
            pattern.ScrollIntoView()
        elif operation == "set_range":
            number = float(value) if value is not None else None
            if number is None:
                return False
            low, high = float(pattern.Minimum), float(pattern.Maximum)
            if number < low or number > high:
                return False
            pattern.SetValue(number)
            return abs(float(pattern.Value) - number) < 1e-3
        else:
            return False
    except Exception:
        return False
    return True


def element_state(ref) -> ElementState | None:
    """Whether this element is still there, on screen, and enabled. None when it cannot be asked."""
    return read_uia_state(ref)


def element_frame(ref) -> Frame | None:
    """The element's box in screen pixels, read now. None when it cannot be read.

    The box captured with the screen is where the element was then. After ScrollIntoView the
    element may have moved, and only this read knows the place a click would land.
    """
    if ref is None:
        return None
    check_abort()
    return _ui_frame(ref)


def read_uia_state(element) -> ElementState | None:
    """The same facts from an element or a stand-in. A dead element, or one that raises, is unknown."""
    if element is None:
        return None
    try:
        offscreen = bool(element.IsOffscreen)
        try:
            enabled = bool(element.IsEnabled)
        except Exception:
            enabled = None
    except Exception:
        return None
    return ElementState(True, not offscreen, enabled)


# ------------------------------------------------------------------ actionable elements


def _ui_children(element) -> list:
    try:
        return list(element.GetChildren())
    except Exception:
        return []


def _ui_frame(element) -> Frame | None:
    try:
        rect = element.BoundingRectangle
    except Exception:
        return None
    if rect is None:
        return None
    return float(rect.left), float(rect.top), float(rect.width()), float(rect.height())


def _ui_attrs(element) -> AxAttrs:
    label = (getattr(element, "Name", "") or "").strip()
    return AxAttrs(role_for(getattr(element, "ControlTypeName", "")), label, _ui_frame(element))


def _toggle_state(element) -> bool | None:
    pattern = _pattern(element, PATTERN_IDS["toggle"])
    try:
        return bool(int(pattern.ToggleState)) if pattern is not None else None
    except Exception:
        return None


def _expand_state(element) -> bool | None:
    pattern = _pattern(element, PATTERN_IDS["expand_collapse"])
    try:
        # 0 is collapsed. Anything expanded counts as open.
        return int(pattern.ExpandCollapseState) != 0 if pattern is not None else None
    except Exception:
        return None


def _range_bounds(element) -> tuple[float | None, float | None]:
    pattern = _pattern(element, PATTERN_IDS["range_value"])
    try:
        return (float(pattern.Minimum), float(pattern.Maximum)) if pattern is not None else (None, None)
    except Exception:
        return None, None


def ax_selected(ref) -> bool | None:
    """Whether this element is in the selection now. None when it cannot say."""
    if ref is None:
        return None
    check_abort()
    return _selected(ref)


def _selected(element) -> bool | None:
    """Whether a list row is in the selection. None when it has no SelectionItem pattern."""
    pattern = _pattern(element, PATTERN_IDS["selection_item"])
    try:
        return bool(pattern.IsSelected) if pattern is not None else None
    except Exception:
        return None


def _writable(element) -> bool | None:
    pattern = _pattern(element, PATTERN_IDS["value"])
    try:
        return (not bool(pattern.IsReadOnly)) if pattern is not None else None
    except Exception:
        return None


def _ui_actions(element) -> AxFacts:
    """Pressability, the operations the element supports, and the state a click needs to see."""
    names: list[str] = []
    for name, pattern_id in PATTERN_IDS.items():
        if _pattern(element, pattern_id) is not None:
            names.append(name)
    has_invoke = "invoke" in names
    if pressable(has_invoke, "" if has_invoke else _ui_default_action(element)):
        names.insert(0, AX_PRESS)
    try:
        enabled: bool | None = bool(element.IsEnabled)
    except Exception:
        enabled = None
    low, high = _range_bounds(element)
    return AxFacts(
        tuple(dict.fromkeys(names)),
        enabled,
        _toggle_state(element),
        _expand_state(element),
        _ui_value(element),
        low,
        high,
        _writable(element),
        _selected(element),
    )


def actionable_elements(pid: int, display_w_pt: float, display_h_pt: float) -> tuple[list[AxNode], list[AxNode], bool]:
    """Labelled controls of the foreground window and its popups, in screen pixels, and the pressable
    off-screen ones. Best effort: a window that refuses UI Automation, or belongs to a different
    process, or has none, yields nothing. A popup that refuses is skipped; the others are still walked."""
    hwnd = win32gui.GetForegroundWindow()
    if _window_pid(hwnd) != pid:
        return [], [], False
    found: list[AxNode] = []
    hidden: list[AxNode] = []
    capped = False
    for root_hwnd in (hwnd, *_popup_hwnds(pid)):
        try:
            root = auto.ControlFromHandle(root_hwnd)
        except Exception:
            continue
        nodes, off, cut = walk_actionable(root, _ui_children, _ui_attrs, _ui_actions, display_w_pt, display_h_pt)
        found.extend(nodes)
        hidden.extend(off)
        capped = capped or cut
    return found, hidden, capped
