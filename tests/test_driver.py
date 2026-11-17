"""The driver's checks: every action the model names is taken only when the screen allows it."""

from dataclasses import replace

import pytest

from typesafe_computer_use import config, macos, windows
from typesafe_computer_use.driver import DEAD, KEYS, SHORTCUTS, _signature, _take, act
from typesafe_computer_use.models import AxNode, Field, Item, signature
from typesafe_computer_use.platform_adapter import desktop
from typesafe_computer_use.runner import UNCHANGED, RunState, screen_moved
from typesafe_computer_use.writer import DriveStep, WriterError, _last_action, checked


@pytest.fixture
def machine(monkeypatch):
    """Every trip to the machine, recorded instead of made."""
    log: list[tuple] = []
    monkeypatch.setattr(desktop, "check_abort", lambda: None)

    def click_at(point, button="left", count=1, modifier=None):
        log.append(("click", point, button, count, modifier))

    monkeypatch.setattr(desktop, "click_at", click_at)
    monkeypatch.setattr(desktop, "ax_press", lambda ref: False)
    monkeypatch.setattr(desktop, "press", lambda key, command=False: log.append(("press", key, command)))
    monkeypatch.setattr(desktop, "scroll", lambda lines, at=None: log.append(("scroll", lines, at)))
    monkeypatch.setattr(desktop, "type_text", lambda text: log.append(("type", text)))
    monkeypatch.setattr(desktop, "clear_field", lambda: log.append(("clear",)))
    monkeypatch.setattr(desktop, "ax_focus", lambda ref: True)
    monkeypatch.setattr(desktop, "focused_field", lambda: None)
    monkeypatch.setattr(desktop, "drag", lambda start, end: log.append(("drag", start, end)))
    monkeypatch.setattr(desktop, "sleep_watching", lambda seconds: log.append(("sleep", seconds)))
    return log


ITEMS = [Item(0, "Archive", 1.0, 100.0, 100.0, 300.0, 140.0)]


def move(action: str, **fields) -> DriveStep:
    return DriveStep(reason="test", action=action, **fields)


def text_field(label: str = "Name") -> Field:
    return Field(role="AXTextField", label=label, placeholder="", value="", x=10, y=20, w=200, h=30)


def test_a_click_lands_on_the_item_the_model_numbered(screen, machine):
    assert act(move("click", item="0"), screen, ITEMS) == "clicked 'Archive'"
    assert machine == [("click", (100.0, 60.0), "left", 1, None)]


def test_a_number_that_is_not_on_the_screen_clicks_nothing(screen, machine):
    assert act(move("click", item="7"), screen, ITEMS) == "click refused: there is no item '7' on this screen"
    assert machine == []


def test_typing_needs_a_focused_text_field(screen, machine):
    assert act(move("type", text="Kyoto"), screen, ITEMS) == "type refused: no text field is focused"
    assert machine == []


def test_typing_into_an_item_clicks_it_and_sends_keystrokes(screen, machine):
    line = act(move("type", item="0", text="30"), screen, ITEMS)
    assert line == "typed '30' into 'Archive' (keystrokes, not read back)"
    assert machine == [("click", (100.0, 60.0), "left", 1, None), ("clear",), ("type", "30")]


def test_typing_fills_the_focused_field(screen, machine):
    line = act(move("type", text="Kyoto"), replace(screen, field=text_field()), ITEMS)
    assert line == "typed 'Kyoto' into 'Name' via keystrokes"
    assert machine == [("clear",), ("type", "Kyoto")]


@pytest.mark.parametrize(("label", "text"), [("Password", "hunter2"), ("Name", "my api key is abc")])
def test_a_credential_is_never_typed(screen, machine, label, text):
    line = act(move("type", text=text), replace(screen, field=text_field(label)), ITEMS)
    assert line.startswith("type refused:") and "credential" in line
    assert machine == []


def test_a_double_click_a_right_click_and_a_modified_click_reach_the_adapter(screen, machine):
    assert act(move("double_click", item="0"), screen, ITEMS) == "double-clicked 'Archive'"
    assert act(move("right_click", item="0"), screen, ITEMS) == "right-clicked 'Archive'"
    assert act(move("click", item="0", modifier="add"), screen, ITEMS) == "clicked 'Archive' with add"
    assert machine == [
        ("click", (100.0, 60.0), "left", 2, None),
        ("click", (100.0, 60.0), "right", 1, None),
        ("click", (100.0, 60.0), "left", 1, "add"),
    ]


def test_a_click_modifier_the_model_invented_clicks_nothing(screen, machine):
    assert act(move("click", item="0", modifier="alt"), screen, ITEMS).startswith("click refused:")
    assert machine == []


def test_control_and_shift_are_the_selection_modifiers(screen, machine):
    assert act(move("click", item="0", modifier="control"), screen, ITEMS) == "clicked 'Archive' with add"
    assert act(move("click", item="0", modifier="shift"), screen, ITEMS) == "clicked 'Archive' with range"
    assert _signature(move("click", item="0", modifier="ctrl"), ITEMS) == _signature(
        move("click", item="0", modifier="add"), ITEMS
    )
    assert machine == [
        ("click", (100.0, 60.0), "left", 1, "add"),
        ("click", (100.0, 60.0), "left", 1, "range"),
    ]


def test_focus_offscreen_focuses_the_named_control(screen, machine, monkeypatch):
    node = AxNode("AXTextField", "Record directory", 0, 0, 10, 10, True, ref="the-field")
    showing = replace(screen, offscreen=[node])
    monkeypatch.setattr(desktop, "ax_focus", lambda ref: machine.append(("focus", ref)) or True)
    monkeypatch.setattr(desktop, "focused_field", lambda: text_field("Record directory"))
    assert act(move("focus_offscreen", item="0"), showing, ITEMS) == "focused 'Record directory' (off-screen control)"
    assert _signature(move("focus_offscreen", item="0"), ITEMS, showing.offscreen) == "focus_offscreen:Record directory"
    assert machine == [("focus", "the-field")]

    machine.clear()
    monkeypatch.setattr(desktop, "focused_field", lambda: text_field("Other"))
    assert act(move("focus_offscreen", item="0"), showing, ITEMS) == (
        "focus_offscreen refused: 'Record directory' did not take the focus"
    )
    assert machine == [("focus", "the-field")]

    machine.clear()
    monkeypatch.setattr(desktop, "ax_focus", lambda ref: False)
    assert act(move("focus_offscreen", item="0"), showing, ITEMS) == (
        "focus_offscreen refused: 'Record directory' did not take the focus"
    )
    assert act(move("focus_offscreen", item="4"), showing, ITEMS).startswith("focus_offscreen refused: there is no")
    assert machine == []


CANVAS = [Item(0, "Oval", 1.0, 100.0, 100.0, 180.0, 140.0), Item(1, "Canvas", 1.0, 400.0, 400.0, 800.0, 700.0)]


def test_a_drag_goes_from_one_item_to_another_or_by_pixels(screen, machine):
    assert act(move("drag", item="0", to="1"), screen, CANVAS) == "dragged 'Oval' to 'Canvas'"
    assert act(move("drag", item="0", dx="80", dy="-40"), screen, CANVAS) == "dragged 'Oval' to '80,-40'"
    assert machine == [("drag", (70.0, 60.0), (300.0, 275.0)), ("drag", (70.0, 60.0), (110.0, 40.0))]
    assert _signature(move("drag", item="0", to="1"), CANVAS) == "drag:Oval->Canvas"


def test_a_drag_that_leaves_the_screen_or_names_nothing_is_refused(screen, machine):
    edge = [Item(0, "Edge", 1.0, 1900.0, 100.0, 1980.0, 140.0)]
    assert act(move("drag", item="0", dx="100"), screen, edge).startswith("drag refused: the drag leaves")
    assert act(move("drag", item="0", dx="601"), screen, CANVAS).startswith("drag refused: a drag stays within")
    assert act(move("drag", item="0"), screen, CANVAS).startswith("drag refused: name the item")
    assert act(move("drag", item="9", to="1"), screen, CANVAS).startswith("drag refused: there is no item")
    assert machine == []


def test_only_listed_keys_are_pressed(screen, machine):
    assert act(move("key", key="tab"), screen, ITEMS) == "pressed tab"
    assert act(move("key", key="start"), screen, ITEMS) == "pressed start"
    assert act(move("key", key="f4"), screen, ITEMS).startswith("key refused: 'f4'")
    assert machine == [("press", "tab", False), ("press", "start", False)]


def test_only_listed_shortcuts_are_pressed(screen, machine):
    assert act(move("shortcut", key="x"), screen, ITEMS) == "pressed command+x (to the clipboard; the screen need not change)"
    assert act(move("shortcut", key="p"), screen, ITEMS) == "pressed command+p"
    assert act(move("shortcut", key="e"), screen, ITEMS) == "pressed command+e"
    assert act(move("shortcut", key="q"), screen, ITEMS).startswith("shortcut refused: 'q'")  # Quit
    assert act(move("shortcut", key="w"), screen, ITEMS).startswith("shortcut refused: 'w'")  # Close
    assert act(move("shortcut", key="h"), screen, ITEMS).startswith("shortcut refused: 'h'")  # Hide on a Mac
    assert act(move("shortcut", key="m"), screen, ITEMS).startswith("shortcut refused: 'm'")  # Minimize
    assert machine == [("press", "x", True), ("press", "p", True), ("press", "e", True)]


def test_a_missing_achieved_flag_defaults_to_false_and_a_missing_flag_still_fails():
    props = {"reason": {"type": "string"}, "action": {"type": "string"}, "achieved": {"type": "boolean"}}
    filled = checked({"reason": "because", "action": "wait"}, props, {"achieved": False})
    assert filled["achieved"] is False
    assert checked({"reason": "because"}, props, {"achieved": False})["action"] == ""
    with pytest.raises(WriterError, match="achieved"):
        checked({"reason": "because", "action": "wait"}, props)


def test_a_cut_is_a_clipboard_shortcut_and_an_unchanged_screen_is_not_idle(screen, machine):
    assert act(move("shortcut", key="c"), screen, ITEMS).endswith("(to the clipboard; the screen need not change)")
    seen = signature(screen, ITEMS)
    line = "driver: pressed command+x (to the clipboard; the screen need not change)"
    quiet = RunState(last=seen, history=[line], compared=0, markable=False, quiet=True)
    assert screen_moved(quiet, screen, ITEMS, lambda *_a, **_k: None) is True
    assert quiet.idle == 0 and quiet.history == [line]
    loud = RunState(last=seen, history=["driver: pressed tab"], compared=0, markable=True)
    assert screen_moved(loud, screen, ITEMS, lambda *_a, **_k: None) is True
    assert loud.idle == 1 and loud.history[0].endswith(UNCHANGED)
    state = RunState(last=seen, seen=[(seen, "shortcut x")])
    assert _take(move("shortcut", key="x"), screen, ITEMS, state).startswith("pressed command+x")
    assert ("press", "x", True) in machine


def test_a_scroll_aims_at_the_named_item_and_a_missing_item_scrolls_nothing(screen, machine):
    assert act(move("scroll_down", item="0"), screen, ITEMS) == "scrolled down over 'Archive'"
    assert act(move("scroll_up"), screen, ITEMS) == "scrolled up"
    assert act(move("scroll_down", item="7"), screen, ITEMS) == "scroll_down refused: there is no item '7' on this screen"
    assert machine == [("scroll", -10, (100.0, 60.0)), ("scroll", 10, None)]


def test_the_last_action_says_whether_it_was_refused():
    assert _last_action([]) == {"did": "", "refused": False}
    assert _last_action(["driver: pressed command+x"])["refused"] is False
    assert _last_action(["driver: type refused: no text field is focused"])["refused"] is True


def test_an_action_that_already_did_nothing_here_is_not_taken_again(screen, machine):
    seen = signature(screen, ITEMS)
    state = RunState(last=seen, seen=[(seen, f"click 'Archive'{UNCHANGED}")])
    assert _take(move("click", item="0"), screen, ITEMS, state) == DEAD
    assert machine == []


def test_an_action_the_model_made_up_does_nothing(screen, machine):
    assert act(move("hover", item="0"), screen, ITEMS) == "refused: 'hover' is not an action"
    assert machine == []


def test_every_key_the_driver_offers_is_one_both_adapters_press():
    for key in (*KEYS, *SHORTCUTS):
        assert key in windows.VK
        # `start` is Command-Space on a Mac, not its own key code.
        if key != "start":
            assert key in macos.KEYCODES


def test_the_driver_takes_over_unless_turned_off(monkeypatch):
    monkeypatch.delenv("CLICKER_DRIVER", raising=False)
    monkeypatch.delenv("CLICKER_DRIVER_STEPS", raising=False)
    assert config.settings().driver_budget == 20
    monkeypatch.setenv("CLICKER_DRIVER", "0")
    assert config.settings().driver_budget == 0
    monkeypatch.setenv("CLICKER_DRIVER", "1")
    monkeypatch.setenv("CLICKER_DRIVER_STEPS", "5")
    assert config.settings().driver_budget == 5


@pytest.mark.parametrize(("name", "value"), [("CLICKER_DRIVER", "maybe"), ("CLICKER_DRIVER_STEPS", "lots")])
def test_a_bad_driver_setting_is_an_error(monkeypatch, name, value):
    monkeypatch.setenv("CLICKER_DRIVER", "true")
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        config.settings()


def test_the_driver_model_is_the_answer_model_unless_named(monkeypatch):
    monkeypatch.delenv("CLICKER_DRIVER_MODEL", raising=False)
    assert config.settings().resolved_driver_model == config.settings().answer_model
    monkeypatch.setenv("CLICKER_DRIVER_MODEL", "some-model")
    assert config.settings().resolved_driver_model == "some-model"
