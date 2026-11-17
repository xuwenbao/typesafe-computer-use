"""The `Desktop` surface over WAA's observations, so jev's loop drives a Windows VM unchanged.

WAA hands its agent one observation per step and takes back a list of actions: pyautogui code, or
`WAIT` / `DONE` / `FAIL`. The inputs between two reads go out as one action, because WAA runs each
action as a step of its own and hands back only the last observation. A wait with no input before
it is `WAIT`; a pause between two inputs of one step is a `time.sleep` in their code.

The tree is fetched by jev (see `tree.py`), started as the observation arrives, and a step's
actions are handed over only once that fetch has finished. A desktop icon opens through its
default action. Every other click is a pointer click: a folder window's row selects on a click,
and opening it would skip a move. Text reaches the code only through `repr()`.
"""

from __future__ import annotations

from collections.abc import Callable
from io import BytesIO
from pathlib import Path

from PIL import Image

from ..config import settings
from ..models import AxNode, ElementState, Field
from ..uia_patterns import ID_TO_NAME, OPERATION_PATTERN, OPERATIONS, PATTERN_IDS, VALUE
from ..uia_roles import ROLE_TO_CONTROL_TYPE_ID
from . import tree

NextObs = Callable[[list[str]], dict]
StartTree = Callable[[], tree.Pending]

TYPE_INTERVAL = 0.02
WAIT = "WAIT"
# Product name to the executable `start` launches. `start` brings a running instance forward.
BROWSERS = {"Google Chrome": "chrome", "Microsoft Edge": "msedge"}
# The desktop's window title. A folder window is named for the folder, and its rows stay clicks.
DESKTOP_TITLES = {"program manager", "desktop"}


class WAADesktop:
    """One WAA run's computer: the observation in hand, and the actions not yet handed over."""

    def __init__(
        self, obs: dict, next_obs: NextObs, *, start_tree: StartTree | None = None, save_tree: Path | None = None
    ) -> None:
        """`start_tree` starts a fetch of the VM's tree for each observation. `save_tree` is a
        folder that receives each observation's raw tree when `JEV_WAA_SAVE_TREE=1`."""
        self._next_obs = next_obs
        self._start_tree = start_tree
        self._save_tree = save_tree if settings().waa_save_tree else None
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

    def click_at(self, point: tuple[float, float], button: str = "left", count: int = 1, modifier: str | None = None) -> None:
        x, y = round(point[0]), round(point[1])
        key = {"add": "ctrl", "range": "shift"}.get(modifier or "")
        if modifier and key is None:
            raise ValueError(f"unknown modifier {modifier!r}")
        click = f"pyautogui.click({x}, {y}, clicks={count}, button={button!r})"
        if key:
            # keyUp runs after the click even when the click line is what the VM executes as one script.
            self._do(f"pyautogui.keyDown({key!r}); {click}; pyautogui.keyUp({key!r})")
        elif count != 1 or button != "left":
            self._do(click)
        else:
            self._do(f"pyautogui.click({x}, {y})")

    def drag(self, start: tuple[float, float], end: tuple[float, float]) -> None:
        x1, y1 = round(start[0]), round(start[1])
        x2, y2 = round(end[0]), round(end[1])
        self._do(f"pyautogui.moveTo({x1}, {y1}); pyautogui.dragTo({x2}, {y2}, duration=0.5, button='left')")

    def press(self, key: str, command: bool = False) -> None:
        if command and key == "[":
            self._do("pyautogui.hotkey('alt', 'left')")  # Back, where Command-[ is Back on a Mac
        elif command:
            self._do(f"pyautogui.hotkey('ctrl', {key!r})")
        else:
            # `delete` stays Backspace: that is the key the Mac adapter's Delete presses, and the
            # classifier's delete was aimed at it. The key named `backspace` is the same key here.
            pressed = {"return": "enter", "escape": "esc", "delete": "backspace", "start": "win"}.get(key, key)
            self._do(f"pyautogui.press({pressed!r})")

    def type_text(self, text: str) -> None:
        self._do(f"pyautogui.write({text!r}, interval={TYPE_INTERVAL})")

    def clear_field(self) -> None:
        self._do("pyautogui.hotkey('ctrl', 'a'); pyautogui.press('backspace')")

    def scroll(self, lines: int, at: tuple[float, float] | None = None) -> None:
        """Scroll under the pointer: on `at`, or over the active window's center when the tree gives it."""
        if at is not None:
            self._do(f"pyautogui.scroll({int(lines)}, x={round(at[0])}, y={round(at[1])})")
            return
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
        """Open a desktop list item by its default action. A single click only selects it."""
        if not isinstance(ref, dict) or ref.get("role") != "AXRow":
            return False
        name = (ref.get("name") or "").strip()
        if not name:
            return False
        reading = self._current()
        if reading.app != "explorer" or reading.title.strip().lower() not in DESKTOP_TITLES:
            return False
        self._do(_open_list_item(name))
        return True

    def ax_focus(self, ref) -> bool:
        """Queue a focus of the named element. Whether it took is the next screen's focused field.

        The code runs on the VM with the rest of this step, so True means it was queued, not that
        the element already has the focus. A control that supports it is scrolled into view first.
        """
        if not isinstance(ref, dict):
            return False
        name = (ref.get("name") or "").strip()
        if not name:
            return False
        self._do(_focus_element(name, ROLE_TO_CONTROL_TYPE_ID.get(ref.get("role") or "")))
        return True

    def ax_set_value(self, ref, text: str) -> bool:
        """Queue a Value-pattern write, after this observation's tree has arrived.

        True means it was queued. The script sets the value and reads it back; a mismatch is caught
        inside the VM and does not abort the step.
        """
        self._ensure()
        if not isinstance(ref, dict):
            return False
        supported = {ID_TO_NAME.get(pattern_id) for pattern_id in ref.get("patterns") or []}
        if VALUE not in supported:
            return False
        name = (ref.get("name") or "").strip()
        if not name:
            return False
        self._do(_set_value_element(name, ROLE_TO_CONTROL_TYPE_ID.get(ref.get("role") or ""), text))
        return True

    def ax_value(self, ref) -> str | None:
        return None

    def ax_operate(self, ref, operation: str, value: str | None = None) -> bool:
        """Queue one UIA operation, after this observation's tree has arrived.

        True means the operation was queued, which is how `ax_focus` reports a focus. The element
        has to list the pattern; otherwise the click falls through to the pointer.
        """
        self._ensure()
        if operation not in OPERATIONS or not isinstance(ref, dict):
            return False
        pattern = OPERATION_PATTERN[operation]
        supported = {ID_TO_NAME.get(pattern_id) for pattern_id in ref.get("patterns") or []}
        if pattern not in supported:
            return False
        name = (ref.get("name") or "").strip()
        if not name:
            return False
        self._do(_operate_element(name, ROLE_TO_CONTROL_TYPE_ID.get(ref.get("role") or ""), operation, value))
        return True

    def element_state(self, ref) -> ElementState | None:
        """Hand over whatever input is waiting, then answer from the tree of the screen that follows."""
        self._step()
        if self._ensure().raw is None or not isinstance(ref, dict):
            return None
        reading = self._current()
        name = (ref.get("name") or "").strip()
        role = ref.get("role") or ""
        for node in reading.found:
            if node.label == name and node.role == role:
                return ElementState(True, True, node.enabled)
        for node in reading.offscreen:
            if node.label == name and node.role == role:
                return ElementState(True, False, node.enabled)
        return ElementState(False, False, None)


_PATTERN_INTERFACE = {
    "toggle": "IUIAutomationTogglePattern",
    "select": "IUIAutomationSelectionItemPattern",
    "expand_toggle": "IUIAutomationExpandCollapsePattern",
    "scroll_into_view": "IUIAutomationScrollItemPattern",
    "set_range": "IUIAutomationRangeValuePattern",
}


def _operate_element(name: str, control_type: int | None, operation: str, value: str | None) -> str:
    """VM code that runs one pattern on the named element. `name` is one Python literal.

    The pattern id is the shared table's. A missing element or a refused call is caught, so input
    queued after this in the same step still runs.
    """
    pattern_id = PATTERN_IDS[OPERATION_PATTERN[operation]]
    interface = _PATTERN_INTERFACE[operation]
    if operation == "set_range":
        number = float(value or "")
        call = (
            f"if {number!r} < _p.CurrentMinimum or {number!r} > _p.CurrentMaximum:\n"
            f"            print('operate failed: outside the range')\n"
            f"        else:\n"
            f"            _p.SetValue({number!r})"
        )
    elif operation == "expand_toggle":
        call = "_p.Expand() if _p.CurrentExpandCollapseState == 0 else _p.Collapse()"
    else:
        call = {"toggle": "_p.Toggle()", "select": "_p.Select()", "scroll_into_view": "_p.ScrollIntoView()"}[operation]
    named = f"_auto.CreatePropertyCondition(30005, {name!r})"
    if control_type is None:
        condition = named
    else:
        kind = f"_auto.CreatePropertyCondition(30003, {int(control_type)})"
        condition = f"_auto.CreateAndCondition({named}, {kind})"
    return (
        "import comtypes, comtypes.client, ctypes\n"
        "try:\n"
        "    comtypes.client.GetModule('UIAutomationCore.dll')\n"
        "    from comtypes.gen import UIAutomationClient as api\n"
        "    _auto = comtypes.CoCreateInstance(api.CUIAutomation._reg_clsid_, interface=api.IUIAutomation)\n"
        "    _root = _auto.ElementFromHandle(ctypes.windll.user32.GetForegroundWindow())\n"
        f"    _item = _root.FindFirst(4, {condition})\n"
        "    if _item is None:\n"
        "        print('operate failed: element not found')\n"
        "    else:\n"
        f"        _raw = _item.GetCurrentPattern({pattern_id})\n"
        f"        _p = _raw.QueryInterface(api.{interface})\n"
        f"        {call}\n"
        "except Exception as _err:\n"
        "    print('operate failed:', _err)"
    )


def _set_value_element(name: str, control_type: int | None, text: str) -> str:
    """VM code that sets a Value-pattern control and reads it back. `text` is one Python literal."""
    pattern_id = PATTERN_IDS[VALUE]
    named = f"_auto.CreatePropertyCondition(30005, {name!r})"
    if control_type is None:
        condition = named
    else:
        kind = f"_auto.CreatePropertyCondition(30003, {int(control_type)})"
        condition = f"_auto.CreateAndCondition({named}, {kind})"
    return (
        "import comtypes, comtypes.client, ctypes\n"
        "try:\n"
        "    comtypes.client.GetModule('UIAutomationCore.dll')\n"
        "    from comtypes.gen import UIAutomationClient as api\n"
        "    _auto = comtypes.CoCreateInstance(api.CUIAutomation._reg_clsid_, interface=api.IUIAutomation)\n"
        "    _root = _auto.ElementFromHandle(ctypes.windll.user32.GetForegroundWindow())\n"
        f"    _item = _root.FindFirst(4, {condition})\n"
        "    if _item is None:\n"
        "        print('operate failed: element not found')\n"
        "    else:\n"
        f"        _raw = _item.GetCurrentPattern({pattern_id})\n"
        "        _p = _raw.QueryInterface(api.IUIAutomationValuePattern)\n"
        f"        _p.SetValue({text!r})\n"
        "        _got = _p.CurrentValue\n"
        f"        if _got != {text!r}:\n"
        "            print('operate failed: read back', _got)\n"
        "except Exception as _err:\n"
        "    print('operate failed:', _err)"
    )


def _focus_element(name: str, control_type: int | None) -> str:
    """VM code that focuses the named element, scrolling it into view when the control allows it.

    `name` is one Python literal. A missing element, or one that cannot scroll, is caught, so
    input queued after this in the same step still runs.
    """
    named = f"_auto.CreatePropertyCondition(30005, {name!r})"  # UIA_NamePropertyId
    if control_type is None:
        condition = named
    else:
        kind = f"_auto.CreatePropertyCondition(30003, {int(control_type)})"  # UIA_ControlTypePropertyId
        condition = f"_auto.CreateAndCondition({named}, {kind})"
    # The first line stays a simple statement: WAA prefixes it with `... False; `, and a `try`
    # there is a syntax error. The block below is where a missing element is swallowed.
    return (
        "import comtypes, comtypes.client, ctypes\n"
        "try:\n"
        "    comtypes.client.GetModule('UIAutomationCore.dll')\n"
        "    from comtypes.gen import UIAutomationClient as api\n"
        "    _auto = comtypes.CoCreateInstance(api.CUIAutomation._reg_clsid_, interface=api.IUIAutomation)\n"
        "    _root = _auto.ElementFromHandle(ctypes.windll.user32.GetForegroundWindow())\n"
        f"    _item = _root.FindFirst(4, {condition})\n"  # TreeScope_Descendants
        "    if _item is None:\n"
        "        print('focus failed: element not found')\n"
        "    else:\n"
        "        try:\n"
        "            _scroll = _item.GetCurrentPattern(10017)\n"  # UIA_ScrollItemPatternId
        "            _scroll.QueryInterface(api.IUIAutomationScrollItemPattern).ScrollIntoView()\n"
        "        except Exception:\n"
        "            pass\n"
        "        try:\n"
        "            _item.SetFocus()\n"
        "        except Exception as _err:\n"
        "            print('focus failed: SetFocus', _err)\n"
        "        try:\n"
        "            _legacy = _item.GetCurrentPattern(10018)\n"  # UIA_LegacyIAccessiblePatternId
        "            _legacy.QueryInterface(api.IUIAutomationLegacyIAccessiblePattern).Select(1)\n"  # SELFLAG_TAKEFOCUS
        "        except Exception as _err:\n"
        "            print('focus failed: Select', _err)\n"
        "except Exception as _err:\n"
        "    print('focus failed:', _err)"
    )


def _open_list_item(name: str) -> str:
    """VM code that runs the named list item's legacy default action. `name` is one Python literal."""
    return (
        "import comtypes, comtypes.client, ctypes\n"
        "comtypes.client.GetModule('UIAutomationCore.dll')\n"
        "from comtypes.gen import UIAutomationClient as api\n"
        "_auto = comtypes.CoCreateInstance(api.CUIAutomation._reg_clsid_, interface=api.IUIAutomation)\n"
        "_root = _auto.ElementFromHandle(ctypes.windll.user32.GetForegroundWindow())\n"
        f"_name = _auto.CreatePropertyCondition(30005, {name!r})\n"  # UIA_NamePropertyId
        "_kind = _auto.CreatePropertyCondition(30003, 50007)\n"  # UIA_ControlTypePropertyId, ListItem
        "_item = _root.FindFirst(4, _auto.CreateAndCondition(_name, _kind))\n"  # TreeScope_Descendants
        "_legacy = _item.GetCurrentPattern(10018).QueryInterface(api.IUIAutomationLegacyIAccessiblePattern)\n"
        "_legacy.DoDefaultAction()"
    )


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
