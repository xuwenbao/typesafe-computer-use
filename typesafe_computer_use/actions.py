"""Execute one decided action. Every function returns a one-line description for the history."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from typesafe_sdk import TypeSafeClient

from .config import SITES
from .decide import OFFSCREEN_PREFIX, SHORTCUT_KEYS, Decision, row_mates, verify_typed
from .models import AxNode, Field, Guidance, Item, Missed, Popup, Screen
from .platform_adapter import desktop
from .writer import Writer, WriterError, compose_text, compose_url

VERIFY_THRESHOLD = 0.5
WAIT_SECONDS = 3.0  # what a wait adds to the settle delay every step already gets; three of them cover a slow page
CLOSE_SECONDS = 0.5  # for a closed popup to leave the screen before the click under it


@dataclass(frozen=True)
class Context:
    goal: str
    browser: str
    email: str | None
    typesafe: TypeSafeClient
    writer: Writer | None
    history: list[str]
    ask: Callable[[str], str] | None = None  # puts the writer's question to the user; None when nobody is there to answer
    guidance: Guidance = field(default_factory=Guidance)  # the runner replaces the context when the writer or the user adds to it


def perform(decision: Decision, screen: Screen, items: list[Item], ctx: Context) -> str:
    desktop.check_abort()
    key = decision.chosen
    by_index = {str(it.index): it for it in items}
    if key in by_index:
        if decision.adding:
            what = add_item(by_index[key], screen)
        elif decision.opening:
            what = open_item(by_index[key], screen)
        else:
            what = click_item(by_index[key], screen, items)
        mates = row_mates(items, limit=None).get(int(key))
        # Three rows each end in a Buy: the line must say which, or trying one marks them all as tried.
        # The whole row goes in, so two rows that open alike still get lines of their own.
        return f"{what} beside {', '.join(repr(m) for m in mates)}" if mates else what
    if key.startswith(OFFSCREEN_PREFIX):
        return press_offscreen(key[len(OFFSCREEN_PREFIX) :], screen)
    if decision.kind.choice == "add_item":
        return "click refused: there is no item to add"
    handler = _HANDLERS.get(key)
    if handler is None:
        raise ValueError(f"unknown action {key!r}")
    return handler(decision, screen, items, ctx)


def _menu_blocks(item: Item, screen: Screen) -> str | None:
    """A menu item has the keyboard. Return takes it. A click on anything else picks a different command."""
    field = screen.field
    if field is None or field.role != "AXMenuBarItem":
        return None
    if item.role == "menu" and item.text == field.label:
        return None
    return (
        f"click refused: {field.label!r} is highlighted in the open menu; "
        "press return to choose it, or up or down to move the highlight"
    )


def _return_opens(screen: Screen) -> str | None:
    """Return on a file row opens the file. A menu item, a button, and a text field still take Return."""
    field = screen.field
    if field is None or field.role != "AXRow" or field.is_text:
        return None
    return f"return refused: Return opens {field.label!r}"


def _press_enter(decision, screen, items, ctx) -> str:
    refused = _return_opens(screen)
    if refused is not None:
        return refused
    desktop.press("return")
    return "pressed Return"


def open_item(item: Item, screen: Screen) -> str:
    """Open an item by double-clicking it. A single click only selects a file or a folder."""
    refused = _menu_blocks(item, screen)
    if refused is not None:
        return refused
    try:
        desktop.click_at(screen.to_points(item), count=2)
    except Missed as e:
        return f"open refused: {item.text!r} was not opened, {e}"
    return f"opened {item.text!r}"


def _disabled(item: Item) -> str | None:
    """A refusal when the tree said this control cannot be used. Unknown is not disabled."""
    if item.enabled is False:
        return f"click refused: {item.text!r} is disabled"
    return None


# After Invoke and a named default action, both of which `ax_press` already tries.
_PATTERN_CLICKS = (("toggle", "toggled"), ("select", "selected"), ("expand_toggle", "expanded or collapsed"))


def _file_rows(items: list[Item] | None) -> list[Item]:
    """File-list rows. A tab or a radio can also report a selection, and neither is one of these."""
    if not items:
        return []
    return [it for it in items if it.role == "cell" and "selection_item" in it.controls]


def _selected_file_names(items: list[Item] | None) -> list[str]:
    """Names of the selected file rows, in the order they appear on the screen."""
    names: list[str] = []
    for it in _file_rows(items):
        if it.selected and it.text not in names:
            names.append(it.text)
    return names


def _replacing_click(item: Item, items: list[Item] | None) -> str | None:
    """A plain click on a different file row would drop a selection of two or more files."""
    names = _selected_file_names(items)
    if len(names) < 2 or item.text in names:
        return None
    if item.role != "cell" or "selection_item" not in item.controls:
        return None
    listed = f"{names[0]} and {names[1]}" if len(names) == 2 else ", ".join(names[:-1]) + f" and {names[-1]}"
    return f"click refused: {listed} are selected; a plain click on {item.text!r} would replace them"


def click_item(item: Item, screen: Screen, items: list[Item] | None = None) -> str:
    """Press an item the app declared through the accessibility tree; click the pixel under it otherwise.

    A press goes to the control itself, so it lands even when the center of the box is covered by
    a sticky header, a cookie banner, or a tooltip. An element that refuses still has a location.
    A click there would land on a popup in front of the item, so that popup is closed first.
    A control the tree says is disabled is not clicked at all.
    A row that can be selected is not invoked: Invoke on a file opens it, and a click only selects.
    A plain click on a different file row is refused while two or more file rows are already selected.
    """
    refused = _disabled(item) or _menu_blocks(item, screen) or _replacing_click(item, items)
    if refused is not None:
        return refused
    ref = screen.ax_refs.get(item.index)
    # A file row lists both. Invoke is the double-click, so a click must not take it.
    selectable = "selection_item" in item.controls
    if ref is not None and not selectable and desktop.ax_press(ref):
        return f"pressed {item.text!r} via accessibility"
    if ref is not None:
        # A radio button's Select() is reported as success by Qt and then changes nothing.
        # The circle only moves when the pointer clicks it, so that pattern is not used here.
        role = ref.get("role") if isinstance(ref, dict) else None
        for operation, verb in _PATTERN_CLICKS:
            if operation == "select" and role == "AXRadioButton":
                continue
            if desktop.ax_operate(ref, operation):
                if operation == "select":
                    desktop.ax_focus(ref)  # Select() does not give the list the keyboard, so a later cut misses it
                return f"{verb} {item.text!r} via accessibility"
    popup = screen.covered.get(item.index)
    closed = f"{close_popup(popup)} then " if popup is not None else ""
    try:
        desktop.click_at(screen.to_points(item))
    except Missed as e:
        return f"{closed}click refused: {item.text!r} was not clicked, {e}"
    # The mouse click is the action either way. Whether it took is the next capture's to say.
    return f"{closed}clicked {item.text!r}"


def add_item(item: Item, screen: Screen) -> str:
    """Add an item to the selection. Select() would replace it, and Invoke would open a file."""
    refused = _disabled(item) or _menu_blocks(item, screen)
    if refused is not None:
        return refused
    ref = screen.ax_refs.get(item.index)
    if ref is not None and desktop.ax_operate(ref, "add_select"):
        desktop.ax_focus(ref)
        return f"clicked {item.text!r} with add"
    popup = screen.covered.get(item.index)
    closed = f"{close_popup(popup)} then " if popup is not None else ""
    try:
        desktop.click_at(screen.to_points(item), modifier="add")
    except Missed as e:
        return f"{closed}click refused: {item.text!r} was not clicked, {e}"
    return f"{closed}clicked {item.text!r} with add"


def close_popup(popup: Popup) -> str:
    """Close a popup with its own close button, or with Escape when it has none, and give it a moment
    to go. Never with any of its other buttons: those answer it, as 'Restore' restores the last
    session's tabs."""
    button = popup.close
    if button is not None:
        try:
            desktop.click_at((button.x + button.w / 2, button.y + button.h / 2))
        except Missed:
            button = None  # the pointer never got there
    if button is None:
        desktop.press("escape")
    desktop.sleep_watching(CLOSE_SECONDS)
    return f"closed {popup.title}" if button is not None else f"closed {popup.title} with Escape"


# A press on a text field means "put the cursor there". When the press is refused, focusing it is
# the same outcome, and the next screen's focused field is then where typing goes.
_TEXT_ROLES = frozenset({"AXTextField", "AXTextArea", "AXSearchField"})


def focus_landed(label: str) -> bool:
    """Whether the focused field is the control just asked for.

    Queuing a focus is not the same as it taking. The next read is what shows it.
    """
    field = desktop.focused_field()
    return field is not None and field.label.strip() == label.strip()


def press_offscreen(key: str, screen: Screen) -> str:
    """Press a control the app exposes but does not show.

    AXPress does not need the element to be visible: a note row scrolled thousands of points down
    and a link the browser parked above the viewport both take it. A text field that refuses the
    press is focused instead, so the next step can type into it. Anything else is scrolled into
    view and, when that puts it on the capture, clicked at the centre it has then. The box stored
    on the node is where it was at capture time, which is off the screen, so it is not a place to click.
    """
    nodes = screen.offscreen
    node = nodes[int(key)] if key.isdigit() and int(key) < len(nodes) else None
    if node is None:
        return f"press_offscreen refused: there is no off-screen control {key!r}"
    if node.enabled is False:
        return f"press_offscreen refused: {node.label!r} is disabled"
    desktop.ax_operate(node.ref, "scroll_into_view")
    if desktop.ax_press(node.ref):
        return f"pressed {node.label!r} (off-screen control) via accessibility"
    if node.role in _TEXT_ROLES and desktop.ax_focus(node.ref):
        if focus_landed(node.label):
            return f"focused {node.label!r} (off-screen control)"
        return f"press_offscreen refused: {node.label!r} did not take the focus"
    clicked = _click_revealed(node, screen)
    if clicked is not None:
        return clicked
    return f"press_offscreen refused: {node.label!r} did not accept the press"


def _click_revealed(node: AxNode, screen: Screen) -> str | None:
    """Click a control that ScrollIntoView has brought onto the capture. None when it is not there."""
    frame = desktop.element_frame(node.ref)
    if frame is None:
        return None
    x, y, w, h = frame
    if w <= 0 or h <= 0:
        return None
    cx, cy = x + w / 2, y + h / 2
    width, height = screen.size_pt
    if not (0 <= cx < width and 0 <= cy < height):
        return None
    state = desktop.element_state(node.ref)
    if state is None or not state.present or not state.visible or state.enabled is False:
        return None
    try:
        desktop.click_at((cx, cy))
    except Missed as e:
        return f"press_offscreen refused: {node.label!r} was not clicked, {e}"
    return f"clicked {node.label!r} (off-screen control)"


def fill_field(field: Field, text: str) -> str:
    """Put text in the focused field, by value if the element accepts one and keystrokes otherwise.

    Setting the value is one message instead of one per character, and it cannot be stolen by a
    page that moves the focus mid-word. It is also widely ignored, so the value is read back and
    only a field that really holds the text counts. Keystrokes land after whatever the field
    holds, including a value the element took but did not read back, so the field is always
    emptied first. Returns which path ran, for the history.
    """
    ref = field.ref
    if ref is not None:
        desktop.ax_focus(ref)
        if desktop.ax_set_value(ref, text):
            back = desktop.ax_value(ref)
            if back is not None and back.endswith(text):
                return "via accessibility"
    desktop.clear_field()
    desktop.type_text(text)
    return "via keystrokes"


def restore_field(field: Field, typed: str) -> bool:
    """Undo only our write, through the original element, never through the current focus.

    If the element disappeared or its value changed again, leave it alone. A failed AX restore
    has no keyboard fallback: Select All/Delete could destroy an unrelated field's contents.
    """
    ref = field.ref
    if ref is None or desktop.ax_value(ref) != typed:
        return False
    return desktop.ax_set_value(ref, field.value) and desktop.ax_value(ref) == field.value


def _use_browser(decision: Decision, screen, items, ctx: Context) -> str:
    """Go to the browser, and open the website the site answer named.

    `none` is the page already open there, so bringing the browser forward is the whole action. A
    catalog key is its URL, and `other` is a site outside the catalog, which only the writer can
    name. Opening a URL activates the browser too, so the three cases differ only in the page.
    """
    site = decision.site.choice
    if site == "none":
        if desktop.activate(ctx.browser):
            return f"activated {ctx.browser}"
        return f"use_browser failed: {ctx.browser} did not come to the front"
    url = SITES.get(site)
    if url is None:
        if ctx.writer is None:
            return "use_browser refused: the site is outside the catalog and no writer is available to propose a URL"
        try:
            url = compose_url(ctx.writer, ctx.goal, ctx.history, ctx.guidance)
        except WriterError as e:
            return f"use_browser refused: the writer failed ({e})"
    if not url:
        return "use_browser refused: the writer proposed no usable URL for this goal"
    if desktop.open_url(ctx.browser, url):
        return f"opened {url}"
    return f"use_browser failed: opened {url} but {ctx.browser} did not come to the front"


def _type_email(decision, screen: Screen, items, ctx: Context) -> str:
    if not (screen.field and screen.field.is_text):
        return "type_email refused: no text field is focused"
    how = fill_field(screen.field, ctx.email or "")
    return f"typed email {how}"


def _type_text(decision, screen: Screen, items, ctx: Context) -> str:
    """Fill the focused field with the writer's text, and check it, or submit it with Return.

    Text the writer submits is not checked: Return usually takes the field away (a dialog closes, a
    search runs), so a read after it no longer shows the field, and a read before it would split
    the keystrokes and the Return into two steps. The next screen says whether it took.
    """
    if not (screen.field and screen.field.is_text):
        return "type_text refused: no text field is focused"
    if ctx.writer is None:
        return "type_text refused: no writer available"
    try:
        fill = compose_text(ctx.writer, ctx.goal, screen, items, ctx.history, ctx.guidance)
    except WriterError as e:
        return f"type_text refused: the writer failed ({e})"
    text = fill.text
    if not text:
        return "type_text refused: writer declined to fill this field"
    how = fill_field(screen.field, text)
    if fill.submit:
        desktop.press("return")
        return f"typed {text!r} into {screen.field.label!r} {how} and pressed Return"
    time.sleep(0.3)
    p = verify_typed(ctx.typesafe, ctx.goal, screen.field, text, desktop.focused_field())
    if p < VERIFY_THRESHOLD:
        recovery = "restored previous value" if restore_field(screen.field, text) else "could not safely restore previous value"
        return f"typed {text!r} into {screen.field.label!r} {how} but verification failed ({p:.2f}); {recovery}"
    return f"typed {text!r} into {screen.field.label!r} {how} (verified {p:.2f})"


# Copy and cut only change the clipboard, so an unchanged screen is not a failed action.
_CLIPBOARD_KEYS = frozenset({"c", "x"})
# 上一次真正放上剪贴板的键，和当时剪贴板里的文件名。同键、同文件、剪贴板没变，就不再按一次。
_last_clipboard: tuple[str, frozenset[str]] | None = None


def forget_clipboard() -> None:
    """忘掉上一次剪切或复制。每次运行开始时调用，测试之间也调用，避免互相影响。"""
    global _last_clipboard
    _last_clipboard = None


def clipboard_line(key: str, items: list[Item] | None = None) -> str:
    """Press Control (Command on a Mac) with `key`, and name the files the clipboard holds afterward.

    A file list with nothing selected is not cut: the key would act on whatever else looked selected.
    None from the platform means it cannot read the clipboard, so that part of the line stays unmarked.
    An empty or unchanged clipboard is said outright: a quiet success would send the next step to paste.
    """
    names = _selected_file_names(items)
    if _file_rows(items) and not names:
        return "shortcut refused: no file is selected"
    before = desktop.clipboard_files()
    if _already_on_clipboard(key, names, before):
        shown = ", ".join(names)
        return f"shortcut refused: {shown} are already on the clipboard; paste them in the destination"
    desktop.press(key, command=True)
    after = desktop.clipboard_files()
    _remember_clipboard(key, before, after)
    line = f"pressed command+{key}"
    chosen = f"{', '.join(names)}; " if names else ""
    if after is None:
        return f"{line} ({chosen}to the clipboard; the screen need not change)"
    shown = ", ".join(after)
    if not after:
        return f"{line} ({chosen}the clipboard has no files)"
    if before == after:
        return f"{line} ({chosen}the clipboard is unchanged: {shown})"
    return f"{line} ({chosen}to the clipboard: {shown}; the screen need not change)"


def _already_on_clipboard(key: str, names: list[str], before: tuple[str, ...] | None) -> bool:
    """这批选中的文件刚用同一个键放上剪贴板，而且剪贴板现在还是它们。"""
    if _last_clipboard is None or not names or before is None:
        return False
    remembered_key, remembered = _last_clipboard
    held = frozenset(names)
    return remembered_key == key and held == remembered and frozenset(before) == remembered


def _remember_clipboard(key: str, before: tuple[str, ...] | None, after: tuple[str, ...] | None) -> None:
    """剪贴板确实换了一批文件时才记住。读不到、没变、或变空，都不记。"""
    global _last_clipboard
    if after and before != after:
        _last_clipboard = (key, frozenset(after))


def _paste_lands_on_itself(items: list[Item] | None) -> str | None:
    """A paste whose every clipboard file is already a row in this folder. None means the platform cannot say."""
    held = desktop.clipboard_files()
    if not held:
        return None
    rows = {it.text for it in _file_rows(items)}
    if rows and all(name in rows for name in held):
        return f"paste refused: {', '.join(held)} are already in this folder"
    return None


def _shortcut(decision: Decision, screen: Screen, items: list[Item], ctx: Context) -> str:
    key = decision.key.choice if decision.key is not None else ""
    allowed = ", ".join(SHORTCUT_KEYS)
    if key not in SHORTCUT_KEYS:
        return f"shortcut refused: {key!r} is not one of {allowed}"
    if key == "v":
        refused = _paste_lands_on_itself(items)
        if refused is not None:
            return refused
    if key in _CLIPBOARD_KEYS:
        return clipboard_line(key, items)
    desktop.press(key, command=True)
    return f"pressed command+{key}"


def _key(name: str, description: str, command: bool = False):
    def handler(decision, screen, items, ctx) -> str:
        desktop.press(name, command)
        return description

    return handler


def _scroll(lines: int, description: str):
    def handler(decision, screen, items, ctx) -> str:
        desktop.scroll(lines)
        return description

    return handler


def _wait(decision, screen, items, ctx) -> str:
    """Give a loading page time. The step's own delay follows, so a wait is worth both."""
    desktop.sleep_watching(WAIT_SECONDS)
    return "waited"


_HANDLERS = {
    "use_browser": _use_browser,
    "type_email": _type_email,
    "type_text": _type_text,
    "press_enter": _press_enter,
    "press_escape": _key("escape", "pressed Escape"),
    "go_back": _key("[", "went back", command=True),
    "scroll_down": _scroll(-10, "scrolled down"),
    "scroll_up": _scroll(10, "scrolled up"),
    "wait": _wait,
    "shortcut": _shortcut,
}
