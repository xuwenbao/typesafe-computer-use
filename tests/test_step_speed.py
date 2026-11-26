"""What keeps a step short: the wait after an action, the models warmed ahead, and one parse cache."""

import threading
from inspect import getsource
from types import SimpleNamespace

import pytest
from PIL import Image

from yuanji_computer_use.agents.driver import DriverAgent
from yuanji_computer_use.agents.session import Session
from yuanji_computer_use.agents.writer import WriterAgent
from yuanji_computer_use.perception import pipeline as perception
from yuanji_computer_use.perception.pipeline import SETTLE_POLL, SETTLE_QUIET, settle
from yuanji_computer_use.platforms import windows_session
from yuanji_computer_use.platforms.adapter import desktop
from yuanji_computer_use.runner import RunConfig

BEFORE = Image.new("RGB", (512, 512), "white")


def changed() -> Image.Image:
    image = BEFORE.copy()
    image.paste((0, 0, 0), (100, 100, 400, 300))
    return image


@pytest.fixture
def clock(monkeypatch):
    """A clock that moves only when the wait sleeps, and the screens the wait sees, one per look."""
    now = [0.0]
    slept: list[float] = []
    looks: list = []

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    def screenshot():
        shot = looks.pop(0) if len(looks) > 1 else looks[0]
        if isinstance(shot, Exception):
            raise shot
        return shot

    monkeypatch.setattr(perception.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(desktop, "sleep_watching", sleep)
    monkeypatch.setattr(desktop, "screenshot", screenshot)
    return SimpleNamespace(now=now, slept=slept, looks=looks)


def test_a_screen_that_never_changes_gets_the_whole_delay(clock):
    clock.looks.append(BEFORE)
    settle(BEFORE, 2.0)
    assert clock.now[0] == pytest.approx(2.0)


def test_a_changed_screen_that_holds_still_ends_the_wait_early(clock):
    clock.looks.append(changed())
    settle(BEFORE, 2.0)
    assert clock.now[0] == pytest.approx(SETTLE_QUIET + SETTLE_POLL / 2, abs=SETTLE_POLL)
    assert clock.now[0] < 1.0


def test_a_screen_still_moving_waits_until_it_stops(clock):
    other = changed()
    other.paste((0, 0, 255), (0, 400, 512, 512))
    clock.looks.extend([changed(), other, changed(), other, changed()])
    settle(BEFORE, 2.0)
    assert clock.now[0] >= 4 * SETTLE_POLL + SETTLE_QUIET


def test_a_failed_screenshot_falls_back_to_the_plain_delay(clock):
    clock.looks.append(RuntimeError("no capture"))
    settle(BEFORE, 2.0)
    assert clock.slept == [2.0]


def test_a_desktop_whose_screenshot_waits_out_the_action_only_sleeps(clock, monkeypatch):
    monkeypatch.setattr(desktop, "screenshot_waits", True, raising=False)
    clock.looks.append(AssertionError("the benchmark desktop was looked at"))
    settle(BEFORE, 2.0)
    assert clock.slept == [2.0]


def test_no_delay_takes_no_screenshot(clock):
    clock.looks.append(AssertionError("looked with no delay to spend"))
    settle(BEFORE, 0)
    assert clock.slept == [0]


def test_the_warm_up_runs_each_model_once_and_only_once(fake_models, monkeypatch):
    monkeypatch.setattr(perception, "_warming", [])
    perception.start_warm_up()
    started = list(perception._warming)
    perception.start_warm_up()
    for thread in started:
        thread.join(timeout=60)
    assert len(started) == 2
    assert perception._warming == started
    assert fake_models.calls == {"ocr": 1, "detect": 1, "caption": 1}


def test_a_model_that_fails_to_warm_up_does_not_stop_the_others(monkeypatch):
    ran = []
    monkeypatch.setattr(perception.parse_ocr, "recognize_text", lambda image: ran.append("ocr") or 1 / 0)
    monkeypatch.setattr(perception.parse_detect, "detect", lambda image: ran.append("detect") or [])
    monkeypatch.setattr(perception.parse_caption, "generate_captions", lambda icons: ran.append("caption") or [""])
    perception._warm_up()
    assert ran == ["ocr", "detect", "caption"]


def test_reading_a_screen_waits_for_a_warm_up_still_loading(fake_models, monkeypatch, screen):
    release = threading.Event()
    warming = threading.Thread(target=release.wait, daemon=True)
    warming.start()
    monkeypatch.setattr(perception, "_warming", [warming])
    threading.Timer(0.05, release.set).start()
    timing: dict[str, float] = {}
    perception.read_screen(screen, None, timing)
    assert not warming.is_alive()
    assert timing["warm_up"] > 0


def test_the_driver_reads_with_the_runs_parse_cache(monkeypatch, tmp_path, screen):
    state = Session(cfg=RunConfig(goal="save", out=tmp_path), ctx=SimpleNamespace(browser=""), log=lambda line: None)
    seen = []
    monkeypatch.setattr(perception, "capture", lambda *args, **kwargs: screen)
    monkeypatch.setattr(perception, "perceive", lambda *args, **kwargs: seen.append(kwargs.get("cache")) or [])
    DriverAgent.look(state)
    assert seen == [state.parse_cache]


def test_the_answer_reads_with_the_runs_parse_cache(monkeypatch, tmp_path, screen):
    state = Session(
        cfg=RunConfig(goal="save", out=tmp_path),
        ctx=SimpleNamespace(browser="", writer=None, guidance=None),
        log=lambda line: None,
    )
    seen = []
    monkeypatch.setattr(desktop, "check_abort", lambda: None)
    monkeypatch.setattr(perception, "capture", lambda *args, **kwargs: screen)
    monkeypatch.setattr(perception, "perceive", lambda *args, **kwargs: seen.append(kwargs.get("cache")) or [])
    monkeypatch.setattr(
        "yuanji_computer_use.agents.writer.compose_answer",
        lambda *args, **kwargs: SimpleNamespace(text="", focus="", achieved=True, missing=[]),
    )
    WriterAgent().review(state, "", False)
    assert seen == [state.parse_cache]


def test_startup_marks_stay_in_order():
    perception._marks.clear()
    perception.adopt_start(perception.time.perf_counter(), {"launcher": 0.0})
    perception.mark("desktop")
    perception.mark("main")
    marks = perception.startup_report()["marks"]
    assert list(marks) == ["launcher", "desktop", "main"]
    assert marks["launcher"] <= marks["desktop"] <= marks["main"]


def test_a_warm_up_stage_records_how_long_it_took():
    perception._stages.clear()
    perception._stage("ocr", lambda: None)
    report = perception.startup_report()
    assert report["stages"]["ocr"] >= 0


def test_the_child_runs_at_full_speed_and_warms_up_before_it_starts():
    source = windows_session._LAUNCHER
    attached = source.index("attach_input_desktop()")
    unthrottled = source.index("run_at_full_speed()")
    warming = source.index("start_warm_up()")
    started = source.index("from yuanji_computer_use.cli import main")
    assert attached < unthrottled < warming < started


def test_the_child_session_task_runs_at_normal_priority():
    module = getsource(windows_session)
    start = module.index("def launch_in_session(")
    launch = module[start : module.index("\ndef ", start + 1)]
    assert "definition.Settings.Priority = TASK_PRIORITY_NORMAL" in launch
    assert 4 <= windows_session.TASK_PRIORITY_NORMAL <= 6
