"""One desktop for this user, shared by every `clicker mcp` process.

`Local\\jev-desk-<SID>` is a mutex. Taking it fails at once when another process holds it, and
Windows releases it if the holder crashes. A mutex belongs to the thread that took it, so a call
acquires and releases it on the same thread.
"""

from __future__ import annotations

import win32api
import win32event
import win32security

# Win32 wait results. Named here so importing this module does not touch win32event's constants:
# the test suite stubs that module, and reading an attribute of the stub is a refused call.
WAIT_OBJECT_0 = 0x00000000
WAIT_ABANDONED = 0x00000080
WAIT_OWNED = (WAIT_OBJECT_0, WAIT_ABANDONED)


class DeskLock:
    """The desktop mutex. `try_acquire` is non-blocking; `release` is the same thread."""

    @staticmethod
    def user_sid() -> str:
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
        sid, _ = win32security.GetTokenInformation(token, win32security.TokenUser)
        return win32security.ConvertSidToStringSid(sid)

    @staticmethod
    def mutex_name() -> str:
        return rf"Local\jev-desk-{DeskLock.user_sid()}"

    @staticmethod
    def open_mutex():
        """This user's desktop mutex, created if no jev process has asked yet."""
        return win32event.CreateMutex(None, False, DeskLock.mutex_name())

    def __init__(self, opener=None) -> None:
        self._opener = DeskLock.open_mutex if opener is None else opener
        self._handle = None

    def try_acquire(self) -> bool:
        if self._handle is None:
            self._handle = self._opener()
        return win32event.WaitForSingleObject(self._handle, 0) in WAIT_OWNED

    def release(self) -> None:
        win32event.ReleaseMutex(self._handle)
