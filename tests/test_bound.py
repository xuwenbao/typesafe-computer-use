"""A capture read that does not return gives up its wait instead of holding the step."""

import json
import time

import pytest
from PIL import Image

from yuanji_computer_use import perception
from yuanji_computer_use.actions import Context
from yuanji_computer_use.bound import CaptureLines, CaptureTimeout, SilentEvent, assemble_capture, await_capture, call_within
from yuanji_computer_use.platform_adapter import desktop
from yuanji_computer_use.report import Log
from yuanji_computer_use.runner import RunConfig, RunState, run_step


def test_call_within_returns_a_value_that_arrives_in_time():
    assert call_within(1.0, lambda: "ready", "late") == "ready"
    assert call_within(1.0, lambda: None, "late") is None


def test_call_within_returns_the_default_when_the_call_is_still_running():
    def slow():
        time.sleep(1)
        return "late"

    assert call_within(0.05, slow, "default") == "default"


def test_call_within_raises_an_error_that_arrives_in_time():
    def fail():
        raise OSError("grab failed")

    with pytest.raises(OSError, match="grab failed"):
        call_within(1.0, fail, None)


def _quiet_desktop(monkeypatch, screenshot, field=lambda: None, url=lambda browser: None):
    monkeypatch.setattr(desktop, "screenshot", screenshot)
    monkeypatch.setattr(desktop, "display_scale", lambda image: 1.0)
    monkeypatch.setattr(desktop, "frontmost_app_and_pid", lambda: ("notepad.exe", 3))
    monkeypatch.setattr(desktop, "frontmost_window_bounds", lambda pid: (0.0, 0.0, 8.0, 8.0))
    monkeypatch.setattr(desktop, "owned_popup_bounds", lambda pid: ())
    monkeypatch.setattr(desktop, "focused_field", field)
    monkeypatch.setattr(desktop, "browser_url", url)


def test_a_stuck_focused_field_still_returns_the_screen(monkeypatch):
    monkeypatch.setattr(perception, "FIELD_WAIT", 0.05)
    image = Image.new("RGB", (8, 8))

    def stuck():
        time.sleep(1)

    _quiet_desktop(monkeypatch, lambda: image, field=stuck)
    timing: dict[str, float] = {}
    screen = perception.capture(browser="", timing=timing)
    assert screen.image is image
    assert screen.field is None
    assert screen.app == "notepad.exe"
    assert timing["field_timeout"] == 1.0
    assert "url_timeout" not in timing


def test_a_stuck_address_bar_leaves_the_page_without_a_url(monkeypatch):
    monkeypatch.setattr(perception, "URL_WAIT", 0.05)
    image = Image.new("RGB", (8, 8))

    def stuck(browser):
        time.sleep(1)

    _quiet_desktop(monkeypatch, lambda: image, url=stuck)
    timing: dict[str, float] = {}
    screen = perception.capture(browser="Google Chrome", timing=timing)
    assert screen.url is None
    assert timing["url_timeout"] == 1.0
    assert "field_timeout" not in timing


def test_a_screenshot_that_waits_for_the_machine_is_not_cut_off(monkeypatch):
    monkeypatch.setattr(perception, "SCREENSHOT_WAIT", 0.05)
    image = Image.new("RGB", (8, 8))

    def slow():
        time.sleep(0.2)
        return image

    _quiet_desktop(monkeypatch, slow)
    monkeypatch.setattr(desktop, "screenshot_waits", True, raising=False)
    timing: dict[str, float] = {}
    try:
        screen = perception.capture(browser="", timing=timing)
    finally:
        monkeypatch.delattr(desktop, "screenshot_waits", raising=False)
    assert screen.image is image
    assert "screenshot_timeout" not in timing


def test_a_stuck_screenshot_ends_the_step(monkeypatch, tmp_path):
    monkeypatch.setattr(perception, "SCREENSHOT_WAIT", 0.05)

    def stuck():
        time.sleep(1)

    monkeypatch.setattr(desktop, "screenshot", stuck)
    cfg = RunConfig(goal="open notepad", out=tmp_path)
    state = RunState()
    ctx = Context(goal="open notepad", browser="", email=None, typesafe=None, writer=None, history=[])
    assert run_step(cfg, ctx, state, 1, Log(tmp_path / "run.log")) is False
    assert state.outcome == "capture timed out"
    assert state.timings[0]["screenshot_timeout"] == 1.0
    assert list(tmp_path.glob("step-*")) == []
    assert "capture timed out" in (tmp_path / "run.log").read_text(encoding="utf-8")


def test_a_shot_without_the_detail_keeps_the_picture_and_stops_the_process():
    shot, done = SilentEvent(), SilentEvent()
    lines = CaptureLines(shot, done)
    lines.feed("shot\n")
    killed = []
    got_shot, got_detail = await_capture(shot, done, 1.0, 0.05, lambda: killed.append("killed"))
    image = Image.new("RGB", (4, 4))
    timing: dict[str, float] = {}
    found, field, url = assemble_capture(got_shot, lines.detail if got_detail else None, image, timing)
    assert found is image
    assert field is None and url is None
    assert killed == ["killed"]
    assert timing["field_timeout"] == 1.0 and timing["url_timeout"] == 1.0
    shot.close()
    done.close()


def test_no_shot_line_is_a_capture_timeout():
    shot, done = SilentEvent(), SilentEvent()
    killed = []
    got_shot, got_detail = await_capture(shot, done, 0.05, 0.05, lambda: killed.append("killed"))
    with pytest.raises(CaptureTimeout):
        assemble_capture(got_shot, None, None, {})
    assert (got_shot, got_detail) == (False, False)
    assert killed == ["killed"]
    shot.close()
    done.close()


def test_a_detail_line_keeps_the_focus_and_the_address():
    shot, done = SilentEvent(), SilentEvent()
    lines = CaptureLines(shot, done)
    lines.feed("shot\n")
    field = {
        "role": "AXTextArea",
        "label": "",
        "placeholder": "",
        "value": "hi",
        "x": 1,
        "y": 2,
        "w": 3,
        "h": 4,
    }
    lines.feed(json.dumps({"field": field, "url": None}) + "\n")
    killed = []
    got_shot, got_detail = await_capture(shot, done, 1.0, 1.0, lambda: killed.append("killed"))
    image = Image.new("RGB", (2, 2))
    found, field, url = assemble_capture(got_shot, lines.detail if got_detail else None, image, {})
    assert killed == []
    assert found is image
    assert url is None
    assert field["role"] == "AXTextArea" and field["value"] == "hi"
    shot.close()
    done.close()
