"""A Windows child session: jev runs there, and the console session only hosts the window.

The controller enables the child session, shows it in a Remote Desktop window, and talks to the
jev process over a named pipe. It does not import the platform adapter and it never sends input.
Nothing here falls back to driving the console desktop.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from .models import Abort

KINDS = ("task", "log", "progress", "question", "answer", "cancel", "done")
PIPE_NAME = "jev-session"
TASK_NAME = "jev-child-session"
RDP_PROGID = "MsTscAx.MsTscAx.10"  # a ProgID this machine registers; the interface name is not one
# GetExtendedSettings and AdvancedSettings9 live on these, not on the control's default IDispatch.
RDP_CLIENT_IIDS = (
    "{28904001-04B6-436C-A55B-0AF1A0883DC9}",  # IMsRdpClient9
    "{7ED92C39-EB38-4927-A70A-708AC5A59321}",  # IMsRdpClient10
    "{B2A5B5CE-3461-444A-91D4-ADD26D070638}",  # IMsRdpClient7
)
RDP_EXTENDED_IID = "{302D8188-0052-4807-806A-362B628F9AC5}"  # IMsRdpExtendedSettings
AX_WINDOW_CLASSES = ("AtlAxWin140", "AtlAxWin110", "AtlAxWin100", "AtlAxWin")
WINDOW_TITLE = "jev child session"
MASK_TEXT = "智能体执行中"
FRAME_X, FRAME_Y = 80, 80
# Client area of the frame, and the child session's own pixels. The title bar and borders sit
# outside this rectangle. A desktop larger than the client makes the control present its own view.
FRAME_WIDTH, FRAME_HEIGHT = 1280, 800
DESKTOP_WIDTH, DESKTOP_HEIGHT = FRAME_WIDTH, FRAME_HEIGHT
CHILD_LOGON_SECONDS = 90
ERROR_PIPE_CONNECTED = 535
STILL_ACTIVE = 259

_cancelled = False


@dataclass(frozen=True)
class SessionFacts:
    """What has to be true before a child session is worth opening. All of it is asked up front."""

    edition: str
    rdp_localhost: bool
    smart_card: bool
    child_running: bool


def rejection(facts: SessionFacts) -> str | None:
    """Why this machine cannot host the session, or None when it can.

    Home editions have no Remote Desktop host. A smart-card logon will not auto-logon into the
    child session. One child session at a time. A machine that cannot accept a localhost Remote
    Desktop connection cannot show the window.
    """
    edition = facts.edition.strip().lower()
    if "home" in edition or edition == "core" or edition.startswith("core"):
        return "Windows Home cannot host a child session"
    if facts.smart_card:
        return "a smart card logon cannot open a child session"
    if facts.child_running:
        return "a child session is already running"
    if not facts.rdp_localhost:
        return "remote desktop cannot connect to localhost"
    return None


def should_refuse_session(jev_session: bool, in_console: bool) -> bool:
    """A jev that was started for the child session must not keep running on the console desktop."""
    return bool(jev_session) and bool(in_console)


def child_command(executable: str) -> str:
    """jev inside the child session. `--act` is what makes it type and click."""
    return f'"{executable}" -m yuanji_computer_use --act'


def child_wait_failure(elapsed: float, connected: bool, found: bool, limit: float, seen_connected: bool) -> str | None:
    """Why the wait for the child session's desktop should stop, or None to keep waiting.

    `found` means explorer.exe is up in the child session, so the wait is over. A connection that
    drops after it had come up is a different failure from a logon that never finishes.
    """
    if found:
        return None
    if seen_connected and not connected:
        return "the picture-in-picture window disconnected before the child session logged on"
    if elapsed >= limit:
        return "the child session did not finish logging on"
    return None


def host_stop(relay_done: bool, window_open: bool, child_alive: bool = True) -> str | None:
    """Why the parent should leave its message loop. None means keep pumping.

    A closed window is the user's cancel. The relay finishing is a normal end. The child process
    leaving first means it quit without saying it was done.
    """
    if not window_open:
        return "closed"
    if relay_done:
        return "done"
    if not child_alive:
        return "exited"
    return None


def encode(kind: str, **payload) -> str:
    """One session message as a single line of JSON."""
    if kind not in KINDS:
        raise ValueError(f"{kind!r} is not a session message")
    return json.dumps({"type": kind, **payload}, ensure_ascii=False) + "\n"


def decode(line: str) -> dict:
    """The message in one line. Anything else is refused, not guessed."""
    data = json.loads(line)
    if not isinstance(data, dict) or data.get("type") not in KINDS:
        raise ValueError(f"not a session message: {line!r}")
    return data


def session_cancelled() -> bool:
    """Whether the controller has told this process to stop."""
    return _cancelled


def note_cancel() -> None:
    """Record a cancel so `check_abort` takes the existing abort path."""
    global _cancelled
    _cancelled = True


def reset_cancel() -> None:
    """Test hook. A real process starts with no cancel."""
    global _cancelled
    _cancelled = False


class SessionChannel:
    """The child's view of the pipe: ask a question, or notice a cancel.

    `send` and `recv` move one line each. A queue pair stands in for the pipe in tests.
    """

    def __init__(self, send: Callable[[str], None], recv: Callable[[], str]):
        self._send = send
        self._recv = recv

    def ask(self, question: str) -> str:
        """Write a question and wait. A cancel raises Abort and arms `check_abort`."""
        self._send(encode("question", text=question))
        message = decode(self._recv())
        if message["type"] == "cancel":
            note_cancel()
            raise Abort("the session was cancelled")
        if message["type"] != "answer":
            raise ValueError(f"expected an answer, got {message['type']}")
        return str(message.get("text") or "")

    def cancel(self) -> None:
        """The controller tells the child to stop."""
        self._send(encode("cancel"))


def refuse_if_console() -> None:
    """Exit before any screenshot or input when this jev is still on the console session."""
    if should_refuse_session(True, in_console_session()):
        sys.exit("clicker session is running in the console session")


def run_controller(argv: list[str]) -> None:
    """Open the child session and relay its pipe until the run is done.

    A failed precondition exits with the reason. A failed launch exits too. Neither starts jev
    on the console desktop.
    """
    if not argv:
        sys.exit("clicker session needs the goal")
    reason = rejection(collect_facts())
    if reason:
        sys.exit(reason)
    pipe = create_session_pipe(PIPE_NAME)
    hwnd = None
    client = None
    try:
        enable_child_session()
        hwnd, client = open_child_window()
        connect_child_session(client)
        deadline = time.monotonic() + CHILD_LOGON_SECONDS

        def pump() -> None:
            _pump_messages()
            if not _window_open(hwnd):
                raise OSError("the picture-in-picture window was closed")

        wait_for_child_explorer(pump, deadline, client)
        pid = launch_in_session(PIPE_NAME)
        _show_execution_mask(hwnd)
        errors: list[BaseException] = []

        def relay_target() -> None:
            try:
                _relay(pipe, " ".join(argv))
            except BaseException as exc:  # the host thread reports it; a thread must not sys.exit
                errors.append(exc)

        relay = threading.Thread(target=relay_target, name="jev-session-relay", daemon=True)
        relay.start()
        why = _host_until(relay, hwnd, pipe, pid)
        if errors:
            raise errors[0]
        if why == "exited":
            raise OSError("the child process exited before the run finished")
    except SystemExit:
        raise
    except Exception as e:
        sys.exit(f"the child session did not start: {e}")
    finally:
        _hide_execution_mask()
        _delete_launch_task()
        _close_window(hwnd, client)


def child_startup() -> tuple[str, Callable[[str], str], Callable[[str, str], None]]:
    """The jev inside the session: the goal, an ask that uses the pipe, and a done callback.

    A background read records cancel, so `check_abort` sees it even between questions.
    """
    pipe_name = os.environ.get("JEV_SESSION_PIPE") or PIPE_NAME
    client = attach_session_pipe(pipe_name)
    incoming: queue.Queue[dict] = queue.Queue()

    def pump() -> None:
        while True:
            message = decode(_read_line(client))
            if message["type"] == "cancel":
                note_cancel()
            incoming.put(message)

    threading.Thread(target=pump, name="jev-session", daemon=True).start()
    first = incoming.get()
    if first["type"] != "task":
        sys.exit("the session did not send a task")

    def ask(question: str) -> str:
        _write_line(client, encode("question", text=question))
        message = incoming.get()
        if message["type"] == "cancel" or session_cancelled():
            note_cancel()
            raise Abort("the session was cancelled")
        return str(message.get("text") or "")

    def finish(outcome: str, folder: str) -> None:
        _write_line(client, encode("done", text=outcome, folder=folder))

    def report(line: str) -> None:
        _write_line(client, encode("log", text=line))

    _bind_action_report(report)
    return str(first.get("goal") or ""), ask, finish


def end_child(finish: Callable[[str, str], None], outcome: str, folder: str) -> None:
    """Report the outcome, then leave. Closing HTTPS clients on the way out can wait forever."""
    finish(outcome, folder)
    os._exit(130 if outcome.startswith("aborted") else 0)


def collect_facts() -> SessionFacts:
    """Edition, localhost Remote Desktop, smart-card logon, and an existing child session."""
    return SessionFacts(_edition(), _rdp_localhost(), _smart_card_logon(), _child_running())


def in_console_session() -> bool:
    """Whether this process's session is the active console session."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    session = wintypes.DWORD()
    if not kernel.ProcessIdToSessionId(kernel.GetCurrentProcessId(), ctypes.byref(session)):
        raise OSError(ctypes.get_last_error(), "ProcessIdToSessionId failed")
    return session.value == kernel.WTSGetActiveConsoleSessionId()


def enable_child_session() -> None:
    """Allow one child session on this logon. The call comes from the parent session."""
    import ctypes

    wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
    if not wts.WTSEnableChildSessions(True):
        raise OSError(ctypes.get_last_error(), "WTSEnableChildSessions failed")


_FRAME_CLASS = "JevChildFrame"
_frame_wndproc = None
_frame_hwnd = None


def open_child_window():
    """A normal small window with the Remote Desktop control hosted inside it. Returns (hwnd, client).

    The frame is an ordinary overlapped window. The control lives in a child `AtlAxWin`, filling the
    frame's client area, so connecting cannot replace this window with the control's own full-screen
    frame. `AtlAxWinInit` registers the host class. The child window's text is the ProgID it instantiates.
    """
    import ctypes
    from ctypes import wintypes

    import pythoncom
    import win32com.client
    import win32con

    pythoncom.CoInitialize()
    atl = ctypes.WinDLL("atl", use_last_error=True)
    atl.AtlAxWinInit.argtypes = ()
    atl.AtlAxWinInit.restype = ctypes.c_bool
    if not atl.AtlAxWinInit():
        raise OSError("AtlAxWinInit failed")

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.CreateWindowExW.argtypes = (
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        wintypes.LPVOID,
    )
    user32.CreateWindowExW.restype = wintypes.HWND
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
    kernel.GetModuleHandleW.restype = wintypes.HMODULE
    hwnd = _create_frame()
    instance = kernel.GetModuleHandleW("atl") or None
    child_style = win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.WS_CLIPSIBLINGS
    host = None
    for klass in AX_WINDOW_CLASSES:
        host = user32.CreateWindowExW(
            0, klass, RDP_PROGID, child_style, 0, 0, FRAME_WIDTH, FRAME_HEIGHT, hwnd, None, instance, None
        )
        if host:
            break
    if not host:
        raise OSError(ctypes.get_last_error(), f"CreateWindowExW failed for {list(AX_WINDOW_CLASSES)}")
    _fit_hosted_control(hwnd)

    atl.AtlAxGetControl.argtypes = (wintypes.HWND, ctypes.POINTER(ctypes.c_void_p))
    atl.AtlAxGetControl.restype = ctypes.c_long
    unknown = ctypes.c_void_p()
    hr = atl.AtlAxGetControl(host, ctypes.byref(unknown))
    if hr < 0 or not unknown.value:
        raise OSError(f"AtlAxGetControl failed ({hr:#x})")
    # The host hands back IUnknown. Its default IDispatch is the old scriptable interface:
    # AdvancedSettings9 is on a later client interface, and ConnectToChildSession is a named
    # property of IMsRdpExtendedSettings. This library has no GetExtendedSettings method.
    import pywintypes

    pointer = pythoncom.ObjectFromAddress(unknown.value)
    client = None
    last = None
    for iid in RDP_CLIENT_IIDS:
        try:
            face = pointer.QueryInterface(pywintypes.IID(iid), pythoncom.IID_IDispatch)
        except pythoncom.com_error as error:
            last = error
            continue
        client = win32com.client.Dispatch(face)
        break
    if client is None:
        raise OSError(f"the Remote Desktop control did not expose a client interface: {last}")
    extended = pointer.QueryInterface(pywintypes.IID(RDP_EXTENDED_IID), pythoncom.IID_IDispatch)
    _set_child_session(extended)
    # The DirectX presenter paints its own surface over the monitor and reads back black to a GDI
    # grab. Force the GDI presenter so the picture stays inside this window.
    _set_extended_bool(extended, "EnableHardwareMode", False)
    _size_control_to_window(hwnd, client)
    user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.ShowWindow.restype = wintypes.BOOL
    user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE: visible, without taking the foreground
    return hwnd, client


def _create_frame():
    """The ordinary top-level window. Resizing it resizes the hosted control with it."""
    global _frame_hwnd, _frame_wndproc

    import pywintypes
    import win32api
    import win32con
    import win32gui

    def wndproc(hwnd, msg, wparam, lparam):
        if msg == 0x0046:  # WM_WINDOWPOSCHANGING: the control must not grow this frame to the monitor
            _clamp_window_pos(lparam)
            return 0
        if msg == win32con.WM_SIZE:
            _fit_hosted_control(hwnd)
            _sync_execution_mask(hwnd)
            return 0
        if msg == 0x0003:  # WM_MOVE
            _sync_execution_mask(hwnd)
            return 0
        if msg == win32con.WM_CLOSE:
            win32gui.DestroyWindow(hwnd)
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    instance = win32api.GetModuleHandle(None)
    _frame_wndproc = wndproc  # the class does not keep the callable alive by itself
    window_class = win32gui.WNDCLASS()
    window_class.hInstance = instance
    window_class.lpszClassName = _FRAME_CLASS
    window_class.lpfnWndProc = _frame_wndproc
    window_class.hbrBackground = win32con.COLOR_WINDOW + 1
    window_class.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
    try:
        atom = win32gui.RegisterClass(window_class)
    except pywintypes.error as error:
        if error.winerror != 1410:  # CLASS_ALREADY_EXISTS: a previous run registered it
            raise
        atom = _FRAME_CLASS
    x, y, width, height = frame_outer()
    _frame_hwnd = win32gui.CreateWindowEx(
        0,
        atom,
        WINDOW_TITLE,
        win32con.WS_OVERLAPPEDWINDOW | win32con.WS_CLIPCHILDREN | win32con.WS_VISIBLE,
        x,
        y,
        width,
        height,
        0,
        0,
        instance,
        None,
    )
    return _frame_hwnd


def _clamp_window_pos(lparam) -> None:
    """Keep a resize from turning the frame into the full monitor."""
    import ctypes
    from ctypes import wintypes

    class WindowPos(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("after", wintypes.HWND),
            ("x", ctypes.c_int),
            ("y", ctypes.c_int),
            ("cx", ctypes.c_int),
            ("cy", ctypes.c_int),
            ("flags", wintypes.UINT),
        ]

    pos = ctypes.cast(lparam, ctypes.POINTER(WindowPos)).contents
    if pos.flags & 0x0001:  # SWP_NOSIZE
        return
    x, y, width, height = frame_outer()
    if pos.cx > width + 8 or pos.cy > height + 8:
        pos.x, pos.y, pos.cx, pos.cy = x, y, width, height


def _reveal_hosted_windows(hwnd, client) -> None:
    """Show the control's hidden view and keep that view inside the frame.

    The frame itself is left where the user puts it, including behind other windows.
    """
    import win32gui

    applied = False
    while win32gui.IsWindow(hwnd):
        _show_hidden_children(hwnd)
        _clamp_view_to_client(hwnd)
        _sync_execution_mask(hwnd)
        with contextlib.suppress(Exception):
            if client.FullScreen:
                client.FullScreen = False
        if not applied:
            with contextlib.suppress(Exception):
                if int(client.Connected) == 1:
                    _set_desktop_resolution(client, live=True)
                    applied = True
        time.sleep(0.2)


def _show_hidden_children(hwnd) -> None:
    """Show a hidden remote view without showing or activating the frame."""
    import win32con
    import win32gui

    child = win32gui.GetWindow(hwnd, win32con.GW_CHILD)
    while child:
        if not win32gui.IsWindowVisible(child):
            win32gui.ShowWindow(child, 8)  # SW_SHOWNA
        _show_hidden_children(child)
        child = win32gui.GetWindow(child, win32con.GW_HWNDNEXT)


_MASK_CLASS = "JevSessionMask"
_mask_hwnd = None
_mask_wndproc = None
_mask_on = False
_mask_place = None
_mask_action = ""
_action_report: Callable[[str], None] | None = None


def mask_line(line: str) -> str:
    """The sentence under the title. The driver's prefix only marks who took the action."""
    return line.removeprefix("driver: ").strip()


def report_action(line: str) -> None:
    """Tell the picture window what jev just did. Outside a child session this does nothing."""
    if _action_report is None:
        return
    with contextlib.suppress(Exception):
        _action_report(line)


def _bind_action_report(report: Callable[[str], None] | None) -> None:
    global _action_report
    _action_report = report


def _show_execution_mask(frame) -> None:
    """Cover the picture while jev works. The window can still sit behind other windows."""
    global _mask_on

    _mask_on = True
    _ensure_execution_mask(frame)
    _sync_execution_mask(frame)


def _hide_execution_mask() -> None:
    """Take the mask down. Closing the frame destroys it either way."""
    global _mask_on, _mask_action

    _mask_on = False
    _mask_action = ""
    if not _mask_hwnd:
        return
    with contextlib.suppress(Exception):
        import win32gui

        if win32gui.IsWindow(_mask_hwnd):
            win32gui.ShowWindow(_mask_hwnd, 0)  # SW_HIDE


def _ensure_execution_mask(owner) -> None:
    """An owned layered window. It stays above the picture, and goes behind other apps with it."""
    global _mask_hwnd, _mask_wndproc

    import win32con
    import win32gui

    if _mask_hwnd and win32gui.IsWindow(_mask_hwnd):
        return

    def wndproc(hwnd, msg, wparam, lparam):
        if msg == win32con.WM_ERASEBKGND:
            return 1
        if msg == win32con.WM_PAINT:
            _paint_execution_mask(hwnd)
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    _mask_wndproc = wndproc
    instance = win32gui.GetModuleHandle(None)
    window_class = win32gui.WNDCLASS()
    window_class.hInstance = instance
    window_class.lpszClassName = _MASK_CLASS
    window_class.lpfnWndProc = _mask_wndproc
    try:
        atom = win32gui.RegisterClass(window_class)
    except Exception as error:
        if getattr(error, "winerror", None) != 1410:
            raise
        atom = _MASK_CLASS
    _mask_hwnd = win32gui.CreateWindowEx(
        win32con.WS_EX_LAYERED | win32con.WS_EX_NOACTIVATE | win32con.WS_EX_TOOLWINDOW,
        atom,
        "",
        win32con.WS_POPUP,
        0,
        0,
        0,
        0,
        owner,
        0,
        instance,
        None,
    )
    win32gui.SetLayeredWindowAttributes(_mask_hwnd, 0, 190, 0x2)  # LWA_ALPHA


def _sync_execution_mask(frame) -> None:
    """Match the mask to the frame's client area, and hide it when the frame is minimized."""
    global _mask_place

    import win32gui

    if not _mask_on or not _mask_hwnd or not win32gui.IsWindow(_mask_hwnd) or not win32gui.IsWindow(frame):
        return
    if win32gui.IsIconic(frame) or not win32gui.IsWindowVisible(frame):
        win32gui.ShowWindow(_mask_hwnd, 0)
        return
    left, top, right, bottom = win32gui.GetClientRect(frame)
    origin = win32gui.ClientToScreen(frame, (0, 0))
    place = (origin[0], origin[1], right - left, bottom - top)
    if place != _mask_place:
        win32gui.SetWindowPos(_mask_hwnd, 0, place[0], place[1], place[2], place[3], 0x0010)  # SWP_NOACTIVATE
        win32gui.InvalidateRect(_mask_hwnd, None, True)
        _mask_place = place
    win32gui.ShowWindow(_mask_hwnd, 8)  # SW_SHOWNA


def _note_mask_action(line: str) -> None:
    """Remember the latest action and ask the mask to draw it."""
    global _mask_action

    _mask_action = mask_line(line)
    if not _mask_hwnd:
        return
    with contextlib.suppress(Exception):
        import win32gui

        if win32gui.IsWindow(_mask_hwnd):
            win32gui.InvalidateRect(_mask_hwnd, None, True)


def _paint_execution_mask(hwnd) -> None:
    """A dim cover: the title, and under it the action jev is taking."""
    import ctypes

    import win32con
    import win32gui

    hdc, paint = win32gui.BeginPaint(hwnd)
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    brush = win32gui.CreateSolidBrush(0x00101010)
    win32gui.FillRect(hdc, (left, top, right, bottom), brush)
    win32gui.DeleteObject(brush)
    win32gui.SetBkMode(hdc, win32con.TRANSPARENT)
    gdi = ctypes.WinDLL("gdi32", use_last_error=True)
    gdi.CreateFontW.argtypes = (ctypes.c_int,) * 13 + (ctypes.c_wchar_p,)
    gdi.CreateFontW.restype = ctypes.c_void_p
    gdi.SelectObject.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    gdi.SelectObject.restype = ctypes.c_void_p
    gdi.DeleteObject.argtypes = (ctypes.c_void_p,)
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.DrawTextW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint)
    width, height = right - left, bottom - top
    margin = max(48, width // 10)
    title_h, gap, action_h = 44, 16, 52
    block = title_h + (gap + action_h if _mask_action else 0)
    y = top + max(0, (height - block) // 2)
    title = (left + margin, y, right - margin, y + title_h)
    # DT_CENTER | DT_VCENTER | DT_SINGLELINE | DT_NOPREFIX
    _draw_mask_text(gdi, user, hdc, MASK_TEXT, title, -32, 600, 0x00FFFFFF, 0x0001 | 0x0004 | 0x0020 | 0x0800)
    if _mask_action:
        action = (left + margin, y + title_h + gap, right - margin, y + title_h + gap + action_h)
        # DT_CENTER | DT_WORDBREAK | DT_END_ELLIPSIS | DT_NOPREFIX
        _draw_mask_text(gdi, user, hdc, _mask_action, action, -18, 400, 0x00C8C8C8, 0x0001 | 0x0010 | 0x8000 | 0x0800)
    win32gui.EndPaint(hwnd, paint)


def _draw_mask_text(
    gdi,
    user,
    hdc,
    text: str,
    box: tuple[int, int, int, int],
    height: int,
    weight: int,
    color: int,
    flags: int,
) -> None:
    """One centered line or block, in Microsoft YaHei UI."""
    import ctypes

    import win32gui

    font = gdi.CreateFontW(height, 0, 0, 0, weight, 0, 0, 0, 1, 0, 0, 0, 0, "Microsoft YaHei UI")
    dc = int(hdc)
    old = gdi.SelectObject(dc, font)
    win32gui.SetTextColor(hdc, color)
    user.DrawTextW(dc, text, -1, ctypes.byref(_rect(box)), flags)
    gdi.SelectObject(dc, old)
    gdi.DeleteObject(font)


def _rect(box: tuple[int, int, int, int]):
    from ctypes import wintypes

    return wintypes.RECT(*box)


def frame_outer() -> tuple[int, int, int, int]:
    """Outer size whose client area is the 1280 by 800 desktop. The title bar sits outside it."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    rect = wintypes.RECT(0, 0, DESKTOP_WIDTH, DESKTOP_HEIGHT)
    style = 0x00CF0000 | 0x02000000  # WS_OVERLAPPEDWINDOW | WS_CLIPCHILDREN
    exstyle = 0
    dpi = 96
    get_dpi = getattr(user32, "GetDpiForSystem", None)
    if get_dpi is not None:
        get_dpi.restype = ctypes.c_uint
        dpi = int(get_dpi()) or 96
    adjust = getattr(user32, "AdjustWindowRectExForDpi", None)
    ok = False
    if adjust is not None:
        adjust.argtypes = (
            ctypes.POINTER(wintypes.RECT),
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
            ctypes.c_uint,
        )
        adjust.restype = wintypes.BOOL
        ok = bool(adjust(ctypes.byref(rect), style, False, exstyle, dpi))
    if not ok:
        user32.AdjustWindowRectEx.argtypes = (
            ctypes.POINTER(wintypes.RECT),
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        )
        user32.AdjustWindowRectEx.restype = wintypes.BOOL
        user32.AdjustWindowRectEx(ctypes.byref(rect), style, False, exstyle)
    return FRAME_X, FRAME_Y, int(rect.right - rect.left), int(rect.bottom - rect.top)


def desktop_oversized(width: int, height: int) -> bool:
    """The session picked a resolution larger than the desktop this window is built for."""
    return int(width) > DESKTOP_WIDTH + 8 or int(height) > DESKTOP_HEIGHT + 8


_desktop_apply_at = 0.0


def _set_desktop_resolution(client, *, live: bool) -> None:
    """Set the child desktop to DESKTOP_WIDTH by DESKTOP_HEIGHT.

    DesktopWidth applies before Connect. Once the session is up, a child session often keeps the
    monitor's resolution anyway, so UpdateSessionDisplaySettings asks it to change. A session
    that ignores the request must not be asked again on every size event.
    """
    global _desktop_apply_at

    with contextlib.suppress(Exception):
        client.DesktopWidth = DESKTOP_WIDTH
        client.DesktopHeight = DESKTOP_HEIGHT
    if not live:
        return
    now = time.monotonic()
    if now - _desktop_apply_at < 1:
        return
    _desktop_apply_at = now
    with contextlib.suppress(Exception):
        client.UpdateSessionDisplaySettings(DESKTOP_WIDTH, DESKTOP_HEIGHT, DESKTOP_WIDTH, DESKTOP_HEIGHT, 0, 100, 100)


def _clamp_view_to_client(hwnd) -> None:
    """Resize any remote view that is larger than its parent. That view is the fullscreen surface."""
    import win32con
    import win32gui

    def walk(parent) -> None:
        left, top, right, bottom = win32gui.GetClientRect(parent)
        width, height = right - left, bottom - top
        if width <= 0 or height <= 0:
            return
        child = win32gui.GetWindow(parent, win32con.GW_CHILD)
        while child:
            rect = win32gui.GetWindowRect(child)
            if rect[2] - rect[0] > width + 2 or rect[3] - rect[1] > height + 2:
                win32gui.MoveWindow(child, 0, 0, width, height, True)
            walk(child)
            child = win32gui.GetWindow(child, win32con.GW_HWNDNEXT)

    with contextlib.suppress(Exception):
        walk(hwnd)


def _fit_hosted_control(hwnd) -> None:
    """Stretch the ActiveX host, and the control window inside it, over the frame's client area."""
    import win32con
    import win32gui

    host = win32gui.GetWindow(hwnd, win32con.GW_CHILD)
    if not host:
        return
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    width, height = right - left, bottom - top
    win32gui.MoveWindow(host, 0, 0, width, height, True)
    inner = win32gui.GetWindow(host, win32con.GW_CHILD)
    if inner:
        win32gui.MoveWindow(inner, 0, 0, width, height, True)


def _size_control_to_window(hwnd, client) -> None:
    """Give the child session the frame's client resolution and stretch the control over the frame.

    A desktop larger than this client makes MsTscAx build a view of that size and present it
    outside the frame. The hosted control is stretched over the frame, so the frame does not stay blank.
    """
    _set_desktop_resolution(client, live=False)
    _fit_hosted_control(hwnd)


def _com_address(unknown) -> int:
    """The COM pointer pywin32 prints. This interface is not IDispatch, so Invoke cannot reach it."""
    text = repr(unknown)
    marker = "obj at "
    start = text.find(marker)
    if start < 0:
        raise OSError("the COM object did not expose its address")
    return int(text[start + len(marker) :].rstrip(">"), 16)


def _set_child_session(extended) -> None:
    """Set ConnectToChildSession on IMsRdpExtendedSettings.

    The type library gives this interface a five-slot vtable: the three IUnknown methods, then
    put_Property at slot 3 and get_Property at slot 4. Both take the name and a VARIANT pointer.
    Treating the pointer as IDispatch calls the wrong slot and the flag stays off.
    """
    import ctypes

    oleaut = ctypes.WinDLL("oleaut32", use_last_error=True)
    oleaut.SysAllocString.argtypes = (ctypes.c_wchar_p,)
    oleaut.SysAllocString.restype = ctypes.c_void_p
    oleaut.SysFreeString.argtypes = (ctypes.c_void_p,)

    class Variant(ctypes.Structure):
        _fields_ = [
            ("vt", ctypes.c_ushort),
            ("r1", ctypes.c_ushort),
            ("r2", ctypes.c_ushort),
            ("r3", ctypes.c_ushort),
            ("val", ctypes.c_longlong),
        ]

    address = _com_address(extended)
    table = ctypes.cast(ctypes.cast(address, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    call = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(Variant))
    name = oleaut.SysAllocString("ConnectToChildSession")
    try:
        value = Variant(vt=11, val=-1)  # VT_BOOL, VARIANT_TRUE
        wrote = call(table[3])(address, name, ctypes.byref(value))
        read = Variant()
        got = call(table[4])(address, name, ctypes.byref(read))
    finally:
        oleaut.SysFreeString(name)
    if wrote < 0 or got < 0 or read.vt != 11 or read.val & 0xFFFF != 0xFFFF:
        raise OSError(
            f"ConnectToChildSession was not set (put {wrote & 0xFFFFFFFF:#x}, get {got & 0xFFFFFFFF:#x}, "
            f"vt {read.vt}, value {read.val & 0xFFFF:#x})"
        )


def _set_extended_bool(extended, name: str, enabled: bool) -> None:
    """Set one write-only VT_BOOL property on IMsRdpExtendedSettings through the vtable put slot.

    `EnableHardwareMode` is the one that matters: True (its default) selects the DirectX presenter,
    whose surface covers the monitor and reads back black to a GDI screen grab. False forces the
    GDI presenter, so the picture stays inside this window. The property is write-only, so there is
    no get to confirm it; only the put HRESULT is checked. A VT_I4 here is the wrong type and the
    put is dropped, which is the bug that left hardware mode on.
    """
    import ctypes

    oleaut = ctypes.WinDLL("oleaut32", use_last_error=True)
    oleaut.SysAllocString.argtypes = (ctypes.c_wchar_p,)
    oleaut.SysAllocString.restype = ctypes.c_void_p
    oleaut.SysFreeString.argtypes = (ctypes.c_void_p,)

    class Variant(ctypes.Structure):
        _fields_ = [
            ("vt", ctypes.c_ushort),
            ("r1", ctypes.c_ushort),
            ("r2", ctypes.c_ushort),
            ("r3", ctypes.c_ushort),
            ("val", ctypes.c_longlong),
        ]

    address = _com_address(extended)
    table = ctypes.cast(ctypes.cast(address, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    call = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(Variant))
    label = oleaut.SysAllocString(name)
    try:
        value = Variant(vt=11, val=-1 if enabled else 0)  # VT_BOOL, VARIANT_TRUE / VARIANT_FALSE
        wrote = call(table[3])(address, label, ctypes.byref(value))
    finally:
        oleaut.SysFreeString(label)
    if wrote < 0:
        raise OSError(f"{name} was not set (put {wrote & 0xFFFFFFFF:#x})")


_rdp_events = None


def _reject_fullscreen(client) -> None:
    """Refuse the control's request to cover the monitor. The frame stays the small window."""
    global _rdp_events

    class Handler:
        def OnRequestGoFullScreen(self):
            with contextlib.suppress(Exception):
                client.FullScreen = False

        def OnRemoteDesktopSizeChange(self, width, height):
            with contextlib.suppress(Exception):
                client.FullScreen = False
            if desktop_oversized(width, height):
                _set_desktop_resolution(client, live=True)

    try:
        import win32com.client

        _rdp_events = win32com.client.WithEvents(client, Handler)
    except Exception:
        _rdp_events = None


def connect_child_session(client) -> None:
    """Attach `client` to the child session. No console-desktop fallback.

    `ConnectToChildSession` was set when the window was built. A configured password is stored
    for localhost before `Connect`, and the desktop is scaled into the small window.
    The control creates its view hidden, which leaves the frame blank, so a side thread shows
    those child windows while `Connect` runs.
    """
    if not _frame_hwnd:
        raise OSError("the picture-in-picture window is not open")
    _prepare_connection(client)
    logon = _session_logon()
    if logon is not None:
        _apply_nonscriptable_logon(client, logon[1])
    _reject_fullscreen(client)
    threading.Thread(target=_reveal_hosted_windows, args=(_frame_hwnd, client), name="jev-session-reveal", daemon=True).start()
    client.Connect()


def _prepare_connection(client) -> None:
    """Point the control at localhost, remember a configured password, and keep the desktop small."""
    client.Server = "localhost"
    client.FullScreen = False
    client.ColorDepth = 16
    # The child desktop stays at DESKTOP_WIDTH by DESKTOP_HEIGHT, which fits the frame's client.
    # Full-screen requests are refused below.
    _set_desktop_resolution(client, live=False)
    client.AdvancedSettings9.EnableCredSspSupport = True
    client.AdvancedSettings9.SmartSizing = True
    client.AdvancedSettings9.ContainerHandledFullScreen = True
    logon = _session_logon()
    if logon is None:
        return
    username, password = logon
    _remember_credentials(username, password)
    client.Domain = os.environ.get("USERDOMAIN") or os.environ.get("COMPUTERNAME") or "."
    client.UserName = username
    # The scriptable setting is not the one the control logs on with. The nonscriptable
    # interface takes the password and is told not to show the credential dialog.
    client.AdvancedSettings9.ClearTextPassword = password


def _session_logon() -> tuple[str, str] | None:
    """The configured child-session logon, or None when no password is set."""
    from pathlib import Path

    from . import config

    options = config.settings()
    if not options.session_password:
        config.load_dotenv(Path.cwd() / ".env")
        options = config.settings()
    password = options.session_password
    if not password:
        return None
    username = options.session_username or os.environ.get("USERNAME") or "Administrator"
    return username, password


def _query_interface(unknown, iid: str) -> int:
    """A raw COM pointer for `iid`. pywin32 has no wrapper for the nonscriptable RDP interfaces."""
    import ctypes
    import uuid
    from ctypes import wintypes

    class Guid(ctypes.Structure):
        _fields_ = [
            ("data1", wintypes.DWORD),
            ("data2", wintypes.WORD),
            ("data3", wintypes.WORD),
            ("data4", wintypes.BYTE * 8),
        ]

    parsed = uuid.UUID(iid)
    raw = parsed.bytes_le
    guid = Guid(int.from_bytes(raw[0:4], "little"), int.from_bytes(raw[4:6], "little"), int.from_bytes(raw[6:8], "little"))
    guid.data4[:] = raw[8:]
    address = _com_address(unknown)
    table = ctypes.cast(ctypes.cast(address, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    query = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(Guid), ctypes.POINTER(ctypes.c_void_p))
    found = ctypes.c_void_p()
    result = query(table[0])(address, ctypes.byref(guid), ctypes.byref(found))
    if result < 0 or not found.value:
        raise OSError(f"QueryInterface {iid} failed ({result & 0xFFFFFFFF:#x})")
    return int(found.value)


def _apply_nonscriptable_logon(client, password: str) -> None:
    """Give the control the password and forbid its credential dialog.

    `ClearTextPassword` and `AllowPromptingForCredentials` live on the nonscriptable vtable, not
    on the dispatch used for `Connect`. A child session still shows the dialog when prompting is
    left at its default.
    """
    import ctypes

    oleaut = ctypes.WinDLL("oleaut32", use_last_error=True)
    oleaut.SysAllocString.argtypes = (ctypes.c_wchar_p,)
    oleaut.SysAllocString.restype = ctypes.c_void_p
    oleaut.SysFreeString.argtypes = (ctypes.c_void_p,)
    address = _query_interface(client._oleobj_, "{4f6996d5-d7b1-412c-b0ff-063718566907}")
    table = ctypes.cast(ctypes.cast(address, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    put_text = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)
    put_bool = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_short)
    get_bool = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_short))
    blob = oleaut.SysAllocString(password)
    try:
        wrote = put_text(table[3])(address, blob)  # IMsTscNonScriptable::put_ClearTextPassword
    finally:
        oleaut.SysFreeString(blob)
    hid_prompt = put_bool(table[19])(address, 0)  # IMsRdpClientNonScriptable3::put_PromptForCredentials
    hid_allow = put_bool(table[63])(address, 0)  # IMsRdpClientNonScriptable5::put_AllowPromptingForCredentials
    shown = ctypes.c_short(-1)
    read = get_bool(table[64])(address, ctypes.byref(shown))
    if wrote < 0 or hid_prompt < 0 or hid_allow < 0 or read < 0 or shown.value != 0:
        raise OSError(
            f"the credential prompt was not disabled (password {wrote & 0xFFFFFFFF:#x}, "
            f"prompt {hid_prompt & 0xFFFFFFFF:#x}, allow {hid_allow & 0xFFFFFFFF:#x}, "
            f"read {read & 0xFFFFFFFF:#x}, value {shown.value})"
        )


def _remember_credentials(username: str, password: str) -> None:
    """Store the logon for localhost the way Remote Desktop's 'remember me' does.

    Credential Manager keeps it under TERMSRV/localhost, encrypted for this user, so later
    runs reuse it. The password is not written anywhere else.
    """
    import ctypes
    from ctypes import wintypes

    class FileTime(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class Credential(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", FileTime),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.c_void_p),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    blob = password.encode("utf-16-le")
    buffer = ctypes.create_string_buffer(blob)
    cred = Credential()
    cred.Type = 2  # CRED_TYPE_DOMAIN_PASSWORD
    cred.TargetName = "TERMSRV/localhost"
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.addressof(buffer)
    cred.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = username
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.CredWriteW.argtypes = (ctypes.POINTER(Credential), wintypes.DWORD)
    advapi.CredWriteW.restype = wintypes.BOOL
    if not advapi.CredWriteW(ctypes.byref(cred), 0):
        raise OSError(ctypes.get_last_error(), "CredWriteW failed for TERMSRV/localhost")


def wait_for_child_explorer(pump: Callable[[], None], deadline: float, client) -> None:
    """Pump messages until the child session has an explorer, or fail with the reason.

    The window stays alive only while its thread pumps. A credential prompt, if one appears, is
    the user's to answer; this never types a password.
    """
    started = time.monotonic()
    limit = max(0.0, deadline - started)
    seen_connected = False
    while True:
        pump()
        elapsed = time.monotonic() - started
        connected = _control_connected(client)
        seen_connected = seen_connected or connected
        found = _child_explorer_present()
        failure = child_wait_failure(elapsed, connected, found, limit, seen_connected)
        if found:
            return
        if failure:
            if "disconnected" in failure:
                failure = f"{failure} ({_disconnect_detail(client)})"
            raise OSError(failure)
        time.sleep(0.2)


def _disconnect_detail(client) -> str:
    """The control's own reason for dropping the connection, when it will say."""
    try:
        extended = int(client.ExtendedDisconnectReason)
    except Exception as error:
        return f"no disconnect reason ({error})"
    try:
        text = client.GetErrorDescription(0, extended)
    except Exception as error:
        text = str(error)
    return f"extended disconnect reason {extended}: {text}"


def _control_connected(client) -> bool:
    """Whether the control reports a connection. A missing property is not treated as a drop."""
    try:
        return int(client.Connected) != 0
    except Exception:
        return False


def _child_explorer_present() -> bool:
    """Whether the child session exists and its explorer.exe is running."""
    import ctypes

    wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
    if _child_session_id(wts) is None:
        return False
    try:
        _explorer_pid(wts, _console_session_id())
    except OSError:
        return False
    return True


def _pump_messages() -> None:
    import pythoncom

    pythoncom.PumpWaitingMessages()


def _window_open(hwnd) -> bool:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.IsWindow.argtypes = (wintypes.HWND,)
    user32.IsWindow.restype = wintypes.BOOL
    return bool(user32.IsWindow(hwnd))


def _host_until(relay: threading.Thread, hwnd, pipe, pid) -> str:
    """Pump until the relay ends, the window closes, or the child process exits. Returns which."""
    while True:
        _pump_messages()
        why = host_stop(not relay.is_alive(), _window_open(hwnd), _child_alive(pid))
        if why == "closed":
            _notify_closed(pipe)
            relay.join(timeout=5)
            return why
        if why == "exited":
            relay.join(timeout=2)
            if relay.is_alive():
                _notify_closed(pipe)
                relay.join(timeout=2)
            return why
        if why == "done":
            return why
        time.sleep(0.05)


def _close_window(hwnd, client) -> None:
    """Disconnect the control and destroy the window. Failures here must not hide the real one."""
    if client is not None:
        with contextlib.suppress(Exception):
            client.Disconnect()
    if not hwnd:
        return
    with contextlib.suppress(Exception):
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.DestroyWindow.argtypes = (wintypes.HWND,)
        user32.DestroyWindow.restype = wintypes.BOOL
        user32.DestroyWindow(hwnd)


def _notify_closed(pipe, write: Callable[..., None] | None = None) -> None:
    """Tell the child the window went away, so its run takes the abort path."""
    send = write or _write_line
    with contextlib.suppress(Exception):
        send(pipe, encode("cancel"))


def _child_alive(pid: int | None) -> bool:
    """Whether the launched process is still running. No pid means nothing to watch."""
    if not pid:
        return True
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    code = wintypes.DWORD()
    try:
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel.CloseHandle(handle)


def create_session_pipe(name: str):
    """A duplex message pipe that only the current user can open, rejecting remote clients."""
    import ntsecuritycon
    import win32api
    import win32pipe
    import win32security

    user = win32security.LookupAccountName(None, win32api.GetUserName())[0]
    descriptor = win32security.SECURITY_DESCRIPTOR()
    descriptor.Initialize()
    access = win32security.ACL()
    access.AddAccessAllowedAce(
        win32security.ACL_REVISION, ntsecuritycon.FILE_GENERIC_READ | ntsecuritycon.FILE_GENERIC_WRITE, user
    )
    descriptor.SetSecurityDescriptorDacl(1, access, 0)
    attributes = win32security.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = descriptor
    return win32pipe.CreateNamedPipe(
        rf"\\.\pipe\{name}",
        win32pipe.PIPE_ACCESS_DUPLEX,
        win32pipe.PIPE_TYPE_MESSAGE
        | win32pipe.PIPE_READMODE_MESSAGE
        | win32pipe.PIPE_WAIT
        | win32pipe.PIPE_REJECT_REMOTE_CLIENTS,
        1,
        65536,
        65536,
        0,
        attributes,
    )


# Handles opened so a pending read does not block a write on the same pipe. A synchronous
# duplex handle deadlocks when the reader thread is inside ReadFile and the main thread writes.
_OVERLAPPED: set[int] = set()


def _pipe_is_overlapped(pipe) -> bool:
    try:
        return int(pipe) in _OVERLAPPED
    except (TypeError, ValueError):
        return False


def attach_session_pipe(name: str):
    """The child's end of the pipe the controller created.

    Overlapped, so the thread that waits for cancel can read while the run writes a log line.
    """
    import win32file

    handle = win32file.CreateFile(
        rf"\\.\pipe\{name}",
        win32file.GENERIC_READ | win32file.GENERIC_WRITE,
        0,
        None,
        win32file.OPEN_EXISTING,
        win32file.FILE_FLAG_OVERLAPPED,
        None,
    )
    _OVERLAPPED.add(int(handle))
    return handle


def attach_input_desktop() -> None:
    """Bind this process to the session's interactive desktop, WinSta0\\Default.

    The task scheduler starts jev in the child session on a desktop that is not receiving input,
    so GetCursorPos and SendInput come back access-denied while a screen capture still works.
    The thread must not have created a window yet, which is why the launcher calls this before
    importing the rest of jev. The handles stay open: closing them detaches the thread again.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    access = 0x02000000  # MAXIMUM_ALLOWED
    user32.OpenWindowStationW.argtypes = (wintypes.LPCWSTR, wintypes.BOOL, wintypes.DWORD)
    user32.OpenWindowStationW.restype = wintypes.HANDLE
    user32.SetProcessWindowStation.argtypes = (wintypes.HANDLE,)
    user32.SetProcessWindowStation.restype = wintypes.BOOL
    user32.OpenInputDesktop.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    user32.OpenInputDesktop.restype = wintypes.HANDLE
    user32.OpenDesktopW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    user32.OpenDesktopW.restype = wintypes.HANDLE
    user32.SetThreadDesktop.argtypes = (wintypes.HANDLE,)
    user32.SetThreadDesktop.restype = wintypes.BOOL

    import time

    station = user32.OpenWindowStationW("WinSta0", False, access)
    if not station:
        raise OSError(ctypes.get_last_error(), "OpenWindowStationW failed")
    if not user32.SetProcessWindowStation(station):
        raise OSError(ctypes.get_last_error(), "SetProcessWindowStation failed")
    # The child session has no input desktop until its remote-desktop view is in place, so
    # OpenInputDesktop is polled. Binding Default before input is active leaves GetCursorPos
    # access-denied. Default is the last resort if input never becomes active.
    deadline = time.monotonic() + CHILD_LOGON_SECONDS
    input_desktop = 0
    while time.monotonic() < deadline:
        input_desktop = user32.OpenInputDesktop(0, False, access)
        if input_desktop:
            break
        time.sleep(0.5)
    desktop = input_desktop or user32.OpenDesktopW("Default", 0, False, access)
    if not desktop:
        raise OSError(ctypes.get_last_error(), "the input desktop could not be opened")
    if not user32.SetThreadDesktop(desktop):
        raise OSError(ctypes.get_last_error(), "SetThreadDesktop failed")
    _record_desktop_attachment(user32, bool(input_desktop))


def _record_desktop_attachment(user32, used_input_desktop: bool) -> None:
    """Write what the binding actually did, so a failed run says which desktop jev landed on."""
    import ctypes
    import tempfile
    from ctypes import wintypes

    with contextlib.suppress(Exception):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        session = wintypes.DWORD()
        kernel.ProcessIdToSessionId(kernel.GetCurrentProcessId(), ctypes.byref(session))

        def _name(handle) -> str:
            buffer = ctypes.create_unicode_buffer(256)
            need = wintypes.DWORD()
            user32.GetUserObjectInformationW(handle, 2, buffer, 512, ctypes.byref(need))  # UOI_NAME
            return buffer.value

        user32.GetUserObjectInformationW.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        )
        user32.GetProcessWindowStation.restype = wintypes.HANDLE
        user32.GetThreadDesktop.restype = wintypes.HANDLE
        user32.GetThreadDesktop.argtypes = (wintypes.DWORD,)
        station = _name(user32.GetProcessWindowStation())
        desktop = _name(user32.GetThreadDesktop(kernel.GetCurrentThreadId()))

        class Point(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        point = Point()
        probe = "ok" if user32.GetCursorPos(ctypes.byref(point)) else f"denied {ctypes.get_last_error()}"
        line = (
            f"session={session.value} winsta={station} desktop={desktop} "
            f"input_desktop={used_input_desktop} cursor_probe={probe}\n"
        )
        path = os.path.join(tempfile.gettempdir(), "jev-session-desktop.log")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(line)


NORMAL_PRIORITY_CLASS = 0x20
PROCESS_POWER_THROTTLING = 4  # the PROCESS_INFORMATION_CLASS value of PROCESS_POWER_THROTTLING_STATE
POWER_THROTTLING_EXECUTION_SPEED = 0x1
TASK_PRIORITY_NORMAL = 4  # task scheduler priority: 4 to 6 are normal, and 4 keeps normal I/O priority


def run_at_full_speed() -> None:
    """Run at normal priority, and opt out of the power throttling (EcoQoS) Windows gives background work.

    Throttled, the models run two to three times slower: measured on one capture, detection 0.57 s
    against 1.12 s, and OCR 1.36 s against 4.74 s. jev in a child session starts from a scheduled
    task and has no window. A failed call leaves the process as it was.
    """
    import ctypes
    from ctypes import wintypes

    class Throttling(ctypes.Structure):
        _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG), ("StateMask", wintypes.ULONG)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.SetPriorityClass.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.SetProcessInformation.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    process = kernel.GetCurrentProcess()
    kernel.SetPriorityClass(process, NORMAL_PRIORITY_CLASS)
    # The bit in ControlMask takes execution speed out of Windows' hands; clear in StateMask, it stays unthrottled.
    state = Throttling(1, POWER_THROTTLING_EXECUTION_SPEED, 0)
    kernel.SetProcessInformation(process, PROCESS_POWER_THROTTLING, ctypes.byref(state), ctypes.sizeof(state))


_LAUNCHER = """\
import os
import sys
import time
import traceback

born = time.perf_counter()
stamps = {"launcher": 0.0}

def stamp(name):
    stamps[name] = round(time.perf_counter() - born, 3)

pid_path = sys.argv[3]
try:
    os.environ["JEV_SESSION"] = "1"
    os.environ["JEV_SESSION_PIPE"] = sys.argv[1]
    os.chdir(sys.argv[2])
    sys.path.insert(0, sys.argv[2])
    from yuanji_computer_use.windows_session import attach_input_desktop, run_at_full_speed

    attach_input_desktop()
    stamp("desktop")
    run_at_full_speed()
    stamp("full_speed")
    # Importing perception before the bind fails SetThreadDesktop: the import opens the desktop
    # the thread is already on, and a thread that has opened one cannot switch.
    from yuanji_computer_use.perception import adopt_start, mark, snapshot_session_cpu, start_warm_up

    adopt_start(born, stamps)
    snapshot_session_cpu("at_start")
    start_warm_up()
    with open(pid_path, "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))
    sys.argv = [sys.argv[0], "--act"]
    from yuanji_computer_use.cli import main

    mark("cli_imported")
    main()
except BaseException:
    with open(pid_path + ".log", "w", encoding="utf-8") as handle:
        traceback.print_exc(file=handle)
    raise
"""


def launch_in_session(pipe_name: str) -> int:
    """Start jev in the child session and return its process id.

    `CreateProcessWithTokenW` stays in the caller's session, and `CreateProcessAsUser` into a
    child session is access-denied. The task scheduler can run this user's interactive token
    in a session id. The task command carries the pipe name and the repo path, not secrets;
    jev reads `.env` after it changes to that path. A failure raises. This never starts jev
    in the console session instead.
    """
    import ctypes
    import tempfile

    import win32com.client

    session = _child_session_id(ctypes.WinDLL("wtsapi32", use_last_error=True))
    if session is None:
        raise OSError("the child session is not running")
    directory = tempfile.gettempdir()
    script = os.path.join(directory, "jev-session-launch.py")
    pidfile = os.path.join(directory, "jev-session-pid.txt")
    logfile = pidfile + ".log"
    for path in (pidfile, logfile):
        with contextlib.suppress(OSError):
            os.remove(path)
    with open(script, "w", encoding="utf-8") as handle:
        handle.write(_LAUNCHER)
    scheduler = win32com.client.Dispatch("Schedule.Service")
    scheduler.Connect()
    folder = scheduler.GetFolder("\\")
    _delete_launch_task()
    definition = scheduler.NewTask(0)
    definition.Principal.UserId = os.environ.get("USERNAME", "")
    definition.Principal.LogonType = 3  # TASK_LOGON_INTERACTIVE_TOKEN, so no password is stored
    definition.Principal.RunLevel = 1
    definition.Settings.Priority = TASK_PRIORITY_NORMAL  # a new task runs at 7, below normal
    action = definition.Actions.Create(0)
    # python.exe opens a console on the child desktop and that window takes the focus. pythonw does not.
    windowless = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    action.Path = windowless if os.path.exists(windowless) else sys.executable
    action.Arguments = f'"{script}" {pipe_name} "{os.getcwd()}" "{pidfile}"'
    action.WorkingDirectory = os.environ.get("USERPROFILE") or directory
    folder.RegisterTaskDefinition(TASK_NAME, definition, 6, "", "", 3)  # TASK_CREATE_OR_UPDATE
    folder.GetTask(TASK_NAME).RunEx(None, 4, int(session), "")  # TASK_RUN_USE_SESSION_ID
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        _pump_messages()
        try:
            with open(pidfile, encoding="utf-8") as handle:
                text = handle.read().strip()
        except OSError:
            text = ""
        if text.isdigit():
            return int(text)
        time.sleep(0.2)
    detail = ""
    with contextlib.suppress(OSError), open(logfile, encoding="utf-8") as handle:
        detail = handle.read().strip()
    raise OSError(detail or "jev did not start in the child session")


def _delete_launch_task() -> None:
    """Remove the one-shot task. Deleting a running task stops it, so this waits until the run is over."""
    with contextlib.suppress(Exception):
        import win32com.client

        scheduler = win32com.client.Dispatch("Schedule.Service")
        scheduler.Connect()
        scheduler.GetFolder("\\").DeleteTask(TASK_NAME, 0)


def _explorer_pid(wts, console: int) -> int:
    """The explorer.exe that belongs to the child session, not the console session."""
    import ctypes
    from ctypes import wintypes

    class Process(ctypes.Structure):
        _fields_ = [
            ("SessionId", wintypes.DWORD),
            ("ProcessId", wintypes.DWORD),
            ("pProcessName", wintypes.LPWSTR),
            ("pUserSid", ctypes.c_void_p),
        ]

    listed = ctypes.POINTER(Process)()
    count = wintypes.DWORD()
    if not wts.WTSEnumerateProcessesW(0, 0, 1, ctypes.byref(listed), ctypes.byref(count)):
        raise OSError(ctypes.get_last_error(), "WTSEnumerateProcesses failed")
    try:
        for entry in listed[: count.value]:
            name = (entry.pProcessName or "").lower()
            if name == "explorer.exe" and entry.SessionId != console:
                return int(entry.ProcessId)
    finally:
        wts.WTSFreeMemory(listed)
    raise OSError("the child session has no explorer.exe")


def _edition() -> str:
    import winreg

    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as key:
        edition, _ = winreg.QueryValueEx(key, "EditionID")
    return str(edition)


def _rdp_localhost() -> bool:
    """Whether anything accepts Remote Desktop on this machine. A closed port cannot host the window."""
    import socket

    try:
        with socket.create_connection(("127.0.0.1", 3389), timeout=1):
            return True
    except OSError:
        return False


def _smart_card_logon() -> bool:
    """Whether this logon used a smart card. Asking and failing counts as not a smart card."""
    try:
        import ctypes
        from ctypes import wintypes

        secur32 = ctypes.WinDLL("secur32", use_last_error=True)
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)

        class Luid(ctypes.Structure):
            _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

        class Stats(ctypes.Structure):
            _fields_ = [
                ("TokenId", Luid),
                ("AuthenticationId", Luid),
                ("ExpirationTime", ctypes.c_int64),
                ("TokenType", wintypes.DWORD),
                ("ImpersonationLevel", wintypes.DWORD),
                ("DynamicCharged", wintypes.DWORD),
                ("DynamicAvailable", wintypes.DWORD),
                ("GroupCount", wintypes.DWORD),
                ("PrivilegeCount", wintypes.DWORD),
                ("ModifiedId", Luid),
            ]

        token = wintypes.HANDLE()
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            return False
        stats = Stats()
        returned = wintypes.DWORD()
        if not advapi.GetTokenInformation(token, 10, ctypes.byref(stats), ctypes.sizeof(stats), ctypes.byref(returned)):
            return False

        class Data(ctypes.Structure):
            _fields_ = [
                ("Size", wintypes.ULONG),
                ("LogonId", Luid),
                ("UserName", wintypes.LPWSTR),
                ("LogonDomain", wintypes.LPWSTR),
                ("AuthenticationPackage", wintypes.LPWSTR),
                ("LogonType", wintypes.ULONG),
                ("Session", wintypes.ULONG),
                ("Sid", ctypes.c_void_p),
                ("LogonTime", ctypes.c_int64),
                ("LogonServer", wintypes.LPWSTR),
                ("DnsDomainName", wintypes.LPWSTR),
                ("Upn", wintypes.LPWSTR),
            ]

        found = ctypes.POINTER(Data)()
        if secur32.LsaGetLogonSessionData(ctypes.byref(stats.AuthenticationId), ctypes.byref(found)) != 0:
            return False
        package = (found.contents.AuthenticationPackage or "").lower()
        return "smartcard" in package or package == "cloudap"
    except Exception:
        return False


def _console_session_id() -> int:
    import ctypes

    return int(ctypes.WinDLL("kernel32", use_last_error=True).WTSGetActiveConsoleSessionId())


def _child_session_id(wts) -> int | None:
    """The child session, if this logon has one. The only argument is the out id."""
    import ctypes
    from ctypes import wintypes

    wts.WTSGetChildSessionId.argtypes = (ctypes.POINTER(wintypes.ULONG),)
    wts.WTSGetChildSessionId.restype = wintypes.BOOL
    session = wintypes.ULONG()
    if not wts.WTSGetChildSessionId(ctypes.byref(session)):
        return None
    return int(session.value)


def _child_running() -> bool:
    import ctypes

    return _child_session_id(ctypes.WinDLL("wtsapi32", use_last_error=True)) is not None


def _connect_pipe(pipe) -> None:
    """Wait until the child opens the pipe. A child that already has is a success, not an error."""
    import pywintypes
    import win32pipe

    try:
        win32pipe.ConnectNamedPipe(pipe, None)
    except pywintypes.error as error:
        if error.winerror != ERROR_PIPE_CONNECTED:
            raise


def _relay(pipe, goal: str, connect=None, write=None, read=None, ask=None) -> None:
    """Connect the pipe, send the task, then print whatever the child sends until it is done."""
    connect = connect or _connect_pipe
    write = write or _write_line
    read = read or _read_line
    ask = ask or input
    connect(pipe)
    write(pipe, encode("task", goal=goal))
    while True:
        message = decode(read(pipe))
        kind = message["type"]
        if kind == "log":
            text = str(message.get("text") or "")
            print(text, flush=True)
            _note_mask_action(text)
        elif kind == "progress":
            print(f"  {message.get('taken')} / {message.get('budget')}", flush=True)
        elif kind == "question":
            print(message.get("text", ""), flush=True)
            try:
                reply = ask("  > ")
            except EOFError:
                reply = ""
            write(pipe, encode("answer", text=reply))
        elif kind == "done":
            print(message.get("text", ""), flush=True)
            return


def _write_line(pipe, line: str) -> None:
    data = line.encode("utf-8")
    if _pipe_is_overlapped(pipe):
        _overlapped_write(pipe, data)
        return
    _blocking_write(pipe, data)


def _blocking_write(pipe, data: bytes) -> None:
    import win32file

    win32file.WriteFile(pipe, data)


def _read_line(pipe) -> str:
    if _pipe_is_overlapped(pipe):
        return _overlapped_read(pipe).decode("utf-8")
    import win32file

    _, data = win32file.ReadFile(pipe, 65536)
    return data.decode("utf-8")


def _overlapped():
    """An event the read or write completes on. Each call gets its own, so the two directions do not share one."""
    import pywintypes
    import win32event

    ov = pywintypes.OVERLAPPED()
    ov.hEvent = win32event.CreateEvent(None, True, False, None)
    return ov


ERROR_IO_PENDING = 997  # win32file does not export this name; the overlapped call returns it


def _complete(pipe, ov, code: int) -> int:
    import win32event
    import win32file

    try:
        if code == ERROR_IO_PENDING:
            win32event.WaitForSingleObject(ov.hEvent, win32event.INFINITE)
        return int(win32file.GetOverlappedResult(pipe, ov, True))
    finally:
        win32file.CloseHandle(ov.hEvent)


def _overlapped_write(pipe, data: bytes) -> None:
    import pywintypes
    import win32file

    ov = _overlapped()
    try:
        code, _written = win32file.WriteFile(pipe, data, ov)
    except pywintypes.error as error:
        if error.winerror != ERROR_IO_PENDING:
            win32file.CloseHandle(ov.hEvent)
            raise
        code = error.winerror
    _complete(pipe, ov, code)


def _overlapped_read(pipe) -> bytes:
    import pywintypes
    import win32file

    ov = _overlapped()
    buf = win32file.AllocateReadBuffer(65536)
    try:
        code, _data = win32file.ReadFile(pipe, buf, ov)
    except pywintypes.error as error:
        if error.winerror != ERROR_IO_PENDING:
            win32file.CloseHandle(ov.hEvent)
            raise
        code = error.winerror
    size = _complete(pipe, ov, code)
    # GetOverlappedResult is the number of bytes in this message. The buffer is larger, and the
    # bytes ReadFile hands back can be that whole buffer with zeros after the message.
    return bytes(buf[:size]).split(b"\x00", 1)[0]
