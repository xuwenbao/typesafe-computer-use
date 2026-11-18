"""Windows borrowings: UIA operations, a batch of driver actions, and the child-session protocol.

Nothing here opens a session, a pipe, or a UI Automation element on the machine running the tests.
"""

import json
import queue
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from typesafe_computer_use import cli, macos, windows, windows_session
from typesafe_computer_use.actions import click_item, press_offscreen
from typesafe_computer_use.ax_walk import AX_PRESS, AxAttrs, AxFacts, walk_actionable
from typesafe_computer_use.config import settings
from typesafe_computer_use.decide import kind_criteria
from typesafe_computer_use.driver import act, load_driver_record, take_over
from typesafe_computer_use.models import Abort, AxNode, ElementState, Item
from typesafe_computer_use.perception import to_ax_items
from typesafe_computer_use.platform_adapter import desktop
from typesafe_computer_use.runner import RunConfig, RunState
from typesafe_computer_use.uia_patterns import PATTERN_IDS
from typesafe_computer_use.waa import tree
from typesafe_computer_use.waa.desktop import WAADesktop, _operate_element
from typesafe_computer_use.waa.walk_vm import LiveElement, walk
from typesafe_computer_use.writer import (
    DRIVE_ACTIONS,
    DriveStep,
    WriterError,
    _drive_batch,
    _drive_properties,
    compose_drive_step,
)

PACKAGE = Path(__file__).resolve().parent.parent / "typesafe_computer_use"
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
    item = to_ax_items(found, 1.0)[0]
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
    assert click_item(item, live) == "toggled 'Sound' via accessibility"
    assert calls == [("press", "sound"), ("toggle", "sound")]

    calls.clear()

    def default_press(ref):
        calls.append(("press", ref))
        return True

    monkeypatch.setattr(desktop, "ax_press", default_press)
    listed = Item(0, "Report", 1.0, 0, 0, 20, 20, role="cell", source="ax")
    assert click_item(listed, live) == "pressed 'Report' via accessibility"
    assert calls == [("press", "sound")]

    calls.clear()
    monkeypatch.setattr(desktop, "ax_press", ax_press)
    monkeypatch.setattr(desktop, "ax_operate", lambda ref, operation, value=None: calls.append(operation) or False)
    assert click_item(item, live).startswith("clicked ")
    names = [call if isinstance(call, str) else call[0] for call in calls]
    assert names == ["press", "toggle", "select", "expand_toggle", "click"]
    assert "scroll_into_view" not in calls


def test_a_shift_click_stays_a_pointer_click(screen, monkeypatch):
    calls = []
    monkeypatch.setattr(desktop, "ax_operate", lambda *args, **kwargs: calls.append("operate") or False)
    monkeypatch.setattr(desktop, "click_at", lambda *args, **kwargs: calls.append(("click", kwargs.get("modifier"))))
    item = Item(0, "Archive", 1.0, 100, 100, 300, 140)
    assert "with range" in act(step("click", item="0", modifier="shift"), screen, [item])
    assert calls == [("click", "range")]


def test_offscreen_scrolls_and_an_onscreen_click_does_not(screen, monkeypatch):
    calls = []
    monkeypatch.setattr(desktop, "ax_operate", lambda ref, operation, value=None: calls.append(operation) or False)
    monkeypatch.setattr(desktop, "ax_press", lambda ref: calls.append("press") or True)
    node = AxNode("AXButton", "Save", 0, -40, 20, 20, True, ref="save")
    assert "pressed" in press_offscreen("0", replace(screen, offscreen=[node]))
    assert calls[0] == "scroll_into_view"

    calls.clear()
    monkeypatch.setattr(desktop, "ax_operate", lambda ref, operation, value=None: calls.append(operation) or False)
    monkeypatch.setattr(desktop, "click_at", lambda *args, **kwargs: calls.append("click"))
    item = Item(0, "Save", 1.0, 0, 0, 20, 20, source="ax")
    click_item(item, replace(screen, ax_refs={0: "save"}))
    assert "scroll_into_view" not in calls


def test_a_disabled_control_is_refused_and_an_unknown_one_is_clicked(screen, monkeypatch):
    clicks = []
    monkeypatch.setattr(desktop, "click_at", lambda *args, **kwargs: clicks.append("click"))
    monkeypatch.setattr(desktop, "ax_press", lambda ref: False)
    monkeypatch.setattr(desktop, "ax_operate", lambda *args, **kwargs: False)
    disabled = Item(0, "Save", 1.0, 0, 0, 20, 20, enabled=False)
    assert "disabled" in click_item(disabled, replace(screen, ax_refs={0: "save"}))
    assert clicks == []
    unknown = Item(0, "Save", 1.0, 0, 0, 20, 20, enabled=None)
    assert click_item(unknown, replace(screen, ax_refs={0: "save"})).startswith("clicked ")
    assert clicks == ["click"]


def test_driver_batch_defaults_to_three_and_rejects_zero(monkeypatch):
    monkeypatch.delenv("CLICKER_DRIVER_BATCH", raising=False)
    assert settings().driver_batch == 3
    monkeypatch.setenv("CLICKER_DRIVER_BATCH", "1")
    assert settings().driver_batch == 1
    monkeypatch.setenv("CLICKER_DRIVER_BATCH", "0")
    with pytest.raises(ValueError, match="at least 1"):
        settings()


def test_a_batch_parses_and_a_done_in_the_middle_or_an_overlong_list_does_not():
    ok = _drive_batch({"reason": "next", "actions": [{"action": "wait"}, {"action": "done", "achieved": True}]}, 3)
    assert [move.action for move in ok] == ["wait", "done"]
    with pytest.raises(WriterError, match="last"):
        _drive_batch({"reason": "next", "actions": [{"action": "done"}, {"action": "wait"}]}, 3)
    with pytest.raises(WriterError, match="batch"):
        _drive_batch({"reason": "next", "actions": [{"action": "wait"}] * 4}, 3)


def test_a_reply_without_an_action_name_quotes_the_entry_and_a_type_name_is_accepted():
    raw = {"item": "3", "label": "Save"}
    with pytest.raises(WriterError, match="action name") as caught:
        _drive_batch({"reason": "next", "actions": [raw]}, 3)
    assert json.dumps(raw) in str(caught.value)
    named = _drive_batch({"reason": "next", "actions": [{"type": "click", "item": "3"}]}, 3)
    assert [(move.action, move.item) for move in named] == [("click", "3")]
    by_name = _drive_batch({"reason": "next", "actions": [{"name": "give_up"}]}, 1)
    assert by_name[0].action == "give_up"


def test_the_takeover_schema_names_every_action(screen, monkeypatch):
    monkeypatch.setenv("CLICKER_WRITER_VISION", "false")
    seen = []

    def create(**kwargs):
        seen.append(kwargs["output_config"]["format"]["schema"])
        payload = {"reason": "next", "actions": [{"action": "wait"}]}
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(payload))])

    compose_drive_step(
        SimpleNamespace(messages=SimpleNamespace(create=create)),
        "set it",
        screen,
        [],
        [],
        "stuck",
        [],
        [],
        None,
        3,
        None,
        batch=3,
    )
    actions = seen[0]["properties"]["actions"]
    assert actions["minItems"] == 1 and actions["maxItems"] == 3
    assert actions["items"]["properties"]["action"]["enum"] == list(DRIVE_ACTIONS)
    assert actions["items"]["additionalProperties"] is False
    assert _drive_properties(1)["actions"]["maxItems"] == 1


def test_the_model_sees_controls_and_the_classifier_does_not_gain_set_value(screen, monkeypatch):
    monkeypatch.setenv("CLICKER_WRITER_VISION", "false")
    seen = []

    def create(**kwargs):
        seen.append(json.loads(kwargs["messages"][0]["content"][0]["text"]))
        payload = {"reason": "next", "actions": [{"action": "wait"}]}
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(payload))])

    item = Item(0, "Volume", 1.0, 0, 0, 20, 20, source="ax", controls=("range_value",), current="10")
    compose_drive_step(
        SimpleNamespace(messages=SimpleNamespace(create=create)), "set it", screen, [item], [], "stuck", [], [], None, 3, None
    )
    assert seen[0]["screen_items"][0]["controls"] == ["range_value"]
    assert seen[0]["screen_items"][0]["value"] == "10"
    assert "batch_size" in seen[0]
    assert "set_value" not in kind_criteria("Google Chrome", None)


def _run_batch(monkeypatch, tmp_path, screen, items, moves, element, shots):
    monkeypatch.setattr(desktop, "check_abort", lambda: None)
    monkeypatch.setattr(desktop, "sleep_watching", lambda seconds: None)
    monkeypatch.setattr(desktop, "ax_press", lambda ref: False)
    monkeypatch.setattr(desktop, "ax_operate", lambda *args, **kwargs: False)
    monkeypatch.setattr(desktop, "press", lambda key, command=False: None)
    clicks = []
    monkeypatch.setattr(desktop, "click_at", lambda *args, **kwargs: clicks.append(args))
    monkeypatch.setattr(desktop, "element_state", element)
    images = iter(shots)
    monkeypatch.setattr(desktop, "screenshot", lambda: next(images))
    planned = [moves, [step("give_up")]]

    def compose(*args, **kwargs):
        return planned.pop(0)

    monkeypatch.setattr("typesafe_computer_use.driver.compose_drive_step", compose)
    from typesafe_computer_use import runner

    def look(cfg, ctx, state, log):
        from typesafe_computer_use.runner import screen_moved

        state.view = (screen, items)
        return (screen, items) if screen_moved(state, screen, items, log) else None

    monkeypatch.setattr(runner, "look", look)
    state = RunState(outcome="nothing helps")
    cfg = RunConfig(goal="save", out=tmp_path, driver_steps=5, driver_batch=3)
    take_over(cfg, SimpleNamespace(writer=object()), state, lambda *args: None, 0)
    return state, clicks


def test_a_missing_control_stops_the_batch(monkeypatch, tmp_path, screen):
    item = Item(0, "Save", 1.0, 0, 0, 20, 20, source="ax")
    live = replace(screen, ax_refs={0: "save"})
    state, clicks = _run_batch(
        monkeypatch,
        tmp_path,
        live,
        [item],
        [step("click", item="0"), step("click", item="0")],
        lambda ref: ElementState(False, False, None),
        [],
    )
    assert clicks == []
    assert any("no longer available" in line for line in state.history)
    assert any("stopped the rest of the batch" in line for line in state.history)
    assert state.driver_actions == 1


def test_an_icon_cannot_be_the_second_action(monkeypatch, tmp_path, screen):
    icon = Item(1, "Bell", 1.0, 0, 0, 20, 20, source="icon")
    state, clicks = _run_batch(
        monkeypatch,
        tmp_path,
        screen,
        [icon],
        [step("key", key="tab"), step("click", item="1")],
        lambda ref: ElementState(True, True, None),
        [Image.new("RGB", screen.image.size, "white")],
    )
    assert clicks == []
    assert any("not an accessibility item" in line for line in state.history)
    assert state.driver_actions == 2


def test_an_unchanged_screen_drops_the_rest_and_a_wait_does_not(monkeypatch, tmp_path, screen):
    first = Item(0, "Archive", 1.0, 0, 0, 20, 20, source="ax")
    second = Item(1, "Tools", 1.0, 30, 0, 50, 20, source="ax")
    third = Item(2, "Extra", 1.0, 60, 0, 80, 20, source="icon")
    live = replace(screen, ax_refs={0: "a", 1: "b"})
    white = Image.new("RGB", screen.image.size, "white")
    state, clicks = _run_batch(
        monkeypatch,
        tmp_path,
        live,
        [first, second, third],
        [step("click", item="0"), step("click", item="1"), step("click", item="2")],
        lambda ref: ElementState(True, True, True),
        [white, white],
    )
    assert len(clicks) == 2
    assert state.driver_actions == 2
    assert 5 - state.driver_actions == 3
    assert not any("Extra" in line for line in state.history)
    assert any("stopped the rest of the batch" in line for line in state.history)
    record = load_driver_record(tmp_path / "step-000-driver.json")
    assert record["call"] == 1
    assert [entry["action"] for entry in record["actions"]] == ["click", "click"]

    state, clicks = _run_batch(
        monkeypatch,
        tmp_path,
        live,
        [first],
        [step("wait"), step("click", item="0")],
        lambda ref: ElementState(True, True, True),
        [],
    )
    assert len(clicks) == 1
    assert any("waited" in line for line in state.history)


def test_done_ends_the_batch_without_spending_the_budget(monkeypatch, tmp_path, screen):
    item = Item(0, "Archive", 1.0, 0, 0, 20, 20, source="ax")
    live = replace(screen, ax_refs={0: "a"})
    state, clicks = _run_batch(
        monkeypatch,
        tmp_path,
        live,
        [item],
        [step("click", item="0"), step("done", achieved=True)],
        lambda ref: ElementState(True, True, True),
        [Image.new("RGB", screen.image.size, "white")],
    )
    assert state.outcome == "driver done"
    assert state.driver_actions == 1
    assert len(clicks) == 1


def test_an_old_driver_record_reads_as_one_call_and_one_action(tmp_path):
    path = tmp_path / "step-003-driver.json"
    path.write_text(json.dumps({"step": 3, "action": "wait", "did": "waited"}), encoding="utf-8")
    record = load_driver_record(path)
    assert record["call"] == 1
    assert record["actions"] == [{"step": 3, "action": "wait", "did": "waited"}]


def test_set_value_sets_a_slider_and_refuses_a_range_and_a_password(screen, monkeypatch):
    calls = []
    monkeypatch.setattr(desktop, "check_abort", lambda: None)
    monkeypatch.setattr(desktop, "ax_operate", lambda ref, operation, value=None: calls.append((operation, value)) or True)
    slider = Item(0, "Volume", 1.0, 0, 0, 40, 20, source="ax", controls=("range_value",), low=0.0, high=100.0)
    live = replace(screen, ax_refs={0: "volume"})
    assert "RangeValue" in act(step("set_value", item="0", text="40"), live, [slider])
    assert calls == [("set_range", "40")]
    calls.clear()
    assert "outside" in act(step("set_value", item="0", text="120"), live, [slider])
    assert calls == []
    assert "password" in act(step("set_value", item="0", text="my password"), live, [slider])


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
    windows_text = (PACKAGE / "windows.py").read_text()
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
    assert order.index("launch") < order.index("relay") < order.index("host")
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
    # The desktop size is set to the frame's client area in open_child_window, not here.
    assert not hasattr(client, "DesktopWidth")


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
    assert (windows_session.FRAME_WIDTH, windows_session.FRAME_HEIGHT) == (800, 480)


def test_the_child_session_is_launched_acting():
    assert "--act" in windows_session.child_command(r"C:\Python\python.exe")
    assert cli.child_argv("save it", ["--act"]) == ["save it", "--act"]


def test_the_child_binds_the_input_desktop_before_it_starts():
    source = windows_session._LAUNCHER
    attached = source.index("attach_input_desktop()")
    started = source.index("from typesafe_computer_use.cli import main")
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
