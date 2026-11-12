"""Shared fixtures, a guard that keeps every test off the real machine, and import-only
stand-ins for the platform-only modules.

The suite is pure logic and should run on any OS. The platform adapters
(`typesafe_computer_use.macos`, `typesafe_computer_use.windows`) import their
platform's packages at module scope, but the tests only ever import them -- they
call nothing but the pure rules -- so a stand-in that exists and raises on any
real use is enough to run the whole suite anywhere. Where a real module is
installed, nothing is registered for it.
"""

from __future__ import annotations

import importlib.util
import ipaddress
import json
import os
import socket
import subprocess
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace


def _absent(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is None
    except (ImportError, ValueError):
        return True


def _stub(name: str) -> types.ModuleType:
    """A module that can be imported and nothing else: every unset attribute raises."""
    module = types.ModuleType(name)

    def _getattr(attr: str) -> object:
        # Module metadata, which libraries such as PyTorch probe on every loaded module.
        if attr.startswith("__") and attr.endswith("__"):
            raise AttributeError(attr)
        raise RuntimeError(f"{name}.{attr} is unavailable on this OS; tests must not call the platform adapter")

    module.__getattr__ = _getattr
    sys.modules[name] = module
    return module


REAL_ACCESSIBILITY = not _absent("ApplicationServices")

if _absent("Quartz"):
    _stub("Quartz").kCGHIDEventTap = 0
if not REAL_ACCESSIBILITY:
    _stub("ApplicationServices")
for _windows_module in ("psutil", "uiautomation", "win32api", "win32con", "win32gui", "win32process"):
    if _absent(_windows_module):
        _stub(_windows_module)

import pytest  # noqa: E402
from PIL import Image  # noqa: E402

from typesafe_computer_use import macos, windows  # noqa: E402
from typesafe_computer_use.browser import cdp  # noqa: E402
from typesafe_computer_use.models import Item, Screen  # noqa: E402
from typesafe_computer_use.parse import caption as parse_caption  # noqa: E402
from typesafe_computer_use.parse import detect as parse_detect  # noqa: E402
from typesafe_computer_use.parse import ocr as parse_ocr  # noqa: E402


@pytest.fixture(autouse=True)
def no_real_machine(monkeypatch):
    """No test reaches the computer it runs on, whoever wrote it.

    The suite runs on the developer's own Mac, often while they use it. Every call that would
    move the pointer, press a key, run AppleScript (which opens apps and URLs), capture the
    screen, open a file, or act on another app's accessibility element refuses here, so a test
    that forgot to patch one fails instead of taking over the machine. A test that needs one
    patches it itself, after this. The pointer reads as mid-screen, never the abort corner.
    """

    def refuse(what: str):
        def call(*args, **kwargs):
            raise RuntimeError(f"a test reached the real machine through {what}; patch it in the test")

        return call

    for name in ("_post", "osascript", "screenshot", "open_path"):
        monkeypatch.setattr(macos, name, refuse(f"macos.{name}"))
    monkeypatch.setattr(macos, "mouse_location", lambda: (500.0, 500.0))
    if REAL_ACCESSIBILITY:
        for name in ("AXUIElementPerformAction", "AXUIElementSetAttributeValue"):
            monkeypatch.setattr(macos.AS, name, refuse(f"ApplicationServices.{name}"))
    # The Windows adapter: SendInput and the cursor carry all input; the rest launch, activate,
    # open, capture, or act on another app's element.
    for name in ("_send", "_move", "screenshot", "activate", "open_url", "open_path", "ax_press", "ax_focus", "ax_set_value"):
        monkeypatch.setattr(windows, name, refuse(f"windows.{name}"))
    monkeypatch.setattr(windows, "mouse_location", lambda: (500.0, 500.0))

    # The browser backend: no Chrome and no process of any kind, nothing over CDP, and no
    # connection except to a server on this machine that the test started itself.
    monkeypatch.setattr(subprocess, "Popen", refuse("subprocess.Popen"))
    for name in ("system", "posix_spawn", "posix_spawnp"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, refuse(f"os.{name}"))
    monkeypatch.setattr(cdp, "find_chrome", refuse("cdp.find_chrome"))
    monkeypatch.setattr(cdp, "_get_json", refuse("the CDP HTTP endpoint"))
    monkeypatch.setattr(cdp.websocket, "create_connection", refuse("a CDP websocket"))
    _loopback_only(monkeypatch, refuse)

    # The perception's models: hundreds of MB each, and no PyTorch at all on an Intel Mac. A test
    # that reads a capture patches the step that would load one, as `fake_models` does.
    def refuse_model(what: str):
        def call(*args, **kwargs):
            raise RuntimeError(f"a test loaded {what}; patch it in the test, for instance with the fake_models fixture")

        return call

    monkeypatch.setattr(parse_detect, "load_model", refuse_model("the icon detector"))
    monkeypatch.setattr(parse_caption, "load_model", refuse_model("the icon captioner"))
    monkeypatch.setattr(parse_ocr, "_engine", refuse_model("the OCR engine"))


@pytest.fixture
def fake_models(monkeypatch):
    """Stand-ins for the three models, fed by the test: OCR lines, icon boxes, and captions.

    Set `lines` to what OCR reads off any image (boxes in that image's pixels), `icons` to the
    detector's (box, score) pairs, and `captions` to a callable from a 64x64 crop to its caption.
    `calls` counts how often each model ran.
    """
    fake = SimpleNamespace(lines=[], icons=[], captions=lambda icon: "icon", calls={"ocr": 0, "detect": 0, "caption": 0})

    def recognize(image):
        fake.calls["ocr"] += 1
        return list(fake.lines)

    def find(image, *args, **kwargs):
        fake.calls["detect"] += 1
        return list(fake.icons)

    def describe(icons):
        fake.calls["caption"] += 1
        return [fake.captions(icon) for icon in icons]

    monkeypatch.setattr(parse_ocr, "recognize_text", recognize)
    monkeypatch.setattr(parse_detect, "detect", find)
    monkeypatch.setattr(parse_caption, "generate_captions", describe)
    return fake


def _is_loopback(host: object) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(str(host).split("%")[0]).is_loopback
    except ValueError:
        return False


def _loopback_only(monkeypatch, refuse) -> None:
    """Sockets reach this machine's loopback address and nothing else, so a test can talk to a
    fake server it started (the `endpoint` fixture) but never to the network. A name other than
    localhost is not even looked up."""
    real_connect, real_connect_ex, real_getaddrinfo = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo

    def remote(sock: socket.socket, address: object) -> bool:
        return sock.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(address[0])

    def connect(sock, address):
        if remote(sock, address):
            refuse(f"a connection to {address!r}")()
        return real_connect(sock, address)

    def connect_ex(sock, address):
        if remote(sock, address):
            refuse(f"a connection to {address!r}")()
        return real_connect_ex(sock, address)

    def getaddrinfo(host, *args, **kwargs):
        if host is not None and not _is_loopback(host.decode() if isinstance(host, bytes) else host):
            refuse(f"a lookup of {host!r}")()
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.fixture
def screen() -> Screen:
    return Screen(image=Image.new("RGB", (2000, 1200)), scale=2.0, app="Google Chrome", field=None, url=None)


def busy_page(size: tuple[int, int]) -> Image.Image:
    """A white page ruled with a gray line every third row: something is drawn under every control on
    it, so none reads as hidden (see `perception.drawn`)."""
    image = Image.new("RGB", size, "white")
    for y in range(0, size[1], 3):
        image.paste((180, 180, 180), (0, y, size[0], y + 1))
    return image


def item(index: int, text: str, x1=100, y1=100, x2=400, y2=130, conf=1.0) -> Item:
    return Item(index, text, conf, x1, y1, x2, y2)


@pytest.fixture
def make_item():
    return item


@pytest.fixture
def tmp_env(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("CLICKER_TEST_KEY", raising=False)
    return tmp_path


WRITER_ENV = (
    "CLICKER_WRITER_API",
    "CLICKER_WRITER_BASE_URL",
    "CLICKER_WRITER_API_KEY",
    "CLICKER_WRITER_VISION",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_PROFILE",
)


@pytest.fixture
def clean_env(monkeypatch):
    """No writer configuration from the shell running the tests."""
    for name in WRITER_ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def endpoint():
    """A writer endpoint on localhost, as a proxy or a local model would serve one.

    It answers the Anthropic Messages API or OpenAI's Chat Completions API by path, records each
    request with its headers, and replies with `state["reply"]`. `state["reject"]` may return an
    error message for a request body, which then gets a 400, the way an endpoint refuses a parameter.
    `state["usage"]`, when set, is the Chat Completions reply's `usage`; without it there is none.
    """
    seen: list[dict] = []
    state: dict = {"reply": '{"ok": true, "url": "https://example.com", "reason": ""}', "reject": lambda body: None}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
            refusal = state["reject"](body)
            if refusal:
                status, out = 400, {"type": "error", "error": {"type": "invalid_request_error", "message": refusal}}
            elif self.path.endswith("/chat/completions"):
                status, out = (
                    200,
                    {
                        "id": "chatcmpl-1",
                        "object": "chat.completion",
                        "created": 0,
                        "model": body["model"],
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": state["reply"],
                                    **({"reasoning_content": state["reasoning_content"]} if "reasoning_content" in state else {}),
                                },
                                "finish_reason": "stop",
                            }
                        ],
                        **({"usage": state["usage"]} if "usage" in state else {}),
                    },
                )
            else:
                status, out = (
                    200,
                    {
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "model": body["model"],
                        "content": [{"type": "text", "text": state["reply"]}],
                        "stop_reason": "end_turn",
                        "stop_sequence": None,
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    },
                )
            data = json.dumps(out).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    yield SimpleNamespace(url=f"http://127.0.0.1:{server.server_port}", seen=seen, state=state)
    server.shutdown()
    server.server_close()
