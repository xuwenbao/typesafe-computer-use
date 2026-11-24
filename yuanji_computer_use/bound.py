"""Run one call without letting it hold the caller, and collect a capture from a process that can be killed.

`threading.Event.wait` on Windows is an alertable wait: COM delivers a call back onto the thread
that is waiting and the timeout never resumes. A capture therefore runs in another process, and
the parent waits on a handle that does not dispatch those calls.
"""

from __future__ import annotations

import ctypes
import json
import os
import threading
from collections.abc import Callable
from ctypes import wintypes

from .timing import FIELD_TIMEOUT, URL_TIMEOUT


class CaptureTimeout(Exception):
    """A screenshot did not return before its limit, so the step cannot start."""


def call_within[T](seconds: float, fn: Callable[[], T], default: T) -> T:
    """Return what `fn` returns, or `default` when it has not returned in `seconds`.

    An error that arrives in time is raised here. One that arrives after the limit is
    dropped with the thread, which ends on its own. macOS and the tests use this. Windows
    capture does not: that wait would run the UI call on this thread.
    """
    box: list[T] = []
    error: list[Exception] = []
    done = threading.Event()

    def run() -> None:
        try:
            box.append(fn())
        except Exception as caught:
            error.append(caught)
        finally:
            done.set()

    threading.Thread(target=run, name="jev-bound", daemon=True).start()
    if not done.wait(seconds):
        return default
    if error:
        raise error[0]
    return box[0]


class SilentEvent:
    """Set from one thread, waited on from another. The Windows wait does not dispatch COM."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._handle = None
        self._kernel = None
        if os.name != "nt":
            return
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateEventW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)
        kernel.CreateEventW.restype = wintypes.HANDLE
        kernel.SetEvent.argtypes = (wintypes.HANDLE,)
        kernel.SetEvent.restype = wintypes.BOOL
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        self._kernel = kernel
        self._handle = kernel.CreateEventW(None, True, False, None)

    def set(self) -> None:
        if self._handle is None or self._kernel is None:
            self._event.set()
            return
        self._kernel.SetEvent(self._handle)

    def wait(self, seconds: float) -> bool:
        if self._handle is None or self._kernel is None:
            return self._event.wait(seconds)
        return self._kernel.WaitForSingleObject(self._handle, max(0, int(seconds * 1000))) == 0

    def close(self) -> None:
        if self._handle is not None and self._kernel is not None:
            self._kernel.CloseHandle(self._handle)
            self._handle = None


class CaptureLines:
    """The two lines a capture process prints: `shot`, then one JSON object."""

    def __init__(self, shot: SilentEvent, done: SilentEvent) -> None:
        self.shot = shot
        self.done = done
        self.detail: str | None = None

    def feed(self, line: str) -> None:
        text = line.strip()
        if not text:
            return
        if text == "shot":
            self.shot.set()
            return
        self.detail = text
        self.done.set()


def await_capture(shot, done, shot_wait: float, detail_wait: float, kill: Callable[[], None]) -> tuple[bool, bool]:
    """Wait for the screenshot line, then for the focus and address. Kill the process if either misses.

    Returns whether the screenshot arrived and whether the detail line arrived.
    """
    if not shot.wait(shot_wait):
        kill()
        return False, False
    if not done.wait(detail_wait):
        kill()
        return True, False
    return True, True


def assemble_capture(got_shot: bool, detail: str | None, image, timing: dict | None):
    """The image from a finished read, or CaptureTimeout when the screenshot never arrived.

    A screenshot with no detail line is a screen whose focus and address counted as absent.
    `detail` is the JSON line: `{"field": record | null, "url": str | null}`.
    """
    if not got_shot or image is None:
        raise CaptureTimeout("screenshot")
    if not detail:
        if timing is not None:
            timing[FIELD_TIMEOUT] = 1.0
            timing[URL_TIMEOUT] = 1.0
        return image, None, None
    try:
        data = json.loads(detail)
    except json.JSONDecodeError:
        data = {}
    return image, data.get("field"), data.get("url")
