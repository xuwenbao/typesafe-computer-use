"""The `Desktop` surface over WAA's observations, so jev's loop drives a Windows VM unchanged.

WAA hands its agent one observation per step and takes back a list of actions: pyautogui code, or
`WAIT` / `DONE` / `FAIL`. The inputs between two reads go out as one action, because WAA runs each
action as a step of its own and hands back only the last observation. A wait with no input before
it is `WAIT`; a pause between two inputs of one step is a `time.sleep` in their code.

The tree is fetched by jev (see `tree.py`), started as the observation arrives, and a step's
actions are handed over only once that fetch has finished. Nothing in the VM can be pressed
through the tree, so every click is a pointer click. Text reaches the code only through `repr()`.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from io import BytesIO
from pathlib import Path

from PIL import Image

from ..models import AxNode, Field
from . import tree

NextObs = Callable[[list[str]], dict]
StartTree = Callable[[], tree.Pending]

TYPE_INTERVAL = 0.02
WAIT = "WAIT"
# Product name to the executable `start` launches. `start` brings a running instance forward.
BROWSERS = {"Google Chrome": "chrome", "Microsoft Edge": "msedge"}


class WAADesktop:
    """One WAA run's computer: the observation in hand, and the actions not yet handed over."""

    def __init__(
        self, obs: dict, next_obs: NextObs, *, start_tree: StartTree | None = None, save_tree: Path | None = None
    ) -> None:
        """`start_tree` starts a fetch of the VM's tree for each observation. `save_tree` is a
        folder that receives each observation's raw tree when `JEV_WAA_SAVE_TREE=1`."""
        self._next_obs = next_obs
        self._start_tree = start_tree
        self._save_tree = save_tree if os.environ.get(tree.SAVE_TREE) == "1" else None
        self._seen = 0
        self.fetches: list[tree.Fetch] = []
        self.actions: list[str] = []
        self._see(obs)

    def __repr__(self) -> str:
        return f"<WAADesktop, {len(self.actions)} actions pending>"

    def _see(self, obs: dict) -> None:
        self._index, self._seen = self._seen, self._seen + 1
        self._obs = obs
        self._image: Image.Image | None = None
        self._fetch: tree.Fetch | None = None
        self._pending = self._start_tree() if self._start_tree is not None else None

    def _ensure(self) -> tree.Fetch:
        """The current observation's tree, fetched by now and recorded once."""
        if self._fetch is None:
            if self._pending is None:
                self._fetch = tree.Fetch(None, "no tree", 0.0, 0, False)
            else:
                self._fetch = self._pending.result()
                self.fetches.append(self._fetch)
                if self._save_tree is not None:
                    tree.save(self._save_tree, self._index, self._fetch.raw)
        return self._fetch

    def _step(self) -> None:
        """Pending input ends the step, once the tree of the screen it leaves has arrived."""
        if self.actions:
            actions, self.actions = self.actions, []
            self._ensure()
            self._see(self._next_obs(actions))

    def _open(self) -> Image.Image:
        """The observation's screenshot, without ending the step."""
        if self._image is None:
            png = self._obs.get("screenshot")
            if not png:
                raise RuntimeError("WAA's observation has no screenshot")
            self._image = Image.open(BytesIO(png)).convert("RGB")
        return self._image

    def _current(self) -> tree.Reading:
        """The screen in hand. Pending input stays pending, so a scroll still aims at this window."""
        image = self._open()
        return tree.read(self._ensure().raw, float(image.width), float(image.height))

    def _view(self) -> tree.Reading:
        """The screen as it is now. Pending input ends the step first."""
        self._step()
        return self._current()

    def _do(self, code: str) -> None:
        """Add an input's code to the step. A new line joins them, so code after a browser launch
        runs whether or not the launch branched."""
        if self.actions and self.actions[-1] != WAIT:
            self.actions[-1] += "\n" + code
        else:
            self.actions.append(code)

    def check_abort(self) -> None:
        """No corner to slam: WAA's own step budget and `reset` stop a run."""

    def abort_hint(self) -> str:
        return "WAA's step limit, or Ctrl-C on its runner"

    def sleep_watching(self, seconds: float) -> None:
        """A pause between two inputs stays in their code. Any other is `WAIT`, its own action."""
        if seconds <= 0:
            return
        if self.actions and self.actions[-1] != WAIT:
            self._do(f"time.sleep({seconds})")
        else:
            self.actions.append(WAIT)

    def accessibility_trusted(self) -> bool:
        return True

    def click_at(self, point: tuple[float, float]) -> None:
        x, y = point
        self._do(f"pyautogui.click({round(x)}, {round(y)})")

    def press(self, key: str, command: bool = False) -> None:
        if command and key == "[":
            self._do("pyautogui.hotkey('alt', 'left')")  # Back, where Command-[ is Back on a Mac
        elif command:
            self._do(f"pyautogui.hotkey('ctrl', {key!r})")
        else:
            pressed = {"return": "enter", "escape": "esc", "delete": "backspace"}.get(key, key)
            self._do(f"pyautogui.press({pressed!r})")

    def type_text(self, text: str) -> None:
        self._do(f"pyautogui.write({text!r}, interval={TYPE_INTERVAL})")

    def clear_field(self) -> None:
        self._do("pyautogui.hotkey('ctrl', 'a'); pyautogui.press('backspace')")

    def scroll(self, lines: int) -> None:
        """Scroll under the pointer, parked over the active window's center when the tree gives it."""
        window = self._current().window
        if window is None:
            self._do(f"pyautogui.scroll({int(lines)})")
            return
        x, y, w, h = window
        self._do(f"pyautogui.scroll({int(lines)}, x={round(x + w / 2)}, y={round(y + h / 2)})")

    def frontmost_app_and_pid(self) -> tuple[str, int]:
        view = self._view()
        return view.app, view.pid

    def activate(self, app: str, timeout: float = 3.0) -> bool:
        """Bring a browser forward, starting it when it is not running. No other app."""
        exe = BROWSERS.get(app)
        if exe is None:
            return False
        self._do(_launch(exe, None))
        return True

    def open_url(self, browser: str, url: str) -> bool:
        """Open `url` in the browser, starting it when it is not running."""
        exe = BROWSERS.get(browser)
        if exe is None:
            return False
        self._do(_launch(exe, url))
        return True

    def browser_url(self, browser: str) -> str | None:
        return self._view().url

    def open_path(self, path: Path, as_text: bool = False) -> None:
        raise NotImplementedError("a WAA run shows no file on this machine")

    def frontmost_window_bounds(self, pid: int | None = None) -> tuple[float, float, float, float] | None:
        return self._view().window

    def screenshot(self) -> Image.Image:
        self._step()
        return self._open()

    def display_scale(self, image: Image.Image) -> float:
        return 1.0

    def focused_field(self) -> Field | None:
        return self._view().focused

    def actionable_elements(self, pid: int, display_w_pt: float, display_h_pt: float) -> tuple[list[AxNode], list[AxNode], bool]:
        self._step()
        reading = tree.read(self._ensure().raw, display_w_pt, display_h_pt)
        return reading.found, reading.offscreen, reading.capped

    def ax_press(self, ref) -> bool:
        return False

    def ax_focus(self, ref) -> bool:
        return False

    def ax_set_value(self, ref, text: str) -> bool:
        return False

    def ax_value(self, ref) -> str | None:
        return None


def _launch(exe: str, url: str | None) -> str:
    """VM code that runs `start <exe> [url]`. `start` focuses a running program or launches it.

    The empty argument is `start`'s window title, so a quoted executable is not taken for one.
    The URL is one argument, through `repr()` of the whole list, never part of a shell string.
    """
    command = ["cmd", "/c", "start", "", exe] if url is None else ["cmd", "/c", "start", "", exe, url]
    return (
        "subprocess.Popen("
        f"{command!r}, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)"
    )
