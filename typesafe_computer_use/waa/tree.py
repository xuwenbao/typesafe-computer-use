"""The WAA VM's foreground UI Automation tree, fetched and read by jev.

`script` is `uia_roles.py` followed by `walk_vm.py`: the VM runs it with `python -c` and prints
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

from ..ax_walk import AX_PRESS, AxAttrs, walk_actionable
from ..models import AxNode, Field

WALK_SECONDS = 8.0  # the script stops itself here
GRACE_SECONDS = 2.0  # how long the client waits after that before giving up
COMMAND_LIMIT = 32 * 1024  # Windows' command line; the script is one argument of `python -c`
ADDRESS = re.compile(r"(?i)address")
SAVE_TREE = "JEV_WAA_SAVE_TREE"  # "1" stores each observation's raw JSON in the run folder
# Executable stems the classifier should see as the browser's product name, as windows.py does.
APP_NAMES = {"chrome": "Google Chrome", "msedge": "Microsoft Edge", "firefox": "Firefox", "brave": "Brave Browser"}

Run = Callable[[str, float], tuple[int, str]]  # (script, timeout) -> (return code, stdout)
PACKAGE = Path(__file__).resolve().parent.parent


def script() -> str:
    """The program the VM runs: the role table, the walk, and a call to `main`."""
    roles = (PACKAGE / "uia_roles.py").read_text(encoding="utf-8")
    walk = (PACKAGE / "waa" / "walk_vm.py").read_text(encoding="utf-8")
    return f"{roles}\n{walk}\nmain()\n"


def app_name(exe: str) -> str:
    """`C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe` as `Google Chrome`."""
    stem = exe.replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")
    return APP_NAMES.get(stem, stem)


@dataclass(frozen=True)
class Fetch:
    """One observation's tree, and how it was had."""

    raw: dict | None  # as the VM printed it; None when the fetch failed
    reason: str | None  # why there is no tree, or "truncated" when the walk stopped early
    seconds: float
    nodes: int
    truncated: bool


def _count(nodes: list) -> int:
    return sum(1 + _count(node.get("children") or []) for node in nodes)


def _once(run: Run, timeout: float) -> Fetch:
    started = time.monotonic()
    try:
        code, out = run(script(), timeout)
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
    return Fetch(raw, raw.get("reason"), seconds, _count(raw["nodes"]), bool(raw.get("truncated")))


class Pending:
    """A fetch started as the observation arrives, so the screenshot can be read while it runs."""

    def __init__(self, run: Run, timeout: float = WALK_SECONDS + GRACE_SECONDS) -> None:
        self._timeout = timeout
        self._fetch: Fetch | None = None
        self._done = threading.Event()
        self._started = time.monotonic()

        def work() -> None:
            self._fetch = _once(run, timeout)
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


def _window(raw: dict | None) -> tuple[float, float, float, float] | None:
    window = raw.get("window") if raw else None
    if not isinstance(window, dict):
        return None
    try:
        return float(window["x"]), float(window["y"]), float(window["w"]), float(window["h"])
    except (KeyError, TypeError, ValueError):
        return None


def _each(nodes: list):
    for node in nodes:
        yield node
        yield from _each(node.get("children") or [])


def _focused(nodes: list) -> Field | None:
    for node in _each(nodes):
        if not node.get("focused"):
            continue
        rect = node.get("rect") or [0, 0, 0, 0]
        x, y, w, h = (float(v) for v in rect)
        return Field(node.get("role") or "", (node.get("name") or "").strip(), "", node.get("value") or "", x, y, w, h)
    return None


def _url(nodes: list) -> str | None:
    for node in _each(nodes):
        if node.get("role") == "AXTextField" and ADDRESS.search(node.get("name") or ""):
            value = node.get("value") or ""
            return value or None
    return None


def _children(node: dict):
    return node.get("children") or []


def _attrs(node: dict) -> AxAttrs:
    rect = node.get("rect")
    frame = tuple(float(v) for v in rect) if isinstance(rect, list) and len(rect) == 4 else None
    return AxAttrs(node.get("role") or "", (node.get("name") or "").strip(), frame)


def _actions(node: dict) -> list[str]:
    return [AX_PRESS] if node.get("pressable") else []


def read(raw: dict | None, display_w: float, display_h: float) -> Reading:
    """The app, the window, the focused field, the address, and the controls `ax_walk` keeps.

    `raw` of None, or a tree with no nodes, reads as nothing known.
    """
    app = app_name(raw.get("app") or "") if raw else ""
    pid = int(raw.get("pid") or 0) if raw else 0
    window = _window(raw)
    nodes = raw.get("nodes") if raw else None
    if not nodes:
        return Reading(app, pid, window, None, None, [], [], False)
    root = {"role": "", "name": "", "rect": None, "pressable": False, "children": nodes}
    found, offscreen, capped = walk_actionable(root, _children, _attrs, _actions, display_w, display_h)
    return Reading(app, pid, window, _focused(nodes), _url(nodes), found, offscreen, capped or bool(raw.get("truncated")))


def _span(values: list[float]) -> dict[str, float]:
    return {"mean": statistics.fmean(values), "max": max(values)} if values else {"mean": 0.0, "max": 0.0}


def summary(fetches: list[Fetch]) -> dict:
    """The counts a run records: how many fetches, how many failed, and what they cost."""
    failed = [fetch for fetch in fetches if fetch.raw is None]
    return {
        "tree_fetches": len(fetches),
        "tree_failures": len(failed),
        "tree_failure_reasons": [fetch.reason for fetch in failed if fetch.reason],
        "tree_truncated": sum(1 for fetch in fetches if fetch.truncated),
        "tree_seconds": _span([fetch.seconds for fetch in fetches]),
        "tree_nodes": _span([float(fetch.nodes) for fetch in fetches]),
    }


def save(directory: Path, index: int, raw: dict | None) -> None:
    """Write one observation's raw tree as `obs-NNN-uia.json`. Nothing is written when there is none."""
    if raw is None:
        return
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"obs-{index:03d}-uia.json").write_text(json.dumps(raw), encoding="utf-8")
