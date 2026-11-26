"""宿主：把目标应用打开或调到前台。它不点击、不打字，之后的操作交给分类器和接管。"""

from __future__ import annotations

import re
import time
from typing import ClassVar

from ..platforms.adapter import desktop
from .base import BaseAgent, register
from .status import Status

_EXECUTABLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
LAUNCH_WAIT = 5.0  # how long a just-started program gets to reach the front


@register("host")
class HostAgent(BaseAgent):
    # 启动名和前台进程经常不是同一个。设置没有 SystemSettings.exe，控制面板寄居在资源管理器里。
    _PROCESS: ClassVar[dict[str, tuple[str, ...]]] = {"ms-settings:": ("applicationframehost", "systemsettings")}
    _TITLE: ClassVar[dict[str, str]] = {"control": "control panel"}

    def handle(self, session) -> Status:
        """Put the goal's application in front. A failure is recorded, and the loop still runs."""
        self.bind(session)
        if not self.cfg.act or self.cfg.replay or self.ctx.writer is None:
            return Status.POLICY
        try:
            windows = desktop.list_windows()
            front, _pid = desktop.frontmost_app_and_pid()
        except Exception as error:
            self.say(f"host: could not see the desktop ({error})")
            return Status.POLICY
        from ..llm.writer import WriterError, compose_host

        try:
            choice = compose_host(self.ctx.writer, self.cfg.goal, windows, front)
        except WriterError as error:
            self.say(f"host: could not choose an app ({error})")
            return Status.POLICY
        try:
            self._open(choice, windows, front)
        except Exception as error:
            self.say(f"host: could not open the app ({error})")
        return Status.POLICY

    @staticmethod
    def executable_name(name: str) -> str | None:
        """A program name with no path and no arguments, or None when it is anything else."""
        text = name.strip()
        if _EXECUTABLE.fullmatch(text) is None:
            return None
        return text

    @staticmethod
    def _same_app(wanted: str, process: str) -> bool:
        def stem(value: str) -> str:
            return value.strip().lower().removesuffix(".exe")

        return bool(stem(wanted)) and stem(wanted) == stem(process)

    def _open_window(self, name: str) -> bool:
        """Whether a visible titled window belongs to `name`. The foreground process name is not that."""
        return any(self._same_app(name, process) for _, _, process in desktop.list_windows())

    def _present(self, name: str) -> bool:
        """这个启动名对应的窗口是否已经在列表里。按进程，控制面板再按标题。"""
        if self._open_window(name) or any(self._open_window(alias) for alias in self._PROCESS.get(name, ())):
            return True
        needle = self._TITLE.get(name)
        return needle is not None and any(needle in title.lower() for _, title, _ in desktop.list_windows())

    def _bring(self, name: str) -> bool:
        """把已经出现的窗口拉到前面。启动名对不上进程时，用实际的进程再试一次。"""
        if desktop.activate(name):
            return True
        for alias in self._PROCESS.get(name, ()):
            if self._open_window(alias) and desktop.activate(alias):
                return True
        needle = self._TITLE.get(name)
        if needle is None:
            return False
        return any(needle in title.lower() and desktop.activate(process) for _, title, process in desktop.list_windows())

    def _came_forward(self, name: str) -> bool:
        """Bring `name`'s window to the front once it is open. A matching process name is not success."""
        deadline = time.monotonic() + LAUNCH_WAIT
        while True:
            desktop.check_abort()
            if self._present(name) or time.monotonic() >= deadline:
                return self._bring(name)
            desktop.sleep_watching(0.2)

    def _open(self, choice: dict, windows: list[tuple[str, str, str]], front: str) -> None:
        action = str(choice.get("action") or "").strip().lower()
        if action == "ready":
            self._ready(front)
        elif action == "focus":
            self._focus(choice, windows)
        elif action == "launch":
            self._launch(choice)
        else:
            self.say("host: the reply was not an action")

    def _ready(self, front: str) -> None:
        self.say(f"host: {front or 'the frontmost app'} is already the app", remember=False)

    def _focus(self, choice: dict, windows: list[tuple[str, str, str]]) -> None:
        ident = str(choice.get("id") or "")
        window = next((item for item in windows if item[0] == ident), None)
        if window is None:
            self.say(f"host: no window {ident}")
            return
        process = window[2]
        if desktop.activate(process):
            self.say(f"host: focused {process}")
        else:
            self.say(f"host: {process} did not come forward")

    def _launch(self, choice: dict) -> None:
        raw = str(choice.get("executable") or "")
        name = self.executable_name(raw)
        if name is None:
            self.say(f"host: refused {raw!r}")
            return
        # Windows has no SystemSettings.exe. The settings app opens from this URI.
        if name.lower().removesuffix(".exe") == "systemsettings":
            name = "ms-settings:"
        desktop.launch_app(name)
        if self._came_forward(name):
            self.say(f"host: launched {name}")
        else:
            self.say(f"host: {name} did not come forward")
