"""The MCP server inside `clicker.exe`: `clicker mcp` is what other agents start.

It stays light until the first tool call (no PyTorch, no perception, no desktop). That call loads
the models, then drives this computer in this process. A call is one tool and its result. jev
never asks the user or the agent a question. Cancelling the call stops it at the next input.

Its stdout carries the MCP protocol and nothing else. The first call sends later prints to
`state/jev.log`. `clicker status` lives in `helper.cli`.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Literal

import psutil
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ImageContent, TextContent

from .. import config
from . import home
from .approvals import Gate, host_apps

_STEPS = config.Settings.model_fields["steps"].default
_HANDOFFS = config.Settings.model_fields["handoffs"].default
_MIN_CONFIDENCE = config.Settings.model_fields["min_confidence"].default

INSTRUCTIONS = """\
jev drives this Windows computer's real screen, mouse, and keyboard. It is a tool: it does what
you call and returns the result. It never asks you or the user a question, and it never asks to
confirm an app.

Prefer run_goal: hand jev a goal in plain English and it runs its own cheap step loop to the end,
then returns its answer, every action it took, and its run folder. Use inspect_screen to look
without acting.

The primitives (get_app_state, click, type_text, set_value, press_key, scroll, activate,
frontmost_app) are for driving step by step yourself. Element numbers come from the latest
get_app_state or inspect_screen and hold only for its state_id; any input makes it stale, so look
again before the next element action. Coordinates are screen points. You are responsible for every
primitive action you send.

Terminals, shells, Settings, UAC, the lock screen, jev itself, and the agent calling these tools
are never driven, and that comes back as forbidden. IDEs may only be clicked. Every other app is
driven without a confirmation. One call runs at a time: a call made while another runs, in this
process or in another jev, fails as busy.
"""


def helper_version() -> str:
    try:
        return version("yuanji-computer-use")
    except PackageNotFoundError:
        return "unknown"


def ancestors() -> list[str]:
    """The names of every process above this one: the agent that started it, and whatever launched that."""
    try:
        return [p.name() for p in psutil.Process().parents()]
    except psutil.Error:
        return []


def to_content(result: dict) -> list[TextContent | ImageContent]:
    content: list[TextContent | ImageContent] = [
        TextContent(type="text", text=json.dumps(result.get("data"), ensure_ascii=False))
    ]
    if result.get("image"):
        content.append(ImageContent(type="image", data=result["image"], mime_type="image/png"))
    return content


class Local:
    """Tools running in this process. The service, the log, and the gate appear on the first call."""

    def __init__(self, service=None, caller=None) -> None:
        self._service = service
        self._caller = caller
        self._ids = itertools.count(1)
        self._prepared = False

    def _ensure(self):
        if not self._prepared:
            home.state_dir().mkdir(parents=True, exist_ok=True)
            log = (home.state_dir() / "jev.log").open("a", encoding="utf-8", buffering=1)
            sys.stdout = sys.stderr = log
            config.load_dotenv(home.env_file())
            self._prepared = True
        if self._service is None or self._caller is None:
            from .service import Caller, Service

            if self._service is None:
                from .desk_lock import DeskLock

                self._service = Service(desk_lock=DeskLock())
            if self._caller is None:
                self._caller = Caller(Gate(host_apps(ancestors())))
        return self._service, self._caller

    async def call(self, tool: str, args: dict) -> list[TextContent | ImageContent]:
        from .service import ToolFailure

        loop = asyncio.get_running_loop()
        service, caller = await loop.run_in_executor(None, self._ensure)
        call_id = next(self._ids)
        try:
            result = await loop.run_in_executor(None, lambda: service.call(tool, args, caller, call_id))
        except asyncio.CancelledError:
            service.cancel(call_id)
            raise
        except ToolFailure as e:
            raise ToolError(f"{e.kind}: {e.message}") from None
        return to_content(result)


def build_server(local: Local | None = None) -> FastMCP:
    local = local or Local()
    server = FastMCP("jev", instructions=INSTRUCTIONS)

    @server.tool(output_schema=None)
    async def run_goal(
        goal: str,
        steps: int = _STEPS,
        handoffs: int = _HANDOFFS,
        min_confidence: float = _MIN_CONFIDENCE,
    ) -> list[TextContent | ImageContent]:
        """Hand jev a goal in plain English; it drives this computer to the end with its own step loop.

        Returns its outcome, answer, whether the goal was reached, every action taken, the hand-offs
        and questions on the way, the requests made, the seconds spent, the run folder, and the last
        screen with its items marked. jev does not ask a question along the way: if it cannot
        continue it stops and says why. An app it refuses comes back as forbidden, with no prompt.
        """
        return await local.call(
            "run_goal", {"goal": goal, "steps": steps, "handoffs": handoffs, "min_confidence": min_confidence}
        )

    @server.tool(output_schema=None)
    async def inspect_screen(goal: str = "") -> list[TextContent | ImageContent]:
        """Read the screen without acting: the app in front, its URL and focused field, every element
        with its number, role, and box in screen points, a state_id, and the screen with the elements marked."""
        return await local.call("inspect_screen", {"goal": goal})

    @server.tool(output_schema=None)
    async def get_app_state(app: str = "") -> list[TextContent | ImageContent]:
        """Bring `app` to the front (an executable such as notepad.exe, or a browser's name such as
        Google Chrome) when given, then read the screen as inspect_screen does. A refused app is
        forbidden, and jev does not ask to confirm it."""
        return await local.call("get_app_state", {"app": app})

    @server.tool(output_schema=None)
    async def click(
        state_id: str = "",
        element: int | None = None,
        x: float | None = None,
        y: float | None = None,
        button: Literal["left", "right"] = "left",
        count: int = 1,
    ) -> list[TextContent | ImageContent]:
        """Click an element of the latest state (through the accessibility tree when it came from it),
        or the point x, y in screen points. count 2 double-clicks. A refused app is forbidden; there
        is no confirmation prompt."""
        args = {"state_id": state_id, "element": element, "x": x, "y": y, "button": button, "count": count}
        return await local.call("click", args)

    @server.tool(output_schema=None)
    async def type_text(text: str, state_id: str = "", element: int | None = None) -> list[TextContent | ImageContent]:
        """Type text where the focus is, after clicking `element` of the latest state when given.
        A refused app, including an IDE, is forbidden; jev does not ask to confirm it."""
        return await local.call("type_text", {"text": text, "state_id": state_id, "element": element})

    @server.tool(output_schema=None)
    async def set_value(state_id: str, element: int, value: str) -> list[TextContent | ImageContent]:
        """Replace an accessibility element's value in one step, without keystrokes."""
        return await local.call("set_value", {"state_id": state_id, "element": element, "value": value})

    @server.tool(output_schema=None)
    async def press_key(key: str, command: bool = False) -> list[TextContent | ImageContent]:
        """Press one key: return, tab, escape, delete, backspace, space, up, down, left, right, pageup,
        pagedown, home, end, start, f2, f5, a letter, or a digit. command holds Ctrl around it.
        A refused app is forbidden; there is no confirmation prompt."""
        return await local.call("press_key", {"key": key, "command": command})

    @server.tool(output_schema=None)
    async def scroll(lines: int, state_id: str = "", element: int | None = None) -> list[TextContent | ImageContent]:
        """Scroll by lines (negative scrolls down) over `element` of the latest state, or over the window in front."""
        return await local.call("scroll", {"lines": lines, "state_id": state_id, "element": element})

    @server.tool(output_schema=None)
    async def activate(app: str) -> list[TextContent | ImageContent]:
        """Bring an app's window to the front. A refused app is forbidden; jev does not ask to confirm it."""
        return await local.call("activate", {"app": app})

    @server.tool(output_schema=None)
    async def frontmost_app() -> list[TextContent | ImageContent]:
        """The app in front and its process id."""
        return await local.call("frontmost_app", {})

    return server


def serve() -> None:
    """Speak MCP on stdio. Nothing but protocol bytes goes to stdout."""
    build_server().run(transport="stdio", show_banner=False)


def _saved_names() -> set[str]:
    """The names with a value in jev's .env. Never the values: an agent reads this output."""
    path = home.env_file()
    if not path.is_file():
        return set()
    names = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            if value.strip().strip("'\""):
                names.add(name.strip())
    return names


def status() -> int:
    """This program's version and path, whether the bundled models are beside it, and which keys are saved."""
    print(f"version: {helper_version()}")
    print(f"command: {sys.executable}")
    print(f"home: {home.root()}")
    models = Path(sys.executable).resolve().parent / "models"
    print(f"models: {'yes' if models.is_dir() else 'no'}")
    names = _saved_names()
    compatible = ("CLICKER_WRITER_BASE_URL", "CLICKER_WRITER_API_KEY", "CLICKER_WRITER_MODEL")
    if all(name in names for name in compatible):
        writer = "compatible"
    elif "OPENAI_API_KEY" in names:
        writer = "openai"
    else:
        writer = "no"
    print(f"TYPESAFE_API_KEY: {'yes' if 'TYPESAFE_API_KEY' in names else 'no'}")
    print(f"writer: {writer}")
    return 0
