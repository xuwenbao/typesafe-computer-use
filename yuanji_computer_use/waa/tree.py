"""The WAA VM's foreground UI Automation tree, fetched and read by jev.

`script` is `uia_roles.py`, `uia_patterns.py`, then `walk_vm.py`: the VM runs it with `python -c` and prints
JSON. The fetch runs on its own thread. A failure, a timeout, or a window that is not there is a
step with no tree, and the screen is still read from the screenshot. Nothing here reaches this
machine; `run` is the call that posts the script to the VM.
"""

from __future__ import annotations

import json
import re
import statistics
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..models import AxNode, Field
from ..platforms.ax_walk import AX_PRESS, AxAttrs, AxFacts, walk_actionable
from ..platforms.uia_patterns import ID_TO_NAME

WALK_SECONDS = 8.0  # the script stops itself here
GRACE_SECONDS = 2.0  # how long the client waits after that before giving up
COMMAND_LIMIT = 32 * 1024  # Windows' command line; the script is one argument of `python -c`
ADDRESS = re.compile(r"(?i)address")
# Executable stems the classifier should see as the browser's product name, as windows.py does.
APP_NAMES = {"chrome": "Google Chrome", "msedge": "Microsoft Edge", "firefox": "Firefox", "brave": "Brave Browser"}

Run = Callable[[str, float], tuple[int, str]]  # (script, timeout) -> (return code, stdout)
PACKAGE = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Fetch:
    """One observation's tree, and how it was had."""

    raw: dict | None  # as the VM printed it; None when the fetch failed
    reason: str | None  # why there is no tree, or "truncated" when the walk stopped early
    seconds: float
    nodes: int
    truncated: bool


class Pending:
    """A fetch started as the observation arrives, so the screenshot can be read while it runs."""

    def __init__(self, run: Run, timeout: float = WALK_SECONDS + GRACE_SECONDS) -> None:
        self._timeout = timeout
        self._fetch: Fetch | None = None
        self._done = threading.Event()
        self._started = time.monotonic()

        def work() -> None:
            self._fetch = Tree._once(run, timeout)
            self._done.set()

        threading.Thread(target=work, name="jev-waa-tree", daemon=True).start()

    def result(self) -> Fetch:
        """The tree, once the fetch has ended or the grace has run out."""
        if not self._done.wait(self._timeout):
            return Fetch(None, "timed out", time.monotonic() - self._started, 0, False)
        assert self._fetch is not None
        return self._fetch


@dataclass(frozen=True)
class Reading:
    """What jev reads off one tree."""

    app: str
    pid: int
    window: tuple[float, float, float, float] | None
    focused: Field | None
    url: str | None
    found: list[AxNode]
    offscreen: list[AxNode]
    capped: bool
    title: str = ""


class Tree:
    """Fetch the VM's UI Automation tree and read it."""

    @staticmethod
    def script() -> str:
        """The program the VM runs: the role table, the walk, and a call to `main`.

        Only the first file may carry `from __future__ import annotations`. A later copy is a
        syntax error, because that import has to be the first statement of the whole script.
        """
        roles = (PACKAGE / "platforms" / "uia_roles.py").read_text(encoding="utf-8")
        patterns = Tree._without_future((PACKAGE / "platforms" / "uia_patterns.py").read_text(encoding="utf-8"))
        walk = Tree._without_future((PACKAGE / "waa" / "walk_vm.py").read_text(encoding="utf-8"))
        return f"{roles}\n{patterns}\n{walk}\nmain()\n"

    @staticmethod
    def _without_future(source: str) -> str:
        return source.replace("from __future__ import annotations\n", "", 1)

    @staticmethod
    def app_name(exe: str) -> str:
        """`C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe` as `Google Chrome`."""
        stem = exe.replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")
        return APP_NAMES.get(stem, stem)

    @staticmethod
    def _count(nodes: list) -> int:
        return sum(1 + Tree._count(node.get("children") or []) for node in nodes)

    @staticmethod
    def _once(run: Run, timeout: float) -> Fetch:
        started = time.monotonic()
        try:
            code, out = run(Tree.script(), timeout)
        except Exception as error:
            return Fetch(None, str(error), time.monotonic() - started, 0, False)
        seconds = time.monotonic() - started
        if code != 0:
            return Fetch(None, out.strip() or f"exit {code}", seconds, 0, False)
        try:
            raw = json.loads(out)
        except json.JSONDecodeError:
            return Fetch(None, "invalid JSON", seconds, 0, False)
        if not isinstance(raw, dict) or not isinstance(raw.get("nodes"), list):
            return Fetch(None, "invalid JSON", seconds, 0, False)
        return Fetch(raw, raw.get("reason"), seconds, Tree._count(raw["nodes"]), bool(raw.get("truncated")))

    @staticmethod
    def _window(raw: dict | None) -> tuple[float, float, float, float] | None:
        window = raw.get("window") if raw else None
        if not isinstance(window, dict):
            return None
        try:
            return float(window["x"]), float(window["y"]), float(window["w"]), float(window["h"])
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _each(nodes: list):
        for node in nodes:
            yield node
            yield from Tree._each(node.get("children") or [])

    @staticmethod
    def _field(node: dict) -> Field:
        rect = node.get("rect") or [0, 0, 0, 0]
        x, y, w, h = (float(v) for v in rect)
        return Field(node.get("role") or "", (node.get("name") or "").strip(), "", node.get("value") or "", x, y, w, h)

    @staticmethod
    def _focused(nodes: list) -> Field | None:
        """The focused element. A text field wins over a host that is also marked focused."""
        text = None
        other = None
        for node in Tree._each(nodes):
            if not node.get("focused"):
                continue
            field = Tree._field(node)
            if field.is_text and text is None:
                text = field
            elif other is None:
                other = field
        return text if text is not None else other

    @staticmethod
    def _title(raw: dict | None) -> str:
        window = raw.get("window") if raw else None
        if isinstance(window, dict):
            return str(window.get("name") or "")
        return ""

    @staticmethod
    def _url(nodes: list) -> str | None:
        for node in Tree._each(nodes):
            if node.get("role") == "AXTextField" and ADDRESS.search(node.get("name") or ""):
                value = node.get("value") or ""
                return value or None
        return None

    @staticmethod
    def _children(node: dict):
        return node.get("children") or []

    @staticmethod
    def _attrs(node: dict) -> AxAttrs:
        rect = node.get("rect")
        frame = tuple(float(v) for v in rect) if isinstance(rect, list) and len(rect) == 4 else None
        return AxAttrs(node.get("role") or "", (node.get("name") or "").strip(), frame)

    @staticmethod
    def _actions(node: dict) -> AxFacts:
        names: list[str] = [AX_PRESS] if node.get("pressable") else []
        for pattern_id in node.get("patterns") or []:
            name = ID_TO_NAME.get(pattern_id)
            if name and name not in names:
                names.append(name)
        enabled = node.get("enabled")
        if not isinstance(enabled, bool):
            enabled = None
        checked = node.get("checked")
        expanded = node.get("expanded")
        selected = node.get("selected")
        return AxFacts(
            tuple(names),
            enabled=enabled,
            checked=checked if isinstance(checked, bool) else None,
            expanded=expanded if isinstance(expanded, bool) else None,
            current=node.get("value") or None,
            selected=selected if isinstance(selected, bool) else None,
        )

    @staticmethod
    def read(raw: dict | None, display_w: float, display_h: float) -> Reading:
        """The app, the window, the focused field, the address, and the controls `ax_walk` keeps.

        `raw` of None, or a tree with no nodes, reads as nothing known.
        """
        app = Tree.app_name(raw.get("app") or "") if raw else ""
        pid = int(raw.get("pid") or 0) if raw else 0
        window = Tree._window(raw)
        nodes = raw.get("nodes") if raw else None
        title = Tree._title(raw)
        if not nodes:
            return Reading(app, pid, window, None, None, [], [], False, title)
        root = {"role": "", "name": "", "rect": None, "pressable": False, "children": nodes}
        found, offscreen, capped = walk_actionable(root, Tree._children, Tree._attrs, Tree._actions, display_w, display_h)
        return Reading(
            app,
            pid,
            window,
            Tree._focused(nodes),
            Tree._url(nodes),
            found,
            offscreen,
            capped or bool(raw.get("truncated")),
            title,
        )

    @staticmethod
    def _span(values: list[float]) -> dict[str, float]:
        return {"mean": statistics.fmean(values), "max": max(values)} if values else {"mean": 0.0, "max": 0.0}

    @staticmethod
    def summary(fetches: list[Fetch]) -> dict:
        """The counts a run records: how many fetches, how many failed, and what they cost."""
        failed = [fetch for fetch in fetches if fetch.raw is None]
        return {
            "tree_fetches": len(fetches),
            "tree_failures": len(failed),
            "tree_failure_reasons": [fetch.reason for fetch in failed if fetch.reason],
            "tree_truncated": sum(1 for fetch in fetches if fetch.truncated),
            "tree_seconds": Tree._span([fetch.seconds for fetch in fetches]),
            "tree_nodes": Tree._span([float(fetch.nodes) for fetch in fetches]),
        }

    @staticmethod
    def save(directory: Path, index: int, raw: dict | None) -> None:
        """Write one observation's raw tree as `obs-NNN-uia.json`. Nothing is written when there is none."""
        if raw is None:
            return
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"obs-{index:03d}-uia.json").write_text(json.dumps(raw), encoding="utf-8")
