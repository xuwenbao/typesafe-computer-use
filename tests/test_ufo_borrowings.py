"""Windows borrowings: UIA operations, one driver action per look, and the child-session protocol.

Nothing here opens a session, a pipe, or a UI Automation element on the machine running the tests.
"""

import json
import queue
import threading
from dataclasses import replace
from inspect import getsource
from pathlib import Path
from types import SimpleNamespace

import pytest

from yuanji_computer_use import cli
from yuanji_computer_use.actions import ActionPerformer, Context
from yuanji_computer_use.agents.driver.conversation import TOOLS, DriverConversation
from yuanji_computer_use.agents.driver.takeover import MoveExecutor, Takeover
from yuanji_computer_use.agents.policy.stages import UNCHANGED, DecideStrategy, PerceiveStrategy
from yuanji_computer_use.agents.session import Session
from yuanji_computer_use.llm.classifier import Classifier, Decision
from yuanji_computer_use.llm.writer import DRIVE_ACTIONS, DriveStep, Writer, WriterError
from yuanji_computer_use.models import Abort, AxNode, ElementState, Item, signature
from yuanji_computer_use.perception.pipeline import Perceiver
from yuanji_computer_use.platforms import macos, windows, windows_session
from yuanji_computer_use.platforms.adapter import desktop
from yuanji_computer_use.platforms.ax_walk import AX_PRESS, AxAttrs, AxFacts, walk_actionable
from yuanji_computer_use.platforms.uia_patterns import PATTERN_IDS
from yuanji_computer_use.runner import RunConfig
from yuanji_computer_use.waa import tree
from yuanji_computer_use.waa.desktop import WAADesktop, _operate_element
from yuanji_computer_use.waa.walk_vm import LiveElement, walk

PACKAGE = Path(__file__).resolve().parent.parent / "yuanji_computer_use"
# Captured before the machine guard replaces it, so the test can drive the real function
# against a patched attribute copy and still not touch Accessibility.
_MAC_ELEMENT_STATE = macos.element_state


def step(action: str, **fields) -> DriveStep:
    return DriveStep(reason="because", action=action, **fields)


def test_a_toggle_on_a_node_is_a_control_on_its_item():
    node = {"role": "AXCheckBox", "label": "Sound", "frame": (0.0, 0.0, 20.0, 20.0)}

    def attrs(raw):
        return AxAttrs(raw["role"], raw["label"], raw["frame"])

    def actions(raw):
        return AxFacts((AX_PRESS, "toggle"), enabled=True, checked=True)

    found, _, _ = walk_actionable(node, lambda raw: [], attrs, actions, 100, 100)
    item = Perceiver.to_ax_items(found, 1.0)[0]
    assert item.controls == ("toggle",)
    assert item.checked is True


def test_windows_reads_enabled_disabled_and_unknown(monkeypatch):
    monkeypatch.setattr(windows, "_ui_value", lambda element: None)
    monkeypatch.setattr(windows, "_ui_default_action", lambda element: "")
    monkeypatch.setattr(windows, "_range_bounds", lambda element: (None, None))
    monkeypatch.setattr(windows, "_writable", lambda element: None)

    class Live:
        IsEnabled = True
        IsOffscreen = False

        def GetPattern(self, pattern_id):
            if pattern_id == PATTERN_IDS["toggle"]:
                return SimpleNamespace(ToggleState=1)
            raise OSError("unsupported")

    facts = windows._ui_actions(Live())
    assert facts.enabled is True
    assert "toggle" in facts.names
    assert facts.checked is True

    class Disabled(Live):
        IsEnabled = False

    assert windows._ui_actions(Disabled()).enabled is False

    class Unreadable:
        @property
        def IsEnabled(self):
            raise OSError("gone")

        def GetPattern(self, pattern_id):
            raise OSError("gone")

    assert windows._ui_actions(Unreadable()).enabled is None
    assert windows.read_uia_state(Live()) == ElementState(True, True, True)
    assert windows.read_uia_state(Unreadable()) is None


def test_macos_reports_presence_and_visibility_but_not_enabled(monkeypatch):
    # Replace the module. The stand-in raises from __getattr__, which setattr cannot patch one name of.
    bridge = SimpleNamespace(AXUIElementCopyAttributeValue=lambda *args: (0, "AXButton"), kAXRoleAttribute="AXRole")
    monkeypatch.setattr(macos, "AS", bridge)
    assert _MAC_ELEMENT_STATE(object()) == ElementState(True, True, None)
    bridge.AXUIElementCopyAttributeValue = lambda *args: (-1, None)
    assert _MAC_ELEMENT_STATE(object()) is None


def test_click_order_uses_toggle_then_a_default_action_then_the_pointer(screen, monkeypatch):
    calls = []
    item = Item(0, "Sound", 1.0, 0, 0, 20, 20, role="checkbox", source="ax")
    live = replace(screen, ax_refs={0: "sound"})

    def ax_press(ref):
        calls.append(("press", ref))
        return False

    def ax_operate(ref, operation, value=None):
        calls.append((operation, ref))
        return operation == "toggle"

    monkeypatch.setattr(desktop, "ax_press", ax_press)
    monkeypatch.setattr(desktop, "ax_operate", ax_operate)
    monkeypatch.setattr(desktop, "click_at", lambda *args, **kwargs: calls.append(("click", args)))
    assert ActionPerformer().click_item(item, live) == "toggled 'Sound' via accessibility"
    assert calls == [("press", "sound"), ("toggle", "sound")]

    calls.clear()

    def default_press(ref):
        calls.append(("press", ref))
        return True

    monkeypatch.setattr(desktop, "ax_press", default_press)
    listed = Item(0, "Report", 1.0, 0, 0, 20, 20, role="cell", source="ax")
    assert ActionPerformer().click_item(listed, live) == "pressed 'Report' via accessibility"
    assert calls == [("press", "sound")]

    calls.clear()
    monkeypatch.setattr(desktop, "ax_press", ax_press)
    monkeypatch.setattr(desktop, "ax_operate", lambda ref, operation, value=None: calls.append(operation) or False)
    assert ActionPerformer().click_item(item, live).startswith("clicked ")
    names = [call if isinstance(call, str) else call[0] for call in calls]
    assert names == ["press", "toggle", "select", "expand_toggle", "click"]
    assert "scroll_into_view" not in calls


def test_a_selectable_file_is_selected_instead_of_opened(screen, monkeypatch):
    calls = []
    item = Item(0, "Doc01.docx", 1.0, 0, 0, 20, 20, role="cell", source="ax", controls=("invoke", "selection_item"))
    live = replace(screen, ax_refs={0: {"role": "AXCell", "name": "Doc01.docx"}})

    def ax_press(ref):
        pytest.fail("invoke would open the file")

    def ax_operate(ref, operation, value=None):
        calls.append(operation)
        return operation == "select"

    monkeypatch.setattr(desktop, "ax_press", ax_press)
    monkeypatch.setattr(desktop, "ax_operate", ax_operate)
    monkeypatch.setattr(desktop, "click_at", lambda *args, **kwargs: calls.append("click"))
    assert ActionPerformer().click_item(item, live) == "selected 'Doc01.docx' via accessibility"
    assert calls == ["toggle", "select"]


def test_adding_a_file_uses_add_to_selection_and_not_invoke(screen, monkeypatch):
    calls = []
    item = Item(0, "Doc02.docx", 1.0, 0, 0, 20, 20, role="cell", source="ax", controls=("invoke", "selection_item"))
    live = replace(screen, ax_refs={0: {"role": "AXCell", "name": "Doc02.docx"}})

    def ax_press(ref):
        pytest.fail("invoke would open the file")

    def ax_operate(ref, operation, value=None):
        calls.append(operation)
        return operation == "add_select"

    monkeypatch.setattr(desktop, "ax_press", ax_press)
    monkeypatch.setattr(desktop, "ax_operate", ax_operate)
    monkeypatch.setattr(desktop, "ax_focus", lambda ref: calls.append(("focus", ref)) or True)
    monkeypatch.setattr(desktop, "click_at", lambda *args, **kwargs: calls.append(("click", kwargs.get("modifier"))))
    assert ActionPerformer().add_item(item, live) == "clicked 'Doc02.docx' with add"
    assert calls == ["add_select", ("focus", live.ax_refs[0])]

    calls.clear()
    monkeypatch.setattr(desktop, "ax_operate", lambda ref, operation, value=None: calls.append(operation) or False)
    assert ActionPerformer().add_item(item, live) == "clicked 'Doc02.docx' with add"
    assert calls == ["add_select", ("click", "add")]
    assert "AddToSelection" in _operate_element("Doc02.docx", None, "add_select", None)


def test_a_cut_does_not_count_as_an_unchanged_screen(screen, monkeypatch, tmp_path):
    pressed = []
    monkeypatch.setattr(desktop, "press", lambda key, command=False: pressed.append((key, command)))
    decision = Decision(
        kind=SimpleNamespace(choice="shortcut", confidence=0.9, probabilities={"shortcut": 0.9}),
        item=None,
        site=SimpleNamespace(choice="none", confidence=1.0, probabilities={"none": 1.0}),
        key=SimpleNamespace(choice="x", confidence=0.8, probabilities={"x": 0.8}),
    )
    state = Session()
    ctx = Context(goal="cut the files", browser="Google Chrome", email=None, typesafe=None, writer=None, history=[])
    assert DecideStrategy().resolve(
        RunConfig(goal="cut the files", out=tmp_path, act=True), ctx, state, screen, [], decision, {}, lambda _m: None
    )
    assert pressed == [("x", True)]
    assert state.history == ["pressed command+x (to the clipboard; the screen need not change)"]
    assert state.quiet and not state.markable
    state.last = signature(screen, [])
    assert PerceiveStrategy().screen_moved(state, screen, [], lambda _m: None)
    assert state.idle == 0 and UNCHANGED not in state.history[0]


def test_a_radio_button_is_clicked_because_select_does_not_change_it(screen, monkeypatch):
    calls = []
    item = Item(0, "All", 1.0, 0, 0, 20, 20, role="AXRadioButton", source="ax")
    live = replace(screen, ax_refs={0: {"role": "AXRadioButton", "name": "All"}})

    def ax_press(ref):
        calls.append(("press", ref))
        return False

    def ax_operate(ref, operation, value=None):
        calls.append((operation, ref))
        return operation == "select"

    monkeypatch.setattr(desktop, "ax_press", ax_press)
    monkeypatch.setattr(desktop, "ax_operate", ax_operate)
    monkeypatch.setattr(desktop, "click_at", lambda *args, **kwargs: calls.append(("click", args)))
    assert ActionPerformer().click_item(item, live) == "clicked 'All'"
    assert [name for name, _ in calls] == ["press", "toggle", "expand_toggle", "click"]


def test_a_shift_click_stays_a_pointer_click(screen, monkeypatch):
    calls = []
    monkeypatch.setattr(desktop, "ax_operate", lambda *args, **kwargs: calls.append("operate") or False)
    monkeypatch.setattr(desktop, "click_at", lambda *args, **kwargs: calls.append(("click", kwargs.get("modifier"))))
    item = Item(0, "Archive", 1.0, 100, 100, 300, 140)
    assert "with range" in MoveExecutor(screen, [item]).act(step("click", item="0", target="Archive", modifier="shift"))
    assert calls == [("click", "range")]


def test_offscreen_scrolls_and_an_onscreen_click_does_not(screen, monkeypatch):
    calls = []
    monkeypatch.setattr(desktop, "ax_operate", lambda ref, operation, value=None: calls.append(operation) or False)
    monkeypatch.setattr(desktop, "ax_press", lambda ref: calls.append("press") or True)
    node = AxNode("AXButton", "Save", 0, -40, 20, 20, True, ref="save")
    assert "pressed" in ActionPerformer().press_offscreen("0", replace(screen, offscreen=[node]))
    assert calls[0] == "scroll_into_view"

    calls.clear()
    monkeypatch.setattr(desktop, "ax_operate", lambda ref, operation, value=None: calls.append(operation) or False)
    monkeypatch.setattr(desktop, "click_at", lambda *args, **kwargs: calls.append("click"))
    item = Item(0, "Save", 1.0, 0, 0, 20, 20, source="ax")
    ActionPerformer().click_item(item, replace(screen, ax_refs={0: "save"}))
    assert "scroll_into_view" not in calls


def test_a_disabled_control_is_clicked_with_a_note_and_an_unknown_one_without(screen, monkeypatch):
    clicks = []
    monkeypatch.setattr(desktop, "click_at", lambda *args, **kwargs: clicks.append("click"))
    monkeypatch.setattr(desktop, "ax_press", lambda ref: False)
    monkeypatch.setattr(desktop, "ax_operate", lambda *args, **kwargs: False)
    disabled = Item(0, "Save", 1.0, 0, 0, 20, 20, enabled=False)
    assert (
        ActionPerformer().click_item(disabled, replace(screen, ax_refs={0: "save"}))
        == "clicked 'Save' (note: the tree says 'Save' is disabled)"
    )
    unknown = Item(0, "Save", 1.0, 0, 0, 20, 20, enabled=None)
    assert ActionPerformer().click_item(unknown, replace(screen, ax_refs={0: "save"})) == "clicked 'Save'"
    assert clicks == ["click", "click"]


def test_a_reply_is_one_action_and_one_without_an_action_name_quotes_the_reply():
    move = Writer._drive_step({"reason": " next ", "action": "wait"})
    assert (move.reason, move.action) == ("next", "wait")
    raw = {"reason": "next", "item": "3", "label": "Save"}
    with pytest.raises(WriterError, match="action name") as caught:
        Writer._drive_step(raw)
    assert json.dumps(raw) in str(caught.value)
    named = Writer._drive_step({"reason": "next", "type": "click", "item": "3", "target": "  Save  "})
    assert (named.action, named.item, named.target) == ("click", "3", "Save")
    assert Writer._drive_step({"reason": "next", "name": "give_up"}).action == "give_up"
    with pytest.raises(WriterError, match="not a driver action"):
        Writer._drive_step({"reason": "next", "action": "hover"})


def test_the_takeover_tools_name_one_action_each():
    names = {tool.name for tool in TOOLS}
    assert set(DRIVE_ACTIONS) - {"type", "key"} <= names
    assert {"type_text", "press_key", "read_text", "count", "calculate", "done", "give_up"} <= names
    click = next(tool for tool in TOOLS if tool.name == "click")
    assert "reason" in click.params_json_schema["properties"]
    assert "target" in click.params_json_schema["properties"]
    assert "actions" not in click.params_json_schema["properties"]


def test_the_model_sees_controls_and_the_classifier_does_not_gain_set_value(screen):
    item = Item(0, "Volume", 1.0, 0, 0, 20, 20, source="ax", controls=("range_value",), current="10")
    seen = DriverConversation.screen_packet("set it", screen, [item], [], "stuck", [], [], None, 3)
    assert seen["screen_items"][0]["controls"] == ["range_value"]
    assert seen["screen_items"][0]["value"] == "10"
    assert "batch_size" not in seen and "change_course" not in seen
    assert "set_value" not in Classifier.kind_criteria("Google Chrome", None)


def _file_row(index: int, text: str, selected: bool = False) -> Item:
    return Item(
        index,
        text,
        1.0,
        index * 30,
        0,
        index * 30 + 20,
        20,
        role="cell",
        source="ax",
        controls=("selection_item", "invoke"),
        selected=selected,
    )


def _run_steps(monkeypatch, tmp_path, screen, items, moves, steps: int = 5):
    """Drive the takeover with one scripted action per question, counting each look it takes."""
    monkeypatch.setattr(desktop, "check_abort", lambda: None)
    monkeypatch.setattr(desktop, "sleep_watching", lambda seconds: None)
    monkeypatch.setattr(desktop, "ax_press", lambda ref: False)
    monkeypatch.setattr(desktop, "ax_operate", lambda *args, **kwargs: False)
    clicks = []
    monkeypatch.setattr(desktop, "click_at", lambda *args, **kwargs: clicks.append(args))
    planned = [_script_of(move) for move in (*moves, step("give_up"))]
    looks = []
    from scripted_driver import ScriptedModel

    from yuanji_computer_use.agents.driver import DriverAgent

    monkeypatch.setattr(DriverConversation, "model_for", staticmethod(lambda: ScriptedModel(planned)))

    def look(state):
        looks.append(len(state.history))
        state.view = (screen, items)
        return (screen, items) if PerceiveStrategy().screen_moved(state, screen, items, state.log) else None

    monkeypatch.setattr(DriverAgent, "look", staticmethod(look))
    state = Session(outcome="nothing helps")
    Takeover(
        RunConfig(goal="save", out=tmp_path, driver_steps=steps), SimpleNamespace(writer=object()), state, lambda *a: None, 0
    ).run()
    return state, clicks, looks


def test_each_action_is_chosen_from_a_screen_seen_after_the_one_before(monkeypatch, tmp_path, screen):
    archive = Item(0, "Archive", 1.0, 0, 0, 20, 20, source="ax")
    tools = Item(1, "Tools", 1.0, 30, 0, 50, 20, source="ax")
    state, clicks, looks = _run_steps(
        monkeypatch,
        tmp_path,
        screen,
        [archive, tools],
        [step("click", item="0", target="Archive"), step("click", item="1", target="Tools")],
    )
    assert len(clicks) == 2
    assert looks == [0, 1, 2]
    assert state.outcome == "driver gave up" and state.driver_actions == 2 and state.driver_calls == 3
    for index, name in ((0, "Archive"), (1, "Tools")):
        record = Takeover.load_driver_record(tmp_path / f"step-{index:03d}-driver.json")
        assert record["call"] == index + 1
        assert [entry["did"] for entry in record["actions"]] == [f"clicked {name!r}"]


def test_done_does_not_spend_the_budget(monkeypatch, tmp_path, screen):
    archive = Item(0, "Archive", 1.0, 0, 0, 20, 20, source="ax")
    state, clicks, _ = _run_steps(
        monkeypatch, tmp_path, screen, [archive], [step("click", item="0", target="Archive"), step("done", achieved=True)]
    )
    assert state.outcome == "driver done" and state.driver_done == "because"
    assert state.driver_actions == 1
    assert len(clicks) == 1


def test_the_drive_packet_names_the_selected_files(screen):
    rows = [
        _file_row(0, "Doc01.docx", selected=True),
        _file_row(1, "draft.txt"),
        Item(2, "文档", 1.0, 0, 40, 20, 60, role="tab", source="ax", controls=("selection_item",), selected=True),
    ]
    seen = DriverConversation.screen_packet("move the files", screen, rows, [], "stuck", [], [], None, 3)
    assert seen["selected_files"] == ["Doc01.docx"]
    assert seen["on_screen"]["file_rows"] == ["Doc01.docx", "draft.txt"]
    assert "文档" in seen["on_screen"]["names"]
    assert "文档" not in seen["on_screen"]["file_rows"]


def _script_of(move: DriveStep) -> dict:
    return {
        "action": move.action,
        "reason": move.reason,
        "item": move.item,
        "target": move.target,
        "text": move.text,
        "key": move.key,
        "modifier": move.modifier,
        "to": move.to,
        "dx": move.dx,
        "dy": move.dy,
        "achieved": move.achieved,
    }


def _drive_script(replies: list[dict]):
    from scripted_driver import ScriptedModel

    return ScriptedModel(list(replies))


def _drive_action(action: str, **fields) -> dict:
    body = {
        "action": action,
        "item": "",
        "target": "",
        "text": "",
        "key": "",
        "modifier": "",
        "to": "",
        "dx": "",
        "dy": "",
        "achieved": False,
    }
    body.update(fields)
    return body


def _ask_the_driver(monkeypatch, tmp_path, screen, items, script, steps: int):
    monkeypatch.setattr(desktop, "check_abort", lambda: None)
    monkeypatch.setattr(desktop, "sleep_watching", lambda seconds: None)
    pressed = []
    monkeypatch.setattr(desktop, "press", lambda key, command=False: pressed.append((key, command)))
    from yuanji_computer_use.agents.driver import DriverAgent

    def look(state):
        state.view = (screen, items)
        return (screen, items) if PerceiveStrategy().screen_moved(state, screen, items, state.log) else None

    monkeypatch.setattr(DriverAgent, "look", staticmethod(look))
    monkeypatch.setattr(DriverConversation, "model_for", staticmethod(lambda: script))
    state = Session(outcome="nothing helps")
    Takeover(
        RunConfig(goal="move the docx files into Archive", out=tmp_path, driver_steps=steps),
        SimpleNamespace(writer=object()),
        state,
        lambda *args: None,
        0,
    ).run()
    return state, pressed


def test_a_driver_that_calls_a_listed_folder_empty_is_asked_again_without_the_capture(monkeypatch, tmp_path, screen):
    rows = [_file_row(0, "Doc01.docx", selected=True), _file_row(1, "Doc02.docx", selected=True)]
    script = _drive_script(
        [
            {"reason": "The folder is empty.", **_drive_action("shortcut", key="x")},
            {"reason": "Both files are in Archive.", **_drive_action("done", achieved=True)},
        ]
    )

    state, pressed = _ask_the_driver(monkeypatch, tmp_path, screen, rows, script, 4)

    assert state.outcome == "driver done" and state.driver_calls == 2
    assert pressed == []
    assert not any("command+x" in line for line in state.history)
    assert len(script.inputs) == 2
    first = script.inputs[0][-1]["content"]
    assert any(part["type"] == "input_image" for part in first)
    second_parts = script.inputs[1][-1]["content"]
    assert [part["type"] for part in second_parts] == ["input_text"]
    second = json.loads(second_parts[0]["text"])
    assert second["on_screen"]["file_rows"] == ["Doc01.docx", "Doc02.docx"]
    assert second["listed_names_the_previous_reply_denied"] == ["Doc01.docx", "Doc02.docx"]
    record = Takeover.load_driver_record(tmp_path / "step-000-driver.json")
    assert record["call"] == 2 and record["reasked"] == ["Doc01.docx", "Doc02.docx"]
    assert record["actions"][0]["action"] == "done" and record["actions"][0]["did"] == "driver done"


def test_a_missing_name_that_is_not_listed_is_not_asked_of_the_driver_again(monkeypatch, tmp_path, screen):
    script = _drive_script([{"reason": "'Report.pdf' is not on this screen.", **_drive_action("key", key="escape")}])

    state, pressed = _ask_the_driver(monkeypatch, tmp_path, screen, [_file_row(0, "Doc01.docx")], script, 1)

    assert len(script.inputs) == 1
    assert pressed == [("escape", False)]
    assert state.driver_calls == 1


def test_an_old_driver_record_reads_as_one_call_and_one_action(tmp_path):
    path = tmp_path / "step-003-driver.json"
    path.write_text(json.dumps({"step": 3, "action": "wait", "did": "waited"}), encoding="utf-8")
    record = Takeover.load_driver_record(path)
    assert record["call"] == 1
    assert record["actions"] == [{"step": 3, "action": "wait", "did": "waited"}]


def test_set_value_sets_a_slider_and_refuses_a_range_and_a_password(screen, monkeypatch):
    calls = []
    monkeypatch.setattr(desktop, "check_abort", lambda: None)
    monkeypatch.setattr(desktop, "ax_operate", lambda ref, operation, value=None: calls.append((operation, value)) or True)
    slider = Item(0, "Volume", 1.0, 0, 0, 40, 20, source="ax", controls=("range_value",), low=0.0, high=100.0)
    live = replace(screen, ax_refs={0: "volume"})
    assert "RangeValue" in MoveExecutor(live, [slider]).act(step("set_value", item="0", target="Volume", text="40"))
    assert calls == [("set_range", "40")]
    calls.clear()
    assert "outside" in MoveExecutor(live, [slider]).act(step("set_value", item="0", target="Volume", text="120"))
    assert calls == []
    assert "password" in MoveExecutor(live, [slider]).act(step("set_value", item="0", target="Volume", text="my password"))


def test_pattern_ids_are_shared_and_not_copied_into_either_walker():
    assert PATTERN_IDS == {
        "invoke": 10000,
        "toggle": 10015,
        "expand_collapse": 10005,
        "selection_item": 10010,
        "scroll_item": 10017,
        "range_value": 10003,
        "value": 10002,
    }
    windows_text = (PACKAGE / "platforms" / "windows.py").read_text()
    walk_text = (PACKAGE / "waa" / "walk_vm.py").read_text()
    for number in PATTERN_IDS.values():
        assert str(number) not in windows_text
        assert str(number) not in walk_text


def test_the_vm_reports_a_checkbox_a_disabled_control_and_a_missing_bit():
    class Raw:
        def __init__(self, name, control_type, enabled=None, patterns=None):
            self.CurrentName = name
            self.CurrentControlType = control_type
            self.CurrentBoundingRectangle = SimpleNamespace(left=0, top=0, right=20, bottom=20)
            self.CurrentHasKeyboardFocus = False
            self._enabled = enabled
            self._patterns = patterns or {}

        @property
        def CurrentIsEnabled(self):
            if self._enabled is None:
                raise OSError("missing")
            return self._enabled

        def GetCurrentPattern(self, pattern_id):
            if pattern_id not in self._patterns:
                raise OSError("unsupported")
            return self._patterns[pattern_id]

        def GetRuntimeId(self):
            return (1, id(self))

    class Walker:
        def __init__(self, kids):
            self.kids = kids

        def GetFirstChildElement(self, raw):
            row = self.kids.get(id(raw), [])
            return row[0] if row else None

        def GetNextSiblingElement(self, raw):
            for row in self.kids.values():
                if raw in row:
                    index = row.index(raw)
                    return row[index + 1] if index + 1 < len(row) else None
            return None

    box = SimpleNamespace(CurrentToggleState=1)
    window = Raw("Edge", 50032, enabled=True)
    box_raw = Raw("Sound", 50002, enabled=False, patterns={PATTERN_IDS["toggle"]: box})
    missing = Raw("Gone", 50000, enabled=None)
    got = walk(LiveElement(window, Walker({id(window): [box_raw, missing]})), clock=lambda: 0)
    by_name = {node["name"]: node for node in got["nodes"]}
    assert by_name["Sound"]["enabled"] is False
    assert PATTERN_IDS["toggle"] in by_name["Sound"]["patterns"]
    assert by_name["Sound"]["checked"] is True
    assert by_name["Gone"]["enabled"] is None


def test_a_fixture_tree_keeps_enabled_and_patterns():
    body = json.loads((Path(__file__).parent / "fixtures" / "waa" / "edge-address.json").read_text())
    body["nodes"][0]["enabled"] = False
    body["nodes"][0]["patterns"] = [PATTERN_IDS["toggle"]]
    reading = tree.read(body, 1440, 900)
    save = next(node for node in reading.found if node.label == "保存")
    assert save.enabled is False
    assert "toggle" in save.patterns


def test_vm_operate_waits_for_the_tree_and_quotes_the_shared_pattern_id():
    from io import BytesIO

    from conftest import busy_page

    out = BytesIO()
    busy_page((200, 100)).save(out, format="PNG")
    body = {"nodes": [{"role": "AXCheckBox", "name": "Sound", "rect": [0, 0, 20, 20], "pressable": True, "children": []}]}
    order = []

    class Slow:
        def result(self):
            order.append("tree")
            return tree.Fetch(body, None, 0.01, 1, False)

    def next_obs(actions):
        order.append("actions")
        return {"screenshot": out.getvalue()}

    desk = WAADesktop({"screenshot": out.getvalue()}, next_obs, start_tree=lambda: Slow())
    ref = {"name": "Sound", "role": "AXCheckBox", "patterns": [PATTERN_IDS["toggle"]]}
    assert desk.ax_operate(ref, "toggle") is True
    assert order == ["tree"]
    assert str(PATTERN_IDS["toggle"]) in desk.actions[0]
    assert "pip " not in desk.actions[0]
    code = _operate_element("Sound", None, "toggle", None)
    assert str(PATTERN_IDS["toggle"]) in code

    desk.click_at((1, 1))
    state = desk.element_state(ref)
    assert order[:2] == ["tree", "actions"]
    assert state is not None and state.present


def test_session_messages_and_a_cancel_aborts(monkeypatch):
    for kind in windows_session.KINDS:
        assert windows_session.decode(windows_session.encode(kind, text="x"))["type"] == kind
    facts = windows_session.SessionFacts("Core", True, False, False)
    assert windows_session.rejection(facts) == "Windows Home cannot host a child session"
    assert windows_session.rejection(windows_session.SessionFacts("Professional", True, False, True)) == (
        "a child session is already running"
    )
    assert windows_session.rejection(windows_session.SessionFacts("Enterprise", False, False, False)) == (
        "remote desktop cannot connect to localhost"
    )
    assert windows_session.rejection(windows_session.SessionFacts("Education", True, False, False)) is None
    assert windows_session.should_refuse_session(True, True) is True
    assert windows_session.should_refuse_session(True, False) is False

    child_to_parent = queue.Queue()
    parent_to_child = queue.Queue()
    child = windows_session.SessionChannel(child_to_parent.put, parent_to_child.get)

    def answer():
        windows_session.decode(child_to_parent.get())
        parent_to_child.put(windows_session.encode("answer", text="42"))

    threading.Thread(target=answer).start()
    assert child.ask("which?") == "42"

    def cancel():
        child_to_parent.get()
        parent_to_child.put(windows_session.encode("cancel"))

    windows_session.reset_cancel()
    threading.Thread(target=cancel).start()
    with pytest.raises(Abort):
        child.ask("which?")
    monkeypatch.setenv("JEV_SESSION", "1")
    with pytest.raises(Abort, match="cancelled"):
        windows.check_abort()
    windows_session.reset_cancel()


def test_the_picture_in_picture_opens_before_connect_and_launch_waits(monkeypatch):
    order = []
    client = object()
    _patch_session(monkeypatch, order, client)

    def wait(pump, deadline, got):
        assert got is client
        order.append("wait")
        pump()

    monkeypatch.setattr(windows_session, "wait_for_child_explorer", wait)

    def host(relay, hwnd, pipe, pid):
        relay.join(timeout=2)
        order.append("host")
        return "done"

    monkeypatch.setattr(windows_session, "_host_until", host)
    windows_session.run_controller(["open notepad"])
    assert order.index("window") < order.index("connect") < order.index("wait") < order.index("launch")
    assert order.index("launch") < order.index("mask") < order.index("relay") < order.index("host")
    assert order.index("pump") < order.index("launch")
    assert order[-1] == "close"


def test_a_failed_window_does_not_launch_into_the_session(monkeypatch):
    order = []
    _patch_session(monkeypatch, order, object())

    def connect(got):
        order.append("connect")
        raise OSError("no control")

    monkeypatch.setattr(windows_session, "connect_child_session", connect)
    with pytest.raises(SystemExit, match="did not start"):
        windows_session.run_controller(["open notepad"])
    assert "launch" not in order
    assert "close" in order


def test_an_action_is_shown_under_the_mask_title():
    assert windows_session.mask_line("driver: pressed run") == "pressed run"
    assert windows_session.mask_line("clicked 'Archive'") == "clicked 'Archive'"
    windows_session._bind_action_report(None)
    windows_session.report_action("clicked 'Archive'")
    sent = []
    windows_session._bind_action_report(sent.append)
    windows_session.report_action("clicked 'Archive'")
    assert sent == ["clicked 'Archive'"]
    windows_session._bind_action_report(None)
    messages = iter(
        [
            windows_session.encode("log", text="driver: pressed run"),
            windows_session.encode("done", text="ok"),
        ]
    )
    windows_session._relay(
        object(),
        "goal",
        connect=lambda pipe: None,
        write=lambda pipe, line: None,
        read=lambda pipe: next(messages),
    )
    assert windows_session._mask_action == "pressed run"
    windows_session._note_mask_action("")
    windows_session._bind_action_report(None)


def test_a_pending_read_does_not_take_the_blocking_write(monkeypatch):
    """The child keeps a read outstanding for cancel. That read must not block the log write."""
    windows_session._OVERLAPPED.add(7)
    calls = []
    monkeypatch.setattr(windows_session, "_overlapped_write", lambda pipe, data: calls.append(("overlapped", pipe, data)))

    def blocking(pipe, data):
        calls.append(("blocking", pipe, data))

    monkeypatch.setattr(windows_session, "_blocking_write", blocking)
    try:
        windows_session._write_line(7, "host: launched notepad.exe")
        windows_session._write_line(object(), "parent")
    finally:
        windows_session._OVERLAPPED.discard(7)
    assert calls[0] == ("overlapped", 7, b"host: launched notepad.exe")
    kind, pipe, data = calls[1]
    assert (kind, data) == ("blocking", b"parent")
    assert pipe != 7


def test_the_child_leaves_once_it_has_reported(monkeypatch):
    seen = []

    def finish(outcome, folder):
        seen.append((outcome, folder))

    def stop(code):
        seen.append(code)
        raise SystemExit(code)

    monkeypatch.setattr(windows_session.os, "_exit", stop)
    with pytest.raises(SystemExit):
        windows_session.end_child(finish, "driver stalled", "runs/x")
    assert seen == [("driver stalled", "runs/x"), 0]
    seen.clear()
    with pytest.raises(SystemExit):
        windows_session.end_child(finish, "aborted (Ctrl-C)", "runs/x")
    assert seen == [("aborted (Ctrl-C)", "runs/x"), 130]


def test_the_relay_connects_before_it_writes_and_a_closed_window_cancels():
    calls = []

    def connect(pipe):
        calls.append("connect")

    def write(pipe, line):
        calls.append(windows_session.decode(line)["type"])

    windows_session._relay(
        object(),
        "goal",
        connect=connect,
        write=write,
        read=lambda pipe: windows_session.encode("done", text="ok"),
    )
    assert calls == ["connect", "task"]
    sent = []
    windows_session._notify_closed(object(), write=lambda pipe, line: sent.append(windows_session.decode(line)["type"]))
    assert sent == ["cancel"]


def test_waiting_reports_a_disconnect_and_a_timeout_differently():
    assert windows_session.child_wait_failure(1, False, False, 90, False) is None
    gone = windows_session.child_wait_failure(1, False, False, 90, True)
    late = windows_session.child_wait_failure(90, True, False, 90, True)
    assert gone != late
    assert "disconnected" in gone
    assert "logging on" in late
    assert windows_session.child_wait_failure(90, False, True, 90, False) is None
    assert windows_session.host_stop(False, True, True) is None
    assert windows_session.host_stop(True, True, True) == "done"
    assert windows_session.host_stop(False, False, True) == "closed"
    assert windows_session.host_stop(False, True, False) == "exited"


def test_a_configured_password_is_remembered_and_the_desktop_stays_small(monkeypatch, clean_env):
    remembered = []
    monkeypatch.setattr(windows_session, "_remember_credentials", lambda user, password: remembered.append(user))
    clean_env.setenv("CLICKER_SESSION_USERNAME", "Administrator")
    clean_env.setenv("CLICKER_SESSION_PASSWORD", "secret-value")
    client = _connection_client()
    windows_session._prepare_connection(client)
    assert remembered == ["Administrator"]
    assert client.UserName == "Administrator"
    assert client.ClearTextPassword == "secret-value"
    assert client.FullScreen is False
    assert client.SmartSizing is True
    assert client.ContainerHandledFullScreen is True
    assert (client.DesktopWidth, client.DesktopHeight) == (1280, 800)


def test_without_a_password_the_control_is_not_given_a_user(monkeypatch, clean_env, tmp_path):
    remembered = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(windows_session, "_remember_credentials", lambda user, password: remembered.append(user))
    clean_env.delenv("CLICKER_SESSION_PASSWORD", raising=False)
    clean_env.delenv("CLICKER_SESSION_USERNAME", raising=False)
    client = _connection_client()
    windows_session._prepare_connection(client)
    assert remembered == []
    assert not hasattr(client, "UserName")
    assert client.FullScreen is False
    assert client.SmartSizing is True


def _connection_client():
    class Settings:
        def __init__(self):
            self.AdvancedSettings9 = self

    return Settings()


def test_the_picture_window_is_the_small_frame():
    assert (windows_session.FRAME_WIDTH, windows_session.FRAME_HEIGHT) == (1280, 800)
    assert (windows_session.DESKTOP_WIDTH, windows_session.DESKTOP_HEIGHT) == (1280, 800)
    assert windows_session.desktop_oversized(1920, 1080)
    assert not windows_session.desktop_oversized(1280, 800)
    assert "WS_EX_TOPMOST" not in getsource(windows_session._create_frame)
    assert "SetWindowPos" not in getsource(windows_session._reveal_hosted_windows)


def test_the_run_covers_the_window_while_the_agent_works():
    assert windows_session.MASK_TEXT == "智能体执行中"
    source = getsource(windows_session.run_controller)
    assert source.index("_show_execution_mask") < source.index("_host_until")


def test_the_child_session_is_launched_acting():
    assert "--act" in windows_session.child_command(r"C:\Python\python.exe")
    assert cli.child_argv("save it", ["--act"]) == ["save it", "--act"]


def test_the_child_binds_the_input_desktop_before_it_starts():
    source = windows_session._LAUNCHER
    attached = source.index("attach_input_desktop()")
    started = source.index("from yuanji_computer_use.cli import main")
    assert attached < started < source.index("\n    main()")


def test_attaching_the_input_desktop_is_refused_off_the_machine():
    with pytest.raises(RuntimeError, match="real machine"):
        windows_session.attach_input_desktop()


def _patch_session(monkeypatch, order, client):
    monkeypatch.setattr(
        windows_session, "collect_facts", lambda: windows_session.SessionFacts("Professional", True, False, False)
    )
    monkeypatch.setattr(windows_session, "create_session_pipe", lambda name: object())
    monkeypatch.setattr(windows_session, "enable_child_session", lambda: order.append("enable"))
    monkeypatch.setattr(windows_session, "open_child_window", lambda: order.append("window") or (1, client))
    monkeypatch.setattr(windows_session, "connect_child_session", lambda got: order.append("connect") if got is client else None)
    monkeypatch.setattr(windows_session, "wait_for_child_explorer", lambda pump, deadline, got: order.append("wait"))
    monkeypatch.setattr(windows_session, "launch_in_session", lambda name: order.append("launch") or 0)
    monkeypatch.setattr(windows_session, "_show_execution_mask", lambda hwnd: order.append("mask"))
    monkeypatch.setattr(windows_session, "_hide_execution_mask", lambda: None)
    monkeypatch.setattr(windows_session, "_relay", lambda pipe, goal: order.append("relay"))
    monkeypatch.setattr(windows_session, "_host_until", lambda relay, hwnd, pipe, pid: "done")
    monkeypatch.setattr(windows_session, "_close_window", lambda hwnd, got: order.append("close"))
    monkeypatch.setattr(windows_session, "_pump_messages", lambda: order.append("pump"))
    monkeypatch.setattr(windows_session, "_window_open", lambda hwnd: True)


def test_clicker_session_on_this_platform_does_not_start_a_process():
    if __import__("sys").platform == "win32":
        pytest.skip("the refusal is the non-Windows path")
    with pytest.raises(SystemExit, match="only runs on Windows"):
        cli.main(["session", "save the file"])
