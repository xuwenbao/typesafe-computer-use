"""Stand-ins shared by the helper's tests: a desktop that records input, and a socket as a wire stream."""

from __future__ import annotations

import contextlib
import socket

from yuanji_computer_use.models import Abort


class SocketStream:
    """One end of a socket pair, as the wire's `Stream`; the tests' stand-in for the named pipe."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock

    def read(self, n: int) -> bytes:
        try:
            return self.sock.recv(n)
        except OSError:
            return b""

    def write(self, data: bytes) -> None:
        self.sock.sendall(data)

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.shutdown(socket.SHUT_RDWR)
        self.sock.close()


def stream_pair() -> tuple[SocketStream, SocketStream]:
    a, b = socket.socketpair()
    return SocketStream(a), SocketStream(b)


class FakeDesktop:
    """Records every input; the app in front is whatever the test sets."""

    def __init__(self, front: str = "notepad.exe") -> None:
        self.front = front
        self.calls: list[tuple] = []
        self.corner = False

    def frontmost_app_and_pid(self):
        return self.front, 42

    def check_abort(self):
        if self.corner:
            raise Abort("mouse in top-left corner")

    def abort_hint(self):
        return "the corner"

    def click_at(self, point, button="left", count=1, modifier=None):
        self.calls.append(("click_at", point, button, count))

    def press(self, key, command=False):
        self.calls.append(("press", key, command))

    def type_text(self, text):
        self.calls.append(("type_text", text))

    def clear_field(self):
        self.calls.append(("clear_field",))

    def scroll(self, lines, at=None):
        self.calls.append(("scroll", lines, at))

    def activate(self, app, timeout=3.0):
        self.calls.append(("activate", app))
        return True

    def open_url(self, browser, url):
        self.calls.append(("open_url", browser, url))
        return True

    def ax_press(self, ref):
        self.calls.append(("ax_press", ref))
        return True

    def ax_focus(self, ref):
        self.calls.append(("ax_focus", ref))
        return True

    def ax_set_value(self, ref, text):
        self.calls.append(("ax_set_value", ref, text))
        return True

    def screenshot(self):
        return "pixels"
