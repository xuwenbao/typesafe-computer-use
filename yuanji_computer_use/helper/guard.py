"""The adapter the helper drives through: the real one, with the gate in front of every input.

Put in place with `platform_adapter.using()` for the length of one tool call, so jev's loop and the
primitives alike reach the desktop only through it. Reads pass straight through. Every input first
asks the gate about the app in front (or, for `activate` and `open_url`, the app being brought
there). A client's cancel shows up at the next abort check, as the corner does.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from ..models import Abort
from .approvals import Gate

CANCELLED = "cancelled by the client"


class GuardedDesktop:
    def __init__(self, inner, gate: Gate, cancel: threading.Event) -> None:
        self._inner = inner
        self._gate = gate
        self._cancel = cancel

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def _front(self, typing: bool = False) -> None:
        self.check_abort()
        app, _ = self._inner.frontmost_app_and_pid()
        self._gate.check(app, typing=typing)

    # the escape hatch
    def check_abort(self) -> None:
        if self._cancel.is_set():
            raise Abort(CANCELLED)
        self._inner.check_abort()

    def abort_hint(self) -> str:
        return f"the client's cancel, or {self._inner.abort_hint()}"

    def sleep_watching(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while True:
            self.check_abort()
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(0.1, left))

    # input
    def click_at(self, point, button: str = "left", count: int = 1, modifier: str | None = None) -> None:
        self._front()
        self._inner.click_at(point, button=button, count=count, modifier=modifier)

    def press(self, key: str, command: bool = False) -> None:
        self._front(typing=True)
        self._inner.press(key, command=command)

    def type_text(self, text: str) -> None:
        self._front(typing=True)
        self._inner.type_text(text)

    def clear_field(self) -> None:
        self._front(typing=True)
        self._inner.clear_field()

    def scroll(self, lines: int, at=None) -> None:
        self._front()
        self._inner.scroll(lines, at=at)

    # apps and windows
    def activate(self, app: str, timeout: float = 3.0) -> bool:
        self.check_abort()
        self._gate.check(app)
        return self._inner.activate(app, timeout=timeout)

    def open_url(self, browser: str, url: str) -> bool:
        self.check_abort()
        self._gate.check(browser, typing=True)
        return self._inner.open_url(browser, url)

    def open_path(self, path: Path, as_text: bool = False) -> None:
        raise Abort("the helper does not open files")

    # acting on an element
    def ax_press(self, ref) -> bool:
        self._front()
        return self._inner.ax_press(ref)

    def ax_focus(self, ref) -> bool:
        self._front()
        return self._inner.ax_focus(ref)

    def ax_set_value(self, ref, text: str) -> bool:
        self._front(typing=True)
        return self._inner.ax_set_value(ref, text)
