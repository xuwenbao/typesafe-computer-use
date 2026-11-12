"""WAADesktop, offline: observations in, pyautogui code out, and the tree fetched before any action."""

import ast
import json
from io import BytesIO
from pathlib import Path

import pytest
from conftest import busy_page

from typesafe_computer_use.waa import tree
from typesafe_computer_use.waa.desktop import WAADesktop

FIXTURES = Path(__file__).parent / "fixtures" / "waa"
DISPLAY = (1440, 900)


def png(size: tuple[int, int] = DISPLAY) -> bytes:
    out = BytesIO()
    busy_page(size).save(out, format="PNG")
    return out.getvalue()


def raw(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def observation() -> dict:
    return {"screenshot": png()}


class Ready:
    """A fetch that has already happened, so a test never waits on a thread."""

    def __init__(self, body: dict | None) -> None:
        self.fetch = tree.Fetch(body, None if body else "no tree", 0.01, 1 if body else 0, bool(body and body.get("truncated")))

    def result(self) -> tree.Fetch:
        return self.fetch


def over(handed: list | None = None, then: dict | None = None, *bodies: dict | None) -> WAADesktop:
    pending = iter(bodies or (None,))

    def start() -> Ready:
        return Ready(next(pending))

    def next_obs(actions: list[str]) -> dict:
        if handed is None:
            raise AssertionError("no step should end here")
        handed.append(actions)
        return then or observation()

    return WAADesktop(observation(), next_obs, start_tree=start if bodies else None)


def test_reads_before_input_use_the_obs_in_hand_and_the_first_read_after_input_ends_the_step():
    first, second = raw("edge-address.json"), raw("truncated.json")
    second["nodes"][0]["name"] = "下一步"
    handed: list = []
    desktop = over(handed, observation(), first, second)

    assert desktop.browser_url("Microsoft Edge") == "example.com/a"
    assert desktop.frontmost_app_and_pid() == ("Microsoft Edge", 4212)
    assert desktop.frontmost_window_bounds() == (0.0, 0.0, 1440.0, 900.0)
    assert desktop.focused_field().value == "abc"
    assert "保存" in {node.label for node in desktop.actionable_elements(4212, *DISPLAY)[0]}
    assert handed == []

    desktop.click_at((30.2, 20.4))
    desktop.press("return")
    assert desktop.screenshot().size == DISPLAY

    assert handed == [["pyautogui.click(30, 20)\npyautogui.press('enter')"]]
    assert desktop.browser_url("Google Chrome") is None
    assert desktop.frontmost_app_and_pid()[0] == "Google Chrome"


def test_the_tree_is_fetched_before_the_actions_are_handed_over():
    order: list[str] = []

    class Slow(Ready):
        def result(self) -> tree.Fetch:
            order.append("tree")
            return super().result()

    def next_obs(actions: list[str]) -> dict:
        order.append("actions")
        return observation()

    desktop = WAADesktop(observation(), next_obs, start_tree=lambda: Slow(raw("edge-address.json")))
    desktop.click_at((1, 1))
    desktop.screenshot()
    assert order == ["tree", "actions"]


def test_keys_map_onto_pyautogui():
    desktop = over()
    desktop.press("return")
    desktop.press("escape")
    desktop.press("[", command=True)
    desktop.press("a", command=True)
    desktop.press("tab")
    desktop.press("delete")
    (code,) = desktop.actions
    assert code.splitlines() == [
        "pyautogui.press('enter')",
        "pyautogui.press('esc')",
        "pyautogui.hotkey('alt', 'left')",
        "pyautogui.hotkey('ctrl', 'a')",
        "pyautogui.press('tab')",
        "pyautogui.press('backspace')",
    ]


def test_text_goes_in_only_as_a_string_literal():
    hostile = "it's \"quoted\"'); import os; os.system('true') #\nsecond line"
    desktop = over()
    desktop.type_text(hostile)
    desktop.open_url("Google Chrome", hostile)
    desktop.open_url("Microsoft Edge", "https://example.com")
    assert desktop.activate("Files") is False
    assert desktop.activate("Microsoft Edge") is True

    (code,) = desktop.actions
    constants = [node.value for node in ast.walk(ast.parse(code)) if isinstance(node, ast.Constant)]
    assert hostile in constants

    calls = []

    class Subprocess:
        DEVNULL = -3

        @staticmethod
        def Popen(args, **kwargs):
            calls.append(args)

    class Pyautogui:
        @staticmethod
        def write(text, interval):
            calls.append(("write", text))

    exec(code, {"subprocess": Subprocess, "pyautogui": Pyautogui})
    assert ("write", hostile) in calls
    assert calls[1][:5] == ["cmd", "/c", "start", "", "chrome"]
    assert calls[1][-1] == hostile
    assert ["cmd", "/c", "start", "", "msedge", "https://example.com"] in calls
    assert ["cmd", "/c", "start", "", "msedge"] in calls


def test_a_wait_stands_alone_and_a_pause_between_inputs_stays_in_their_code():
    desktop = over(None, None, raw("edge-address.json"))
    desktop.sleep_watching(3.0)
    desktop.click_at((12.4, 99.6))
    desktop.clear_field()
    desktop.scroll(-10)
    desktop.sleep_watching(0)
    desktop.sleep_watching(0.5)
    desktop.press("return")
    assert desktop.actions[0] == "WAIT"
    assert desktop.actions[1].splitlines() == [
        "pyautogui.click(12, 100)",
        "pyautogui.hotkey('ctrl', 'a'); pyautogui.press('backspace')",
        "pyautogui.scroll(-10, x=720, y=450)",
        "time.sleep(0.5)",
        "pyautogui.press('enter')",
    ]
    ast.parse(desktop.actions[1])


def test_with_no_tree_nothing_is_known_but_the_screenshot():
    desktop = over()
    assert desktop.frontmost_app_and_pid() == ("", 0)
    assert desktop.frontmost_window_bounds() is None
    assert desktop.focused_field() is None
    assert desktop.browser_url("Google Chrome") is None
    assert desktop.actionable_elements(0, *DISPLAY) == ([], [], False)
    image = desktop.screenshot()
    assert image.size == DISPLAY and image.mode == "RGB"
    assert desktop.display_scale(image) == 1.0
    desktop.scroll(5)
    assert desktop.actions == ["pyautogui.scroll(5)"]


def test_nothing_can_be_pressed_through_the_tree(tmp_path, monkeypatch):
    monkeypatch.setenv(tree.SAVE_TREE, "1")
    body = raw("edge-address.json")
    desktop = WAADesktop(observation(), lambda actions: observation(), start_tree=lambda: Ready(body), save_tree=tmp_path)
    assert desktop.ax_press(object()) is False
    assert desktop.ax_focus(object()) is False
    assert desktop.ax_set_value(object(), "hi") is False
    assert desktop.ax_value(object()) is None
    assert desktop.accessibility_trusted() is True
    assert desktop.check_abort() is None
    with pytest.raises(NotImplementedError):
        desktop.open_path(Path("state.txt"))
    desktop.focused_field()
    assert (tmp_path / "obs-000-uia.json").is_file()
    assert desktop.actions == []


def test_an_observation_without_a_screenshot_says_so():
    desktop = WAADesktop({"screenshot": None}, lambda actions: {})
    with pytest.raises(RuntimeError, match="no screenshot"):
        desktop.screenshot()
