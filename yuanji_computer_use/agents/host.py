"""宿主：把目标应用打开或调到前台。它不点击、不打字，之后的操作交给分类器和接管。"""

from __future__ import annotations

import re
import time

from ..platforms.adapter import desktop
from .base import register
from .status import Status

_EXECUTABLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
LAUNCH_WAIT = 5.0  # how long a just-started program gets to reach the front


def executable_name(name: str) -> str | None:
    """A program name with no path and no arguments, or None when it is anything else."""
    text = name.strip()
    if _EXECUTABLE.fullmatch(text) is None:
        return None
    return text


def _same_app(wanted: str, process: str) -> bool:
    def stem(value: str) -> str:
        return value.strip().lower().removesuffix(".exe")

    return bool(stem(wanted)) and stem(wanted) == stem(process)


def _say(state, log, line: str, *, remember: bool) -> None:
    """Write the line to the run log and the mask. `remember` also keeps it for the later agents."""
    log(line)
    if remember:
        state.history.append(line)
    from ..platforms.windows_session import report_action

    report_action(line)


def _open_window(name: str) -> bool:
    """Whether a visible titled window belongs to `name`. The foreground process name is not that."""
    return any(_same_app(name, process) for _, _, process in desktop.list_windows())


# 启动名和前台进程经常不是同一个。设置没有 SystemSettings.exe，控制面板寄居在资源管理器里。
_PROCESS = {"ms-settings:": ("applicationframehost", "systemsettings")}
_TITLE = {"control": "control panel"}


def _present(name: str) -> bool:
    """这个启动名对应的窗口是否已经在列表里。按进程，控制面板再按标题。"""
    if _open_window(name) or any(_open_window(alias) for alias in _PROCESS.get(name, ())):
        return True
    needle = _TITLE.get(name)
    return needle is not None and any(needle in title.lower() for _, title, _ in desktop.list_windows())


def _bring(name: str) -> bool:
    """把已经出现的窗口拉到前面。启动名对不上进程时，用实际的进程再试一次。"""
    if desktop.activate(name):
        return True
    for alias in _PROCESS.get(name, ()):
        if _open_window(alias) and desktop.activate(alias):
            return True
    needle = _TITLE.get(name)
    if needle is None:
        return False
    return any(needle in title.lower() and desktop.activate(process) for _, title, process in desktop.list_windows())


def _came_forward(name: str) -> bool:
    """Bring `name`'s window to the front once it is open. A matching process name is not success."""
    deadline = time.monotonic() + LAUNCH_WAIT
    while True:
        desktop.check_abort()
        if _present(name) or time.monotonic() >= deadline:
            return _bring(name)
        desktop.sleep_watching(0.2)


def _open(choice: dict, windows: list[tuple[str, str, str]], front: str, state, log) -> None:
    action = str(choice.get("action") or "").strip().lower()
    if action == "ready":
        _say(state, log, f"host: {front or 'the frontmost app'} is already the app", remember=False)
        return
    if action == "focus":
        ident = str(choice.get("id") or "")
        window = next((item for item in windows if item[0] == ident), None)
        if window is None:
            _say(state, log, f"host: no window {ident}", remember=True)
            return
        process = window[2]
        if desktop.activate(process):
            _say(state, log, f"host: focused {process}", remember=True)
        else:
            _say(state, log, f"host: {process} did not come forward", remember=True)
        return
    if action == "launch":
        raw = str(choice.get("executable") or "")
        name = executable_name(raw)
        if name is None:
            _say(state, log, f"host: refused {raw!r}", remember=True)
            return
        # Windows has no SystemSettings.exe. The settings app opens from this URI.
        if name.lower().removesuffix(".exe") == "systemsettings":
            name = "ms-settings:"
        desktop.launch_app(name)
        if _came_forward(name):
            _say(state, log, f"host: launched {name}", remember=True)
        else:
            _say(state, log, f"host: {name} did not come forward", remember=True)
        return
    _say(state, log, "host: the reply was not an action", remember=True)


@register("host")
class HostAgent:
    def handle(self, session) -> Status:
        """Put the goal's application in front. A failure is recorded, and the loop still runs."""
        cfg, ctx, log = session.cfg, session.ctx, session.log
        if not cfg.act or cfg.replay or ctx.writer is None:
            return Status.POLICY
        try:
            windows = desktop.list_windows()
            front, _pid = desktop.frontmost_app_and_pid()
        except Exception as error:
            _say(session, log, f"host: could not see the desktop ({error})", remember=True)
            return Status.POLICY
        from ..llm.writer import WriterError, compose_host

        try:
            choice = compose_host(ctx.writer, cfg.goal, windows, front)
        except WriterError as error:
            _say(session, log, f"host: could not choose an app ({error})", remember=True)
            return Status.POLICY
        try:
            _open(choice, windows, front, session, log)
        except Exception as error:
            _say(session, log, f"host: could not open the app ({error})", remember=True)
        return Status.POLICY
