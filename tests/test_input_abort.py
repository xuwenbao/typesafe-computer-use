"""Exercise the real input adapter with OS calls replaced; no synthetic input reaches the host."""

from types import SimpleNamespace

import pytest

from yuanji_computer_use.platforms import macos


@pytest.fixture
def input_events(monkeypatch):
    events = []
    quartz = SimpleNamespace(
        kCGEventMouseMoved="move",
        kCGEventLeftMouseDown="down",
        kCGEventLeftMouseUp="up",
        kCGEventLeftMouseDragged="dragged",
        kCGMouseButtonLeft=0,
        CGEventCreateMouseEvent=lambda _, kind, point, button: (kind, point),
        CGEventCreateKeyboardEvent=lambda _, code, down: {"down": down},
        CGEventKeyboardSetUnicodeString=lambda event, length, text: event.update(text=text),
        kCGEventFlagMaskCommand="command",
        CGEventSetFlags=lambda event, flags: event.update(flags=flags),
        kCGScrollEventUnitLine="line",
        CGEventCreateScrollWheelEvent=lambda _, unit, count, lines: ("scroll", lines),
    )
    monkeypatch.setattr(macos, "Quartz", quartz)
    monkeypatch.setattr(macos, "_post", events.append)
    return events


def test_the_top_left_corner_no_longer_aborts(monkeypatch):
    monkeypatch.setattr(macos, "mouse_location", lambda: (0, 0))
    macos.check_abort()


def test_click_releases_the_button_when_interrupted_after_mouse_down(input_events, monkeypatch):
    def post(event):
        input_events.append(event)
        if event[0] == "down":
            raise KeyboardInterrupt

    monkeypatch.setattr(macos, "_post", post)
    with pytest.raises(KeyboardInterrupt):
        macos.click_at((200, 200))
    assert [event[0] for event in input_events] == ["move", "down", "up"]


def test_a_drag_presses_drags_and_releases(input_events):
    macos.drag((10.0, 20.0), (18.0, 28.0))
    kinds = [event[0] for event in input_events]
    assert kinds[0] == "move" and kinds[1] == "down" and kinds[-1] == "up"
    assert kinds[2:-1] == ["dragged"] * macos.DRAG_STEPS
    assert input_events[-1][1] == (18.0, 28.0)


def test_a_drag_releases_the_button_when_a_move_fails(input_events, monkeypatch):
    def post(event):
        input_events.append(event)
        if event[0] == "dragged":
            raise KeyboardInterrupt

    monkeypatch.setattr(macos, "_post", post)
    with pytest.raises(KeyboardInterrupt):
        macos.drag((10.0, 20.0), (50.0, 20.0))
    assert [event[0] for event in input_events] == ["move", "down", "dragged", "up"]


def test_start_opens_spotlight(input_events):
    codes = []
    macos.Quartz.CGEventCreateKeyboardEvent = lambda _, code, down: codes.append(code) or {"down": down}
    macos.press("start")
    assert codes == [macos.KEYCODES["space"], macos.KEYCODES["space"]]
    assert input_events == [{"down": True, "flags": "command"}, {"down": False, "flags": "command"}]


def test_a_key_goes_back_up_when_interrupted_after_key_down(input_events, monkeypatch):
    def post(event):
        input_events.append(dict(event))
        if event["down"]:
            raise KeyboardInterrupt

    monkeypatch.setattr(macos, "_post", post)
    with pytest.raises(KeyboardInterrupt):
        macos.press("a", command=True)
    assert input_events == [{"down": True, "flags": "command"}, {"down": False, "flags": "command"}]
