"""The driver's checks: every action the model names is taken only when the screen allows it."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from yuanji_computer_use import config
from yuanji_computer_use.actions import Context, perform
from yuanji_computer_use.agents.driver.takeover import (
    BANNED,
    DONE_BEFORE,
    KEYS,
    MAX_REVISITS,
    MAX_SAME_ACTION,
    SHORTCUTS,
    _advance_same_action,
    _signature,
    _take,
    act,
)
from yuanji_computer_use.agents.policy.stages import UNCHANGED, screen_moved
from yuanji_computer_use.agents.session import Session
from yuanji_computer_use.llm.classifier import Decision
from yuanji_computer_use.llm.writer import DriveStep, WriterError, _drive_step, _last_action, checked
from yuanji_computer_use.models import AxNode, Field, Item, signature
from yuanji_computer_use.platforms import macos, windows
from yuanji_computer_use.platforms.adapter import desktop


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


def file_row(index: int, text: str, selected: bool = False) -> Item:
    return Item(index, text, 1.0, 0, 0, 20, 20, role="cell", controls=("selection_item",), selected=selected)


def move(action: str, **fields) -> DriveStep:
    return DriveStep(reason="test", action=action, **fields)


def text_field(label: str = "Name") -> Field:
    return Field(role="AXTextField", label=label, placeholder="", value="", x=10, y=20, w=200, h=30)


def test_a_click_lands_on_the_item_the_model_numbered(screen, machine):
    assert act(move("click", item="0", target="Archive"), screen, ITEMS) == "clicked 'Archive'"
    assert machine == [("click", (100.0, 60.0), "left", 1, None)]


def test_a_number_that_is_not_on_the_screen_clicks_nothing(screen, machine):
    assert act(move("click", item="7"), screen, ITEMS) == "click refused: there is no item '7' on this screen"
    assert machine == []


def test_typing_with_no_focused_text_field_still_types_and_says_so(screen, machine):
    line = act(move("type", text="Kyoto"), screen, ITEMS)
    assert line == "typed 'Kyoto' (keystrokes, not read back) (note: no text field was seen focused)"
    assert machine == [("type", "Kyoto")]


def test_a_credential_is_not_typed_without_a_focused_field_either(screen, machine):
    assert act(move("type", text="my api key is abc"), screen, ITEMS) == "type refused: the text looks like a credential"
    assert machine == []


def test_typing_into_an_item_clicks_it_and_sends_keystrokes(screen, machine):
    line = act(move("type", item="0", target="Archive", text="30"), screen, ITEMS)
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
    assert act(move("double_click", item="0", target="Archive"), screen, ITEMS) == "double-clicked 'Archive'"
    assert act(move("right_click", item="0", target="Archive"), screen, ITEMS) == "right-clicked 'Archive'"
    assert act(move("click", item="0", target="Archive", modifier="add"), screen, ITEMS) == "clicked 'Archive' with add"
    assert machine == [
        ("click", (100.0, 60.0), "left", 2, None),
        ("click", (100.0, 60.0), "right", 1, None),
        ("click", (100.0, 60.0), "left", 1, "add"),
    ]


def test_a_click_outside_an_open_menu_still_lands(screen, machine):
    button = Item(1, "新建", 1.0, 40, 0, 80, 20, role="button", source="ax")
    live = replace(screen, field=Field("AXMenuBarItem", "文件夹", "", "", 0, 0, 20, 20))
    assert act(move("click", item="1", target="新建"), live, [button]) == "clicked '新建'"
    assert machine == [("click", (30.0, 5.0), "left", 1, None)]


def test_a_different_menu_item_is_clicked_while_one_is_focused(screen, machine):
    other = Item(2, "DOCX 文档", 1.0, 0, 40, 40, 60, role="menu", source="ax")
    live = replace(screen, field=Field("AXMenuBarItem", "文件夹", "", "", 0, 0, 20, 20))
    assert act(move("click", item="2", target="DOCX 文档"), live, [other]) == "clicked 'DOCX 文档'"
    assert machine == [("click", (10.0, 25.0), "left", 1, None)]


def _press_enter(screen):
    decision = Decision(kind=SimpleNamespace(choice="press_enter"), item=None, site=SimpleNamespace(choice=""))
    ctx = Context(goal="move the files", browser="", email=None, typesafe=None, writer=None, history=[])
    return perform(decision, screen, [], ctx)


def test_return_on_a_file_row_is_pressed_with_a_note(screen, machine):
    live = replace(screen, field=Field("AXRow", "Doc02.docx", "", "", 0, 0, 20, 20))
    line = act(move("key", key="return"), live, [file_row(0, "Doc02.docx", True)])
    assert line == "pressed return (note: Return on the row opens 'Doc02.docx')"
    assert _press_enter(live) == "pressed Return (note: Return on the row opens 'Doc02.docx')"
    assert machine == [("press", "return", False)] * 2


def test_return_on_a_menu_a_button_and_a_text_field_is_pressed(screen, machine):
    menu = replace(screen, field=Field("AXMenuBarItem", "文件夹", "", "", 0, 0, 20, 20))
    button = replace(screen, field=Field("AXButton", "新建", "", "", 0, 0, 20, 20))
    field = replace(screen, field=Field("AXTextField", "Name", "", "", 0, 0, 20, 20))
    assert act(move("key", key="return"), menu, []) == "pressed return"
    assert act(move("key", key="return"), button, []) == "pressed return"
    assert act(move("key", key="return"), field, []) == "pressed return"
    assert _press_enter(menu) == "pressed Return"
    assert _press_enter(button) == "pressed Return"
    assert _press_enter(field) == "pressed Return"
    assert machine == [("press", "return", False)] * 6


def test_a_menu_item_can_be_clicked_while_its_menu_is_open(screen, machine):
    item = Item(0, "文件夹", 1.0, 0, 0, 20, 20, role="menu", source="ax")
    live = replace(screen, field=Field("AXMenuBarItem", "文件夹", "", "", 0, 0, 20, 20))
    line = act(move("click", item="0", target="文件夹"), live, [item])
    assert "refused" not in line
    assert machine[0][0] == "click"


def test_a_click_modifier_the_model_invented_clicks_nothing(screen, machine):
    assert act(move("click", item="0", target="Archive", modifier="alt"), screen, ITEMS).startswith("click refused:")
    assert machine == []


def test_a_plain_click_that_replaces_two_selected_files_lands_with_a_note(screen, machine, monkeypatch):
    rows = [file_row(0, "Doc01.docx", True), file_row(1, "Doc02.docx", True), file_row(2, "Archive")]
    monkeypatch.setattr(desktop, "ax_operate", lambda *args, **kwargs: False)
    line = act(move("click", item="2", target="Archive"), screen, rows)
    assert line == (
        "clicked 'Archive' (note: Doc01.docx and Doc02.docx were selected; this plain click on 'Archive' replaces them)"
    )
    assert [entry[0] for entry in machine] == ["click"]


def test_a_disabled_control_is_clicked_with_a_note(screen, machine):
    off = [replace(ITEMS[0], enabled=False)]
    assert act(move("click", item="0", target="Archive"), screen, off) == (
        "clicked 'Archive' (note: the tree says 'Archive' is disabled)"
    )
    assert act(move("click", item="0", target="Archive", modifier="add"), screen, off) == (
        "clicked 'Archive' with add (note: the tree says 'Archive' is disabled)"
    )
    assert [entry[0] for entry in machine] == ["click", "click"]


def test_a_click_on_one_of_the_selected_files_narrows_the_selection(screen, machine, monkeypatch):
    rows = [file_row(0, "Doc01.docx", True), file_row(1, "Doc02.docx", True)]
    live = replace(screen, ax_refs={0: "doc"})
    calls = []
    monkeypatch.setattr(
        desktop, "ax_operate", lambda ref, operation, value=None: calls.append(operation) or operation == "select"
    )
    assert act(move("click", item="0", target="Doc01.docx"), live, rows) == "selected 'Doc01.docx' via accessibility"
    assert "select" in calls
    assert machine == []


def test_a_single_selected_file_can_be_replaced_by_another(screen, machine, monkeypatch):
    rows = [file_row(0, "Doc01.docx", True), file_row(1, "Doc02.docx")]
    live = replace(screen, ax_refs={1: "doc"})
    calls = []
    monkeypatch.setattr(
        desktop, "ax_operate", lambda ref, operation, value=None: calls.append(operation) or operation == "select"
    )
    assert act(move("click", item="1", target="Doc02.docx"), live, rows) == "selected 'Doc02.docx' via accessibility"
    assert "select" in calls
    assert machine == []


def test_a_cut_with_no_file_selected_presses_with_a_note(screen, machine):
    rows = [file_row(0, "Doc01.docx"), file_row(1, "Doc02.docx")]
    line = act(move("shortcut", key="x"), screen, rows)
    assert line == "pressed command+x (to the clipboard; the screen need not change) (note: no file was selected)"
    assert machine == [("press", "x", True)]


def test_a_cut_goes_ahead_when_the_selection_cannot_be_read(screen, machine):
    rows = [replace(file_row(0, "Doc01.docx"), selected=None), replace(file_row(1, "Doc02.docx"), selected=None)]
    line = act(move("shortcut", key="x"), screen, rows)
    assert line == "pressed command+x (to the clipboard; the screen need not change)"
    assert machine == [("press", "x", True)]


def test_paste_and_select_all_carry_no_note_for_an_empty_selection(screen, machine):
    rows = [file_row(0, "Doc01.docx"), file_row(1, "Doc02.docx")]
    assert act(move("shortcut", key="v"), screen, rows).startswith("pressed command+v")
    assert act(move("shortcut", key="a"), screen, rows).startswith("pressed command+a")
    assert machine == [("press", "v", True), ("press", "a", True)]


def test_a_cut_names_the_selected_files(screen, machine):
    rows = [file_row(0, "Doc01.docx", True), file_row(1, "Doc02.docx", True)]
    line = act(move("shortcut", key="x"), screen, rows)
    assert line == "pressed command+x (Doc01.docx, Doc02.docx; to the clipboard; the screen need not change)"
    assert machine == [("press", "x", True)]


def test_adding_a_file_uses_add_to_selection_and_focuses_it(screen, machine, monkeypatch):
    item = Item(0, "Doc02.docx", 1.0, 0, 0, 20, 20, controls=("selection_item",))
    live = replace(screen, ax_refs={0: "doc"})
    calls = []
    monkeypatch.setattr(
        desktop, "ax_operate", lambda ref, operation, value=None: calls.append(operation) or operation == "add_select"
    )
    monkeypatch.setattr(desktop, "ax_focus", lambda ref: calls.append(("focus", ref)) or True)
    assert act(move("click", item="0", target="Doc02.docx", modifier="add"), live, [item]) == "clicked 'Doc02.docx' with add"
    assert calls == ["add_select", ("focus", "doc")]
    assert machine == []


def test_control_and_shift_are_the_selection_modifiers(screen, machine):
    assert act(move("click", item="0", target="Archive", modifier="control"), screen, ITEMS) == "clicked 'Archive' with add"
    assert act(move("click", item="0", target="Archive", modifier="shift"), screen, ITEMS) == "clicked 'Archive' with range"
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
    assert act(move("drag", item="0", target="Oval", to="1"), screen, CANVAS) == "dragged 'Oval' to 'Canvas'"
    assert act(move("drag", item="0", target="Oval", dx="80", dy="-40"), screen, CANVAS) == "dragged 'Oval' to '80,-40'"
    assert machine == [("drag", (70.0, 60.0), (300.0, 275.0)), ("drag", (70.0, 60.0), (110.0, 40.0))]
    assert _signature(move("drag", item="0", to="1"), CANVAS) == "drag:Oval->Canvas"


def test_a_drag_that_leaves_the_screen_or_names_nothing_is_refused(screen, machine):
    edge = [Item(0, "Edge", 1.0, 1900.0, 100.0, 1980.0, 140.0)]
    assert act(move("drag", item="0", target="Edge", dx="100"), screen, edge).startswith("drag refused: the drag leaves")
    assert act(move("drag", item="0", target="Oval", dx="601"), screen, CANVAS).startswith("drag refused: a drag stays within")
    assert act(move("drag", item="0", target="Oval"), screen, CANVAS).startswith("drag refused: name the item")
    assert act(move("drag", item="9", to="1"), screen, CANVAS).startswith("drag refused: there is no item")
    assert machine == []


def test_only_listed_keys_are_pressed(screen, machine):
    assert act(move("key", key="tab"), screen, ITEMS) == "pressed tab"
    assert act(move("key", key="start"), screen, ITEMS) == "pressed start"
    assert act(move("key", key="f4"), screen, ITEMS).startswith("key refused: 'f4'")
    assert machine == [("press", "tab", False), ("press", "start", False)]


def test_a_cut_names_the_files_the_clipboard_gained(screen, machine, monkeypatch):
    held = {"files": ()}

    def press(key, command=False):
        machine.append(("press", key, command))
        held["files"] = ("Doc01.docx", "Doc02.docx")

    monkeypatch.setattr(desktop, "press", press)
    monkeypatch.setattr(desktop, "clipboard_files", lambda: held["files"])
    assert (
        act(move("shortcut", key="x"), screen, ITEMS)
        == "pressed command+x (to the clipboard: Doc01.docx, Doc02.docx; the screen need not change)"
    )


def test_a_cut_that_puts_nothing_on_the_clipboard_says_so(screen, machine, monkeypatch):
    monkeypatch.setattr(desktop, "clipboard_files", lambda: ())
    assert act(move("shortcut", key="x"), screen, ITEMS) == "pressed command+x (the clipboard has no files)"
    assert machine == [("press", "x", True)]


def _paste(screen, items):
    decision = Decision(
        kind=SimpleNamespace(choice="shortcut"),
        item=None,
        site=SimpleNamespace(choice=""),
        key=SimpleNamespace(choice="v"),
    )
    ctx = Context(goal="move the files", browser="", email=None, typesafe=None, writer=None, history=[])
    return perform(decision, screen, items, ctx)


def test_a_paste_of_files_already_in_this_folder_is_pressed_with_a_note(screen, machine, monkeypatch):
    monkeypatch.setattr(desktop, "clipboard_files", lambda: ("Doc01.docx", "Doc02.docx"))
    rows = [file_row(0, "Doc01.docx"), file_row(1, "Doc02.docx")]
    noted = "pressed command+v (note: Doc01.docx, Doc02.docx were already in this folder)"
    assert act(move("shortcut", key="v"), screen, rows) == noted
    assert _paste(screen, rows) == noted
    assert machine == [("press", "v", True)] * 2


def test_a_paste_goes_ahead_when_a_file_is_missing_or_the_clipboard_cannot_be_read(screen, machine, monkeypatch):
    monkeypatch.setattr(desktop, "clipboard_files", lambda: ("Doc01.docx", "Doc02.docx"))
    assert act(move("shortcut", key="v"), screen, [file_row(0, "Doc01.docx")]) == "pressed command+v"
    assert _paste(screen, [file_row(0, "Doc01.docx")]) == "pressed command+v"
    monkeypatch.setattr(desktop, "clipboard_files", lambda: None)
    both = [file_row(0, "Doc01.docx"), file_row(1, "Doc02.docx")]
    assert act(move("shortcut", key="v"), screen, both) == "pressed command+v"
    assert _paste(screen, both) == "pressed command+v"
    assert machine == [("press", "v", True)] * 4


def test_a_target_matches_a_zero_width_name_and_a_longer_label(screen, machine):
    cut = Item(0, "剪切\u200b\u200b", 1.0, 100.0, 100.0, 300.0, 140.0)
    pinned = Item(1, "文档 (已固定)", 1.0, 0.0, 200.0, 40.0, 220.0)
    clock = Item(2, "Remaining Of, 30 minutes 0 seconds, \u200e9\u200e:\u200e44\u200e AM", 1.0, 0.0, 300.0, 40.0, 320.0)
    assert act(move("click", item="0", target="剪切"), screen, [cut, pinned, clock]).startswith("clicked ")
    assert act(move("click", item="1", target="文档"), screen, [cut, pinned, clock]).startswith("clicked ")
    assert act(move("scroll_down", item="2", target="9:44 AM"), screen, [cut, pinned, clock]).startswith("scrolled ")
    assert len(machine) == 3


def test_a_target_that_names_a_different_item_clicks_nothing(screen, machine):
    row = Item(91, "WPSDrive", 1.0, 0.0, 0.0, 20.0, 20.0)
    line = act(move("double_click", item="91", target="Archive"), screen, [row])
    assert line == "double_click refused: item 91 is 'WPSDrive', not 'Archive'"
    assert machine == []


def test_an_item_without_a_target_is_refused_and_a_bare_action_is_not(screen, machine):
    assert act(move("click", item="0"), screen, ITEMS) == "click refused: name item 0 in target"
    assert act(move("key", key="tab"), screen, ITEMS) == "pressed tab"
    assert act(move("wait"), screen, ITEMS) == "waited"
    assert act(move("shortcut", key="e"), screen, ITEMS) == "pressed command+e"
    assert [entry for entry in machine if entry[0] == "click"] == []
    assert ("press", "tab", False) in machine
    assert ("press", "e", True) in machine
    assert any(entry[0] == "sleep" for entry in machine)


def test_a_second_cut_of_files_already_on_the_clipboard_presses_with_a_note(screen, machine, monkeypatch):
    rows = [file_row(0, "Doc01.docx", True), file_row(1, "Doc02.docx", True)]
    held = {"files": ()}

    def press(key, command=False):
        machine.append(("press", key, command))
        held["files"] = ("Doc02.docx", "Doc01.docx")

    monkeypatch.setattr(desktop, "press", press)
    monkeypatch.setattr(desktop, "clipboard_files", lambda: held["files"])
    assert "to the clipboard:" in act(move("shortcut", key="x"), screen, rows)
    note = "(note: Doc01.docx, Doc02.docx were already on the clipboard; paste them in the destination)"
    assert act(move("shortcut", key="x"), screen, rows).endswith(note)
    decision = Decision(
        kind=SimpleNamespace(choice="shortcut"),
        item=None,
        site=SimpleNamespace(choice=""),
        key=SimpleNamespace(choice="x"),
    )
    ctx = Context(goal="move the files", browser="", email=None, typesafe=None, writer=None, history=[])
    assert perform(decision, screen, rows, ctx).endswith(note)
    assert machine == [("press", "x", True)] * 3


def test_a_cut_goes_ahead_when_the_key_the_files_or_the_clipboard_change(screen, machine, monkeypatch):
    from yuanji_computer_use.actions import forget_clipboard

    both = [file_row(0, "Doc01.docx", True), file_row(1, "Doc02.docx", True)]
    held = {"files": (), "next": ()}

    def press(key, command=False):
        machine.append(("press", key, command))
        held["files"] = held["next"]

    monkeypatch.setattr(desktop, "press", press)
    monkeypatch.setattr(desktop, "clipboard_files", lambda: held["files"])

    held["next"] = ("Doc01.docx", "Doc02.docx")
    act(move("shortcut", key="x"), screen, both)
    assert act(move("shortcut", key="c"), screen, both).startswith("pressed command+c")

    forget_clipboard()
    machine.clear()
    held["files"] = ()
    held["next"] = ("Doc01.docx",)
    act(move("shortcut", key="x"), screen, [file_row(0, "Doc01.docx", True)])
    assert act(move("shortcut", key="x"), screen, both).startswith("pressed command+x")

    forget_clipboard()
    machine.clear()
    held["files"] = ()
    held["next"] = ("Doc01.docx", "Doc02.docx")
    act(move("shortcut", key="x"), screen, both)
    held["files"] = ("Other.txt",)
    assert act(move("shortcut", key="x"), screen, both).startswith("pressed command+x")

    forget_clipboard()
    machine.clear()
    monkeypatch.setattr(desktop, "clipboard_files", lambda: None)
    assert "to the clipboard;" in act(move("shortcut", key="x"), screen, both)
    assert "to the clipboard;" in act(move("shortcut", key="x"), screen, both)
    assert machine == [("press", "x", True), ("press", "x", True)]


def test_a_cut_that_leaves_the_clipboard_unchanged_says_so(screen, machine, monkeypatch):
    monkeypatch.setattr(desktop, "clipboard_files", lambda: ("Doc01.docx",))
    assert act(move("shortcut", key="x"), screen, ITEMS) == "pressed command+x (the clipboard is unchanged: Doc01.docx)"


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
    quiet = Session(last=seen, history=[line], compared=0, markable=False, quiet=True)
    assert screen_moved(quiet, screen, ITEMS, lambda *_a, **_k: None) is True
    assert quiet.idle == 0 and quiet.history == [line]
    loud = Session(last=seen, history=["driver: pressed tab"], compared=0, markable=True)
    assert screen_moved(loud, screen, ITEMS, lambda *_a, **_k: None) is True
    assert loud.idle == 1 and loud.history[0].endswith(UNCHANGED)
    state = Session(last=seen, seen=[(seen, "shortcut x")])
    assert _take(move("shortcut", key="x"), screen, ITEMS, state).startswith("pressed command+x")
    assert ("press", "x", True) in machine


def test_a_scroll_aims_at_the_named_item_and_a_missing_item_scrolls_nothing(screen, machine):
    assert act(move("scroll_down", item="0", target="Archive"), screen, ITEMS) == "scrolled down over 'Archive'"
    assert act(move("scroll_up"), screen, ITEMS) == "scrolled up"
    assert act(move("scroll_down", item="7"), screen, ITEMS) == "scroll_down refused: there is no item '7' on this screen"
    assert machine == [("scroll", -10, (100.0, 60.0)), ("scroll", 10, None)]


def test_the_last_action_says_whether_it_was_refused():
    assert _last_action([]) == {"did": "", "refused": False}
    assert _last_action(["driver: pressed command+x"])["refused"] is False
    assert _last_action(["driver: type refused: no text field is focused"])["refused"] is True


def test_an_action_that_already_did_nothing_here_is_taken_with_a_note(screen, machine):
    seen = signature(screen, ITEMS)
    state = Session(last=seen, seen=[(seen, f"click 'Archive'{UNCHANGED}")])
    assert _take(move("click", item="0", target="Archive"), screen, ITEMS, state) == f"clicked 'Archive' (note: {DONE_BEFORE})"
    assert [entry[0] for entry in machine] == ["click"]


def test_the_action_that_looped_is_refused_after_a_change_of_course(screen, machine):
    here = signature(screen, ITEMS)
    state = Session(last=here, banned={"click 'Archive'"}, banned_on=here)
    assert _take(move("click", item="0", target="Archive"), screen, ITEMS, state) == BANNED
    assert _take(move("key", key="tab"), screen, ITEMS, state) == "pressed tab"
    assert machine == [("press", "tab", False)]


def test_an_action_the_model_made_up_does_nothing(screen, machine):
    assert act(move("hover", item="0"), screen, ITEMS) == "refused: 'hover' is not an action"
    assert machine == []


def test_every_key_the_driver_offers_is_one_both_adapters_press():
    for key in (*KEYS, *SHORTCUTS):
        # `start` and `run` are chords the adapters translate, not entries in the single-key tables.
        if key == "run":
            assert key not in windows.VK
            continue
        assert key in windows.VK
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


def test_the_same_action_stops_after_four_even_when_a_wait_sits_between_them():
    streak, previous = 0, None
    for action in ("key", "wait", "key", "key", "key"):
        streak, previous = _advance_same_action(streak, previous, move(action, key="return"), ITEMS, None)
    assert streak == MAX_SAME_ACTION
    assert previous == "key return"


def test_navigation_keys_do_not_count_as_the_same_action():
    streak, previous = 0, None
    for _ in range(MAX_SAME_ACTION + 2):
        streak, previous = _advance_same_action(streak, previous, move("key", key="down"), ITEMS, None)
    assert streak == 0 and previous is None


def test_the_driver_stops_when_return_repeats_across_screens_that_keep_changing(monkeypatch, tmp_path):
    from world import FakeWriter, Page, World, drive

    pages = [Page(name=f"s{i}", items=[f"placeholder {i}"], app="SearchHost.exe", on={"enter": f"s{i + 1}"}) for i in range(5)]
    pages.append(Page(name="s5", items=["placeholder 5"], app="SearchHost.exe"))
    writer = FakeWriter(reviews=[{"achieved": False}, {"achieved": False}], drives=[{"action": "key", "key": "return"}] * 8)

    state = drive(
        world := World(pages),
        lambda state, questions: ("none", None),
        goal="open notepad",
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        writer=writer,
        driver_steps=10,
    )

    assert state.outcome == "driver stalled"
    assert world.log == ["enter"] * (MAX_SAME_ACTION + 4)
    course = f"driver: changed course: the same action was taken {MAX_SAME_ACTION} times in a row"
    assert course in state.history
    asked = writer.drive_packets[MAX_SAME_ACTION]["change_course"]
    assert asked["why"] == f"the same action was taken {MAX_SAME_ACTION} times in a row"
    assert asked["do_not_repeat"] == ["key return"]
    assert all("change_course" not in packet for packet in writer.drive_packets[:MAX_SAME_ACTION])
    assert "change_course" not in writer.drive_packets[MAX_SAME_ACTION + 1]


def test_direction_keys_may_repeat_while_the_screen_changes(monkeypatch, tmp_path):
    from world import FakeWriter, Page, World, drive

    pages = [Page(name=f"s{i}", items=[f"row {i}"], app="Files", on={"down": f"s{i + 1}"}) for i in range(7)]
    pages.append(Page(name="s7", items=["row 7"], app="Files"))
    writer = FakeWriter(reviews=[{"achieved": False}, {"achieved": False}], drives=[{"action": "key", "key": "down"}] * 8)

    state = drive(
        world := World(pages),
        lambda state, questions: ("none", None),
        goal="reach the last row",
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        writer=writer,
        driver_steps=6,
    )

    assert state.outcome == "driver step limit"
    assert world.log == ["down"] * 6


def test_the_same_action_on_the_same_screen_changes_course_at_the_third_visit(monkeypatch, tmp_path):
    from world import FakeWriter, Page, World, drive

    def to_mid(world: World) -> str:
        world.i += 1
        return f"m{world.i}"

    home = Page(name="home", items=["File"], app="Files", on={"click:File": to_mid})
    mids = [Page(name=f"m{i}", items=["Go", f"marker {i}"], app="Files", on={"click:Go": "home"}) for i in range(1, 5)]
    writer = FakeWriter(
        reviews=[{"achieved": False}, {"achieved": False}],
        drives=[{"action": "click", "item": "File"}, {"action": "click", "item": "Go"}] * 4,
    )
    world = World([home, *mids])
    world.i = 0

    state = drive(
        world,
        lambda state, questions: ("none", None),
        goal="open the file",
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        writer=writer,
        driver_steps=10,
    )

    assert state.outcome == "driver stalled"
    assert world.log == ["click:File", "click:Go"] * 3
    why = f"this action was already taken {MAX_REVISITS} times on this screen"
    assert f"driver: changed course: {why}" in state.history
    assert state.history[-1] == f"driver: {BANNED}"
    assert writer.drive_packets[5]["change_course"]["why"] == why
    assert writer.drive_packets[5]["change_course"]["do_not_repeat"] == ["click 'File'"]


def test_run_and_start_named_as_the_action_are_pressed_as_keys(monkeypatch, tmp_path):
    opened = _drive_step({"reason": "open notepad", "action": "run"})
    assert (opened.action, opened.key) == ("key", "run")
    menu = _drive_step({"reason": "open the menu", "action": "Start"})
    assert (menu.action, menu.key) == ("key", "start")
    with pytest.raises(WriterError, match="not a driver action"):
        _drive_step({"reason": "no", "action": "run", "key": "tab"})

    from world import FakeWriter, Page, World, drive

    writer = FakeWriter(reviews=[{"achieved": False}, {"achieved": False}], drives=[{"action": "run"}, {"action": "give_up"}])
    state = drive(
        world := World([Page(name="desktop", items=["Desktop"], app="explorer")]),
        lambda state, questions: ("none", None),
        goal="open notepad",
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        writer=writer,
        driver_steps=4,
    )

    assert world.log == ["run"]
    assert state.outcome == "driver gave up"
    assert any(line.startswith("driver: pressed run") for line in state.history)
