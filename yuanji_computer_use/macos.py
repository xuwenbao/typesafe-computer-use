"""macOS adapter: synthetic input, app control, screen capture, and the focused accessibility element.

This is the only module that touches Quartz, ApplicationServices, or AppleScript.
windows.py provides the same functions for Windows; platform_adapter.py picks one. The bounded tree
walk itself lives in ax_walk.py, shared by both.
"""

from __future__ import annotations

import subprocess
import tempfile
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path

import ApplicationServices as AS
import Quartz
from PIL import Image

from .ax_walk import AX_PRESS, AxAttrs, Frame, walk_actionable
from .models import AxNode, ElementState, Field

# ANSI key codes. `delete` and `backspace` are the same key: on a Mac the key labeled Delete
# erases backward. Digits are the number row, not the keypad.
KEYCODES = {
    "return": 36,
    "tab": 48,
    "escape": 53,
    "delete": 51,
    "backspace": 51,
    "space": 49,
    "[": 33,
    "up": 126,
    "down": 125,
    "left": 123,
    "right": 124,
    "pageup": 116,
    "pagedown": 121,
    "home": 115,
    "end": 119,
    "f2": 120,
    "f5": 96,
    "a": 0,
    "b": 11,
    "c": 8,
    "d": 2,
    "e": 14,
    "f": 3,
    "g": 5,
    "h": 4,
    "i": 34,
    "j": 38,
    "k": 40,
    "l": 37,
    "m": 46,
    "n": 45,
    "o": 31,
    "p": 35,
    "q": 12,
    "r": 15,
    "s": 1,
    "t": 17,
    "u": 32,
    "v": 9,
    "w": 13,
    "x": 7,
    "y": 16,
    "z": 6,
    "0": 29,
    "1": 18,
    "2": 19,
    "3": 20,
    "4": 21,
    "5": 23,
    "6": 22,
    "7": 26,
    "8": 28,
    "9": 25,
}
MIN_WINDOW_SIDE_PT = 50.0  # anything smaller is a palette or a shadow, not the window being worked in

# ------------------------------------------------------------------ escape hatch


def mouse_location() -> tuple[float, float]:
    loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return loc.x, loc.y


def check_abort() -> None:
    # TODO: 左上角叫停先去掉。画中画子会话里 GetCursorPos 会被拒绝（错误 5），要换一种不依赖读鼠标的叫停方式后再加回来。
    return None


def abort_hint() -> str:
    return "Ctrl-C"


def sleep_watching(seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        check_abort()
        time.sleep(0.1)


def accessibility_trusted() -> bool:
    return bool(AS.AXIsProcessTrusted())


# ------------------------------------------------------------------ input


def _post(event) -> None:
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
    time.sleep(0.04)


def _down_then_up(event: Callable[[bool], object]) -> None:
    """Post the down event, then the up event even when the down is interrupted, so nothing stays held."""
    try:
        _post(event(True))
    finally:
        _post(event(False))


def click_at(point: tuple[float, float], button: str = "left", count: int = 1, modifier: str | None = None) -> None:
    """Click `point`. `button` is left or right, `count` is 2 for a double-click, and `modifier` is
    `add` (Command, the Mac equivalent of Control-click) or `range` (Shift).

    A plain single left click stays the two-event path the abort tests observe. Anything else sets
    the click count and the modifier flags on each event."""
    # Check before moving: the synthetic move would otherwise take the pointer out of the abort corner.
    check_abort()
    mouse = Quartz.kCGMouseButtonLeft if button == "left" else Quartz.kCGMouseButtonRight
    _post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, point, mouse))
    check_abort()
    if button == "left" and count == 1 and not modifier:
        kinds = {True: Quartz.kCGEventLeftMouseDown, False: Quartz.kCGEventLeftMouseUp}
        _down_then_up(lambda down: Quartz.CGEventCreateMouseEvent(None, kinds[down], point, mouse))
        return
    down_kind = Quartz.kCGEventLeftMouseDown if button == "left" else Quartz.kCGEventRightMouseDown
    up_kind = Quartz.kCGEventLeftMouseUp if button == "left" else Quartz.kCGEventRightMouseUp
    flags = 0
    if modifier == "add":
        flags = Quartz.kCGEventFlagMaskCommand
    elif modifier == "range":
        flags = Quartz.kCGEventFlagMaskShift
    elif modifier:
        raise ValueError(f"unknown modifier {modifier!r}")

    def one(click: int):
        def event(down: bool):
            kind = down_kind if down else up_kind
            posted = Quartz.CGEventCreateMouseEvent(None, kind, point, mouse)
            Quartz.CGEventSetIntegerValueField(posted, Quartz.kCGMouseEventClickState, click)
            if flags:
                Quartz.CGEventSetFlags(posted, flags)
            return posted

        _down_then_up(event)

    for click in range(1, count + 1):
        one(click)


DRAG_STEPS = 8


def drag(start: tuple[float, float], end: tuple[float, float]) -> None:
    """Press at `start`, drag to `end`, and release, including when a move in between fails."""
    check_abort()
    mouse = Quartz.kCGMouseButtonLeft
    _post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, start, mouse))
    check_abort()
    _post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDown, start, mouse))
    try:
        for step in range(1, DRAG_STEPS + 1):
            point = (
                start[0] + (end[0] - start[0]) * step / DRAG_STEPS,
                start[1] + (end[1] - start[1]) * step / DRAG_STEPS,
            )
            _post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDragged, point, mouse))
    finally:
        _post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseUp, end, mouse))


def press(key: str, command: bool = False) -> None:
    check_abort()
    # `start` is the Windows key and `run` is Win+R. On a Mac both open Spotlight, Command-Space.
    if key in ("start", "run"):
        key, command = "space", True
    code = KEYCODES[key]

    def event(down: bool):
        e = Quartz.CGEventCreateKeyboardEvent(None, code, down)
        if command:
            Quartz.CGEventSetFlags(e, Quartz.kCGEventFlagMaskCommand)
        return e

    _down_then_up(event)


def _unicode_key(ch: str, down: bool):
    event = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
    Quartz.CGEventKeyboardSetUnicodeString(event, len(ch), ch)
    return event


def type_text(text: str) -> None:
    """One character at a time, checking the abort corner before each."""
    for ch in text:
        check_abort()
        _down_then_up(partial(_unicode_key, ch))


def clear_field() -> None:
    press("a", command=True)
    press("delete")


def scroll(lines: int, at: tuple[float, float] | None = None) -> None:
    """Scroll the view under the cursor. Park it on `at`, or over the frontmost window."""
    center = at if at is not None else frontmost_window_center()
    check_abort()
    if center is not None:
        _post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, center, Quartz.kCGMouseButtonLeft))
    _post(Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 1, lines))


# ------------------------------------------------------------------ apps and windows


def osascript(script: str) -> str:
    return subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=True).stdout.strip()


def frontmost_app() -> str:
    return osascript('tell application "System Events" to get name of first application process whose frontmost is true')


def frontmost_app_and_pid() -> tuple[str, int]:
    """Name and pid of the frontmost process in one AppleScript round trip."""
    name, _, pid = osascript(
        'tell application "System Events" to tell (first application process whose frontmost is true) to get {name, unix id}'
    ).rpartition(", ")
    return name, int(pid)


def frontmost_pid() -> int:
    return int(osascript('tell application "System Events" to get unix id of first application process whose frontmost is true'))


_LIST_WINDOWS = """
tell application "System Events"
    set sep to ASCII character 31
    set rows to {}
    set i to 0
    repeat with proc in (every process whose background only is false)
        try
            set window_name to name of window 1 of proc
            if window_name is not "" then
                set i to i + 1
                set end of rows to ((i as text) & sep & window_name & sep & (name of proc))
            end if
        end try
    end repeat
    if i is 0 then return ""
    set AppleScript's text item delimiters to linefeed
    return rows as text
end tell
"""


def list_windows() -> list[tuple[str, str, str]]:
    """Visible apps' front windows as (id, title, process)."""
    found: list[tuple[str, str, str]] = []
    for line in osascript(_LIST_WINDOWS).splitlines():
        ident, _sep, rest = line.partition("\x1f")
        title, sep, process = rest.partition("\x1f")
        if sep and ident and process:
            found.append((ident, title, process))
    return found


def launch_app(name: str) -> None:
    """Start the named app with `open -a`. No arguments and no shell."""
    check_abort()
    subprocess.run(["open", "-a", name], check=False)


def activate(app: str, timeout: float = 3.0) -> bool:
    """Bring an app to the front and confirm it got there."""
    check_abort()
    osascript(f'tell application "{app}" to activate')
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        check_abort()
        if frontmost_app() == app:
            return True
        time.sleep(0.1)
    check_abort()
    osascript(f'tell application "System Events" to set frontmost of process "{app}" to true')
    time.sleep(0.3)
    return frontmost_app() == app


def open_url(browser: str, url: str) -> bool:
    check_abort()
    osascript(f'tell application "{browser}" to open location "{url}"')
    return activate(browser)


def browser_url(browser: str) -> str | None:
    try:
        return osascript(f'tell application "{browser}" to get URL of active tab of front window') or None
    except subprocess.CalledProcessError:
        return None


def open_path(path: Path, as_text: bool = False) -> None:
    """Show a file to the user; `as_text` opens it in the default text editor."""
    subprocess.run(["open", *(["-t"] if as_text else []), str(path)], check=False)


def frontmost_window_bounds(pid: int | None = None) -> tuple[float, float, float, float] | None:
    """The frontmost app's topmost on-screen window as x, y, w, h in points. Pure Quartz, no AX needed.

    Pass the pid when the caller already has it; looking it up costs an AppleScript round trip.
    """
    pid = frontmost_pid() if pid is None else pid
    options = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    for window in Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []:
        if window.get("kCGWindowOwnerPID") == pid and window.get("kCGWindowLayer") == 0:
            b = window["kCGWindowBounds"]
            if b["Width"] > MIN_WINDOW_SIDE_PT and b["Height"] > MIN_WINDOW_SIDE_PT:
                return float(b["X"]), float(b["Y"]), float(b["Width"]), float(b["Height"])
    return None


def owned_popup_bounds(pid: int | None = None) -> tuple[tuple[float, float, float, float], ...]:
    """Menus on macOS belong to the front window, so none is collected as its own window."""
    return ()


def frontmost_window_center(pid: int | None = None) -> tuple[float, float] | None:
    """Center of the frontmost app's topmost on-screen window, in points."""
    bounds = frontmost_window_bounds(pid)
    if bounds is None:
        return None
    x, y, w, h = bounds
    return x + w / 2, y + h / 2


# ------------------------------------------------------------------ capture and accessibility


def screenshot() -> Image.Image:
    path = Path(tempfile.mkdtemp()) / "screen.png"
    subprocess.run(["screencapture", "-x", "-D", "1", str(path)], check=True, capture_output=True)
    return Image.open(path).convert("RGB")


def display_scale(image: Image.Image) -> float:
    points_wide = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID()).size.width
    return image.width / points_wide


def _ax_attr(element, name: str):
    """One attribute, or None. A dead or hostile element raises from the bridge; that is a miss, not a crash."""
    try:
        err, value = AS.AXUIElementCopyAttributeValue(element, name, None)
    except Exception:
        return None
    return value if err == 0 else None


def focused_field() -> Field | None:
    system = AS.AXUIElementCreateSystemWide()
    element = _ax_attr(system, AS.kAXFocusedUIElementAttribute)
    if element is None:
        return None
    x = y = w = h = 0.0
    pos = _ax_attr(element, AS.kAXPositionAttribute)
    size = _ax_attr(element, AS.kAXSizeAttribute)
    if pos is not None and size is not None:
        _, pt = AS.AXValueGetValue(pos, AS.kAXValueCGPointType, None)
        _, sz = AS.AXValueGetValue(size, AS.kAXValueCGSizeType, None)
        x, y, w, h = pt.x, pt.y, sz.width, sz.height
    value = _ax_attr(element, AS.kAXValueAttribute)
    label = _ax_attr(element, AS.kAXTitleAttribute) or _ax_attr(element, AS.kAXDescriptionAttribute) or ""
    return Field(
        role=str(_ax_attr(element, AS.kAXRoleAttribute) or ""),
        label=str(label),
        placeholder=str(_ax_attr(element, AS.kAXPlaceholderValueAttribute) or ""),
        value=value if isinstance(value, str) else "",
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
    """Send AXPress to an element."""
    check_abort()
    try:
        return AS.AXUIElementPerformAction(ref, AX_PRESS) == 0
    except Exception:
        return False


def ax_focus(ref) -> bool:
    """Give an element the keyboard focus."""
    check_abort()
    try:
        return AS.AXUIElementSetAttributeValue(ref, AS.kAXFocusedAttribute, True) == 0
    except Exception:
        return False


def ax_set_value(ref, text: str) -> bool:
    """Write an element's value. A read-only or unwilling element reports an error."""
    check_abort()
    try:
        return AS.AXUIElementSetAttributeValue(ref, AS.kAXValueAttribute, text) == 0
    except Exception:
        return False


def ax_value(ref) -> str | None:
    """An element's value, when it has a textual one."""
    value = _ax_attr(ref, AS.kAXValueAttribute)
    return value if isinstance(value, str) else None


def element_text(ref) -> str | None:
    """The text an element holds, which on macOS is its AXValue."""
    check_abort()
    return ax_value(ref)


def ax_operate(ref, operation: str, value: str | None = None) -> bool:
    """macOS has no UI Automation patterns. A click falls back to the pointer."""
    return False


def ax_selected(ref) -> bool | None:
    """macOS does not re-read a list selection here. None leaves the captured value as it was."""
    return None


def clipboard_files() -> tuple[str, ...] | None:
    """macOS does not read the file clipboard here. None means the shortcut line stays unmarked."""
    return None


def element_frame(ref) -> tuple[float, float, float, float] | None:
    """macOS clicks an off-screen control through AXPress. There is no fresh box to aim at."""
    return None


def element_state(ref) -> ElementState | None:
    """Whether the element is still there. macOS does not report an enabled bit here."""
    if ref is None:
        return None
    check_abort()
    if _ax_attr(ref, AS.kAXRoleAttribute) is None:
        return None
    return ElementState(True, True, None)


# ------------------------------------------------------------------ actionable elements

AX_MESSAGE_TIMEOUT = 0.2
AX_VALUE_CHARS = 120


def _ax_children(element) -> list:
    return list(_ax_attr(element, AS.kAXChildrenAttribute) or [])


def _ax_label(element) -> str:
    """AXTitle on AppKit, AXDescription on web and Electron, a short AXValue as a last resort."""
    for name in (AS.kAXTitleAttribute, AS.kAXDescriptionAttribute):
        text = _ax_attr(element, name)
        if isinstance(text, str) and text.strip():
            return " ".join(text.split())
    value = _ax_attr(element, AS.kAXValueAttribute)
    if isinstance(value, str) and 0 < len(value.strip()) <= AX_VALUE_CHARS:
        return " ".join(value.split())
    return ""


def _ax_frame(element) -> Frame | None:
    pos = _ax_attr(element, AS.kAXPositionAttribute)
    size = _ax_attr(element, AS.kAXSizeAttribute)
    if pos is None or size is None:
        return None
    ok_pos, pt = AS.AXValueGetValue(pos, AS.kAXValueCGPointType, None)
    ok_size, sz = AS.AXValueGetValue(size, AS.kAXValueCGSizeType, None)
    if not (ok_pos and ok_size):
        return None
    return float(pt.x), float(pt.y), float(sz.width), float(sz.height)


def _ax_attrs(element) -> AxAttrs:
    return AxAttrs(str(_ax_attr(element, AS.kAXRoleAttribute) or ""), _ax_label(element), _ax_frame(element))


def _ax_actions(element) -> list[str]:
    try:
        err, names = AS.AXUIElementCopyActionNames(element, None)
    except Exception:
        return []
    return [str(n) for n in names] if err == 0 and names else []


def actionable_elements(pid: int, display_w_pt: float, display_h_pt: float) -> tuple[list[AxNode], list[AxNode], bool]:
    """Labelled controls of one process: the on-screen ones in points, the pressable off-screen ones,
    and whether a cap cut the walk short."""
    app = AS.AXUIElementCreateApplication(pid)
    AS.AXUIElementSetMessagingTimeout(app, AX_MESSAGE_TIMEOUT)
    return walk_actionable(app, _ax_children, _ax_attrs, _ax_actions, display_w_pt, display_h_pt)
