"""What one `clicker mcp` does for a tool call: jev's whole loop, one look at the screen, or one input.

One call at a time on this computer: this process holds a lock, then the user's desktop mutex, so
another `clicker mcp` is refused too. A call that finds either one taken is refused as `busy`, never
queued. Every call drives the desktop through a `GuardedDesktop`, so the gate sees each input, and
a cancel ends it at the next abort check.

Element numbers come from the latest look (`inspect_screen` or `get_app_state`) and hold only for
it: a call naming another `state_id` is refused as `stale_state`, and any input makes the look stale,
since the screen it read may have changed.
"""

from __future__ import annotations

import base64
import inspect
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .. import config
from ..actions import ActionPerformer, Context
from ..llm.writer import Writer
from ..models import Abort, Item, Missed, Screen
from ..parse.runtime import Runtime
from ..perception.pipeline import Capture, Perceiver
from ..platforms.adapter import current, desktop, using
from ..report import annotate
from ..runner import RunConfig, run
from . import home
from .approvals import Denied, Gate
from .guard import GuardedDesktop

_STEPS = config.Settings.model_fields["steps"].default
_HANDOFFS = config.Settings.model_fields["handoffs"].default
_MIN_CONFIDENCE = config.Settings.model_fields["min_confidence"].default

RUN_FIELDS = (
    "outcome",
    "answer",
    "goal_achieved",
    "steps_taken",
    "history",
    "handoffs",
    "questions",
    "calls",
    "seconds",
    "driver_error",
)


class ToolFailure(Exception):
    """A tool call that stopped. `kind` is what the MCP error text starts with."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass
class Caller:
    """The front a call came from, and the gate on the apps it may drive.

    There is no way to ask the user. `run_goal` tells the writer so, and the gate never prompts.
    """

    gate: Gate


@dataclass
class Look:
    state_id: str
    screen: Screen
    items: list[Item]


def _png(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _box(screen: Screen, it: Item) -> list[float]:
    return [round(v / screen.scale, 1) for v in (it.x1, it.y1, it.x2, it.y2)]


def _fresh_folder(parent: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out, n = parent / stamp, 1
    while out.exists():
        n += 1
        out = parent / f"{stamp}-{n}"
    return out


class Service:
    TOOLS = (
        "run_goal",
        "inspect_screen",
        "get_app_state",
        "click",
        "type_text",
        "set_value",
        "press_key",
        "scroll",
        "activate",
        "frontmost_app",
    )

    def __init__(self, desk_lock=None) -> None:
        self._lock = threading.Lock()
        self._desk = desk_lock
        self._cancels: dict[object, threading.Event] = {}
        self._look: Look | None = None

    def _desktop(self):
        if self._desk is None:
            from .desk_lock import DeskLock

            self._desk = DeskLock()
        return self._desk

    def call(self, tool: str, args: dict, caller: Caller, call_id: object) -> dict:
        if tool not in self.TOOLS:
            raise ToolFailure("invalid_arguments", f"no tool {tool!r}; jev has {', '.join(self.TOOLS)}")
        fn = getattr(self, f"_{tool}")
        try:
            inspect.signature(fn).bind(caller, **args)
        except TypeError as e:
            raise ToolFailure("invalid_arguments", f"{tool}: {e}") from None
        if not self._lock.acquire(blocking=False):
            raise ToolFailure("busy", "jev is already running another call; try again when it ends")
        cancel = threading.Event()
        self._cancels[call_id] = cancel
        desk = False
        try:
            if not self._desktop().try_acquire():
                raise ToolFailure("busy", "another jev is driving this computer; try again when it finishes")
            desk = True
            with using(GuardedDesktop(current(), caller.gate, cancel)):
                return fn(caller, **args)
        except Denied as e:
            raise ToolFailure("forbidden", str(e)) from None
        except Abort as e:
            raise ToolFailure("aborted", str(e) or "aborted") from None
        finally:
            self._cancels.pop(call_id, None)
            if desk:
                self._desk.release()
            self._lock.release()

    def cancel(self, call_id: object) -> None:
        event = self._cancels.get(call_id)
        if event is not None:
            event.set()

    # ------------------------------------------------------------ jev's loop

    def _run_goal(
        self,
        caller: Caller,
        goal: str,
        steps: int = _STEPS,
        handoffs: int = _HANDOFFS,
        min_confidence: float = _MIN_CONFIDENCE,
    ) -> dict:
        reason = Runtime.unsupported()
        if reason:
            raise ToolFailure("not_supported", reason)
        if not os.environ.get("TYPESAFE_API_KEY"):
            raise ToolFailure("not_configured", f"TYPESAFE_API_KEY is not set; put it in {home.env_file()}")
        try:
            cfg_settings = config.settings()
            writer = Writer.open()
            driver_steps = cfg_settings.driver_steps
        except ValueError as e:
            raise ToolFailure("not_configured", str(e)) from None
        out = _fresh_folder(home.runs_dir())
        cfg = RunConfig(
            goal=goal,
            out=out,
            act=True,
            steps=steps,
            min_confidence=min_confidence,
            handoffs=handoffs,
            driver_steps=driver_steps,
        )

        def ctx_factory(typesafe, history):
            return Context(
                goal=goal,
                browser=cfg_settings.browser,
                email=cfg_settings.email,
                typesafe=typesafe,
                writer=writer,
                history=history,
                ask=None,
            )

        state = run(cfg, ctx_factory)
        self._look = None
        record = json.loads((out / "run.json").read_text(encoding="utf-8"))
        data = {key: record.get(key) for key in RUN_FIELDS} | {"run_folder": str(out)}
        image = None
        if state.view is not None:
            screen, items = state.view
            annotate(screen, items, chosen="", out=out / "final.png")
            image = _png(out / "final.png")
        return {"data": data, "image": image}

    # ------------------------------------------------------------ looking

    def _see(self, goal: str) -> dict:
        tuned = config.settings()
        screen = Capture.capture(browser=tuned.browser)
        items = Perceiver.perceive(screen, tuned.max_options, goal)
        look = Look(secrets.token_hex(4), screen, items)
        self._look = look
        shot = home.state_dir() / "look.png"
        shot.parent.mkdir(parents=True, exist_ok=True)
        annotate(screen, items, chosen="", out=shot)
        field = screen.field
        data = {
            "state_id": look.state_id,
            "app": screen.app,
            "url": screen.url,
            "window": list(screen.window) if screen.window else None,
            "field": {"role": field.role, "label": field.label, "value": field.value} if field else None,
            "elements": [
                {"element": it.index, "text": it.text, "role": it.role, "source": it.source, "box": _box(screen, it)}
                for it in items
            ],
            "coordinates": f"screen points; the picture is in capture pixels, at scale {screen.scale} pixels per point",
        }
        return {"data": data, "image": _png(shot)}

    def _inspect_screen(self, caller: Caller, goal: str = "") -> dict:
        return self._see(goal)

    def _get_app_state(self, caller: Caller, app: str = "") -> dict:
        if app and not desktop.activate(app):
            raise ToolFailure("not_found", f"no window of {app!r} could be brought to the front")
        return self._see("")

    def _element(self, state_id: str, element: int) -> tuple[Screen, Item]:
        look = self._look
        if look is None or look.state_id != state_id:
            raise ToolFailure("stale_state", "that state is gone; call get_app_state again and use its state_id")
        item = next((it for it in look.items if it.index == element), None)
        if item is None:
            raise ToolFailure("invalid_arguments", f"state {state_id} has no element {element}")
        return look.screen, item

    # ------------------------------------------------------------ acting

    def _acted(self, line: str) -> dict:
        self._look = None
        return {"data": {"done": line}, "image": None}

    def _click(
        self,
        caller: Caller,
        state_id: str = "",
        element: int | None = None,
        x: float | None = None,
        y: float | None = None,
        button: str = "left",
        count: int = 1,
    ) -> dict:
        if button not in ("left", "right") or count not in (1, 2):
            raise ToolFailure("invalid_arguments", "button is left or right, and count is 1 or 2")
        if element is not None:
            screen, item = self._element(state_id, element)
            if button == "left" and count == 1:
                line = ActionPerformer().click_item(item, screen)
                if "refused" in line:
                    raise ToolFailure("missed", line)
                return self._acted(line)
            point = screen.to_points(item)
        elif x is not None and y is not None:
            point = (x, y)
        else:
            raise ToolFailure("invalid_arguments", "give an element with its state_id, or x and y")
        try:
            desktop.click_at(point, button=button, count=count)
        except Missed as e:
            raise ToolFailure("missed", str(e)) from None
        return self._acted(f"{button} click x{count} at {point}")

    def _type_text(self, caller: Caller, text: str, state_id: str = "", element: int | None = None) -> dict:
        if element is not None:
            screen, item = self._element(state_id, element)
            line = ActionPerformer().click_item(item, screen)
            if "refused" in line:
                raise ToolFailure("missed", line)
        desktop.type_text(text)
        return self._acted(f"typed {len(text)} characters")

    def _set_value(self, caller: Caller, state_id: str, element: int, value: str) -> dict:
        screen, item = self._element(state_id, element)
        ref = screen.ax_refs.get(item.index)
        if ref is None or not desktop.ax_set_value(ref, value):
            raise ToolFailure("unsupported", f"element {element} does not take a value; click it and use type_text")
        return self._acted(f"set the value of {item.text!r}")

    def _press_key(self, caller: Caller, key: str, command: bool = False) -> dict:
        try:
            desktop.press(key, command=command)
        except KeyError:
            raise ToolFailure("invalid_arguments", f"no key {key!r}") from None
        return self._acted(f"pressed {'Ctrl-' if command else ''}{key}")

    def _scroll(self, caller: Caller, lines: int, state_id: str = "", element: int | None = None) -> dict:
        at = None
        if element is not None:
            screen, item = self._element(state_id, element)
            at = screen.to_points(item)
        desktop.scroll(lines, at=at)
        return self._acted(f"scrolled {lines} lines")

    def _activate(self, caller: Caller, app: str) -> dict:
        if not desktop.activate(app):
            raise ToolFailure("not_found", f"no window of {app!r} could be brought to the front")
        return self._acted(f"brought {app} to the front")

    def _frontmost_app(self, caller: Caller) -> dict:
        app, pid = desktop.frontmost_app_and_pid()
        return {"data": {"app": app, "pid": pid}, "image": None}
