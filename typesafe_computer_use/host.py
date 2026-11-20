"""Open the application the goal needs, then let the existing loop drive it.

The host only chooses a window that is already open or starts the program. It never clicks or
types. The classifier and the driver are what work inside the application afterwards.
"""

from __future__ import annotations

import re
import time

from .platform_adapter import desktop

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
    from .windows_session import report_action

    report_action(line)


def _open_window(name: str) -> bool:
    """Whether a visible titled window belongs to `name`. The foreground process name is not that."""
    return any(_same_app(name, process) for _, _, process in desktop.list_windows())


def _came_forward(name: str) -> bool:
    """Bring `name`'s window to the front once it is open. A matching process name is not success."""
    deadline = time.monotonic() + LAUNCH_WAIT
    while True:
        desktop.check_abort()
        if _open_window(name) or time.monotonic() >= deadline:
            return bool(desktop.activate(name))
        desktop.sleep_watching(0.2)


def prepare(cfg, ctx, state, log) -> None:
    """Put the goal's application in front. A failure is recorded, and the existing loop still runs."""
    if not cfg.act or cfg.replay or ctx.writer is None:
        return
    try:
        windows = desktop.list_windows()
        front, _pid = desktop.frontmost_app_and_pid()
    except Exception as error:
        _say(state, log, f"host: could not see the desktop ({error})", remember=True)
        return
    from .writer import WriterError, compose_host

    try:
        choice = compose_host(ctx.writer, cfg.goal, windows, front)
    except WriterError as error:
        _say(state, log, f"host: could not choose an app ({error})", remember=True)
        return
    try:
        _open(choice, windows, front, state, log)
    except Exception as error:
        _say(state, log, f"host: could not open the app ({error})", remember=True)


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
        desktop.launch_app(name)
        if _came_forward(name):
            _say(state, log, f"host: launched {name}", remember=True)
        else:
            _say(state, log, f"host: {name} did not come forward", remember=True)
        return
    _say(state, log, "host: the reply was not an action", remember=True)
