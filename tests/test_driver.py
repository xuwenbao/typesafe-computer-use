"""The driver's checks: every action the model names is taken only when the screen allows it."""

from dataclasses import replace

import pytest

from typesafe_computer_use import config, macos, windows
from typesafe_computer_use.driver import DEAD, KEYS, SHORTCUTS, _take, act
from typesafe_computer_use.models import Field, Item, signature
from typesafe_computer_use.platform_adapter import desktop
from typesafe_computer_use.runner import UNCHANGED, RunState
from typesafe_computer_use.writer import DriveStep, WriterError, checked


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
    monkeypatch.setattr(desktop, "scroll", lambda lines: log.append(("scroll", lines)))
    monkeypatch.setattr(desktop, "type_text", lambda text: log.append(("type", text)))
    monkeypatch.setattr(desktop, "clear_field", lambda: log.append(("clear",)))
    monkeypatch.setattr(desktop, "ax_focus", lambda ref: True)
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


def test_only_listed_keys_are_pressed(screen, machine):
    assert act(move("key", key="tab"), screen, ITEMS) == "pressed tab"
    assert act(move("key", key="f4"), screen, ITEMS).startswith("key refused: 'f4'")
    assert machine == [("press", "tab", False)]


def test_only_listed_shortcuts_are_pressed(screen, machine):
    assert act(move("shortcut", key="x"), screen, ITEMS) == "pressed command+x"
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
        assert key in macos.KEYCODES and key in windows.VK


def test_the_driver_is_off_unless_asked_for(monkeypatch):
    monkeypatch.delenv("CLICKER_DRIVER", raising=False)
    monkeypatch.delenv("CLICKER_DRIVER_STEPS", raising=False)
    assert config.driver_steps() == 0
    monkeypatch.setenv("CLICKER_DRIVER", "1")
    assert config.driver_steps() == config.DEFAULT_DRIVER_STEPS
    monkeypatch.setenv("CLICKER_DRIVER_STEPS", "5")
    assert config.driver_steps() == 5


@pytest.mark.parametrize(("name", "value"), [("CLICKER_DRIVER", "maybe"), ("CLICKER_DRIVER_STEPS", "lots")])
def test_a_bad_driver_setting_is_an_error(monkeypatch, name, value):
    monkeypatch.setenv("CLICKER_DRIVER", "true")
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        config.driver_steps()


def test_the_driver_model_is_the_answer_model_unless_named(monkeypatch):
    monkeypatch.delenv("CLICKER_DRIVER_MODEL", raising=False)
    assert config.driver_model() == config.answer_model()
    monkeypatch.setenv("CLICKER_DRIVER_MODEL", "some-model")
    assert config.driver_model() == "some-model"
