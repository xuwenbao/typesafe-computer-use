"""The tools, on a fake desktop and a fake reading of the screen."""

import json
from types import SimpleNamespace

import pytest
from helper_fakes import FakeDesktop
from PIL import Image

from yuanji_computer_use.helper import service as service_module
from yuanji_computer_use.helper.approvals import Gate
from yuanji_computer_use.helper.service import Caller, Service, ToolFailure
from yuanji_computer_use.models import Item, Missed, Screen
from yuanji_computer_use.platforms.adapter import desktop, using


@pytest.fixture
def jev_home(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_HOME", str(tmp_path / "jev"))
    return tmp_path / "jev"


ITEMS = [Item(1, "Save", 1.0, 100, 200, 300, 240, role="button", source="ax"), Item(2, "Hello", 1.0, 400, 400, 600, 440)]


@pytest.fixture
def screen_reader(monkeypatch):
    """capture and perceive as a 2x display showing ITEMS, the first pressable through accessibility."""
    looks = []

    def capture(**kwargs):
        return Screen(
            image=Image.new("RGB", (1600, 1000)), scale=2.0, app="notepad.exe", field=None, url=None, window=(0, 0, 800, 500)
        )

    def perceive(screen, budget, goal, *args, **kwargs):
        looks.append(goal)
        screen.ax_refs[1] = "save-ref"
        return list(ITEMS)

    monkeypatch.setattr(service_module, "capture", capture)
    monkeypatch.setattr(service_module, "perceive", perceive)
    return looks


@pytest.fixture
def fake_desk():
    fake = FakeDesktop()
    with using(fake):
        yield fake


class OpenLock:
    """A desktop lock that is always free. Tests that care about the mutex pass their own."""

    def try_acquire(self) -> bool:
        return True

    def release(self) -> None:
        return None


def approving():
    return Caller(Gate())


def new_service(desk_lock=None) -> Service:
    return Service(desk_lock=desk_lock or OpenLock())


def call(service, caller, tool, **args):
    return service.call(tool, args, caller, call_id=1)


def failure(service, caller, tool, **args) -> ToolFailure:
    with pytest.raises(ToolFailure) as caught:
        call(service, caller, tool, **args)
    return caught.value


def test_a_look_numbers_the_elements_in_screen_points(jev_home, screen_reader, fake_desk, tmp_path):
    out = call(new_service(), approving(), "get_app_state")
    data = out["data"]
    assert data["app"] == "notepad.exe"
    assert data["elements"][0] == {
        "element": 1,
        "text": "Save",
        "role": "button",
        "source": "ax",
        "box": [50.0, 100.0, 150.0, 120.0],
    }
    assert data["state_id"] and out["image"]
    assert fake_desk.calls == []


def test_inspect_screen_passes_the_goal_and_never_acts(jev_home, screen_reader, fake_desk, tmp_path):
    call(new_service(), approving(), "inspect_screen", goal="find the save button")
    assert screen_reader == ["find the save button"]
    assert fake_desk.calls == []


def test_get_app_state_brings_an_ordinary_app_forward_without_asking(jev_home, screen_reader, fake_desk):
    call(new_service(), approving(), "get_app_state", app="mspaint.exe")
    assert fake_desk.calls == [("activate", "mspaint.exe")]


def test_an_element_click_goes_through_accessibility_and_makes_the_look_stale(jev_home, screen_reader, fake_desk, tmp_path):
    service, caller = new_service(), approving()
    state = call(service, caller, "get_app_state")["data"]["state_id"]
    out = call(service, caller, "click", state_id=state, element=1)
    assert fake_desk.calls == [("ax_press", "save-ref")]
    assert "via accessibility" in out["data"]["done"]
    assert failure(service, caller, "click", state_id=state, element=1).kind == "stale_state"


def test_an_element_without_a_control_is_clicked_at_its_center(jev_home, screen_reader, fake_desk, tmp_path):
    service, caller = new_service(), approving()
    state = call(service, caller, "get_app_state")["data"]["state_id"]
    call(service, caller, "click", state_id=state, element=2, count=2)
    assert fake_desk.calls == [("click_at", (250.0, 210.0), "left", 2)]


def test_an_old_state_id_is_refused_without_input(jev_home, screen_reader, fake_desk, tmp_path):
    service, caller = new_service(), approving()
    old = call(service, caller, "get_app_state")["data"]["state_id"]
    call(service, caller, "get_app_state")
    assert failure(service, caller, "click", state_id=old, element=1).kind == "stale_state"
    assert fake_desk.calls == []


def test_a_point_click_and_a_missed_one(jev_home, fake_desk, tmp_path, monkeypatch):
    service, caller = new_service(), approving()
    call(service, caller, "click", x=10.0, y=20.0, button="right")
    assert fake_desk.calls == [("click_at", (10.0, 20.0), "right", 1)]

    def miss(*args, **kwargs):
        raise Missed("the cursor went elsewhere")

    monkeypatch.setattr(fake_desk, "click_at", miss)
    assert failure(service, caller, "click", x=10.0, y=20.0).kind == "missed"


def test_a_click_needs_an_element_or_a_point(jev_home, fake_desk, tmp_path):
    assert failure(new_service(), approving(), "click").kind == "invalid_arguments"


def test_type_text_clicks_the_element_first(jev_home, screen_reader, fake_desk, tmp_path):
    service, caller = new_service(), approving()
    state = call(service, caller, "get_app_state")["data"]["state_id"]
    call(service, caller, "type_text", text="hello", state_id=state, element=2)
    assert fake_desk.calls == [("click_at", (250.0, 210.0), "left", 1), ("type_text", "hello")]


def test_set_value_needs_an_accessibility_element(jev_home, screen_reader, fake_desk, tmp_path):
    service, caller = new_service(), approving()
    state = call(service, caller, "get_app_state")["data"]["state_id"]
    assert failure(service, caller, "set_value", state_id=state, element=2, value="x").kind == "unsupported"
    state = call(service, caller, "get_app_state")["data"]["state_id"]
    call(service, caller, "set_value", state_id=state, element=1, value="x")
    assert fake_desk.calls[-1] == ("ax_set_value", "save-ref", "x")


def test_an_unknown_key_is_an_invalid_argument(jev_home, fake_desk, tmp_path, monkeypatch):
    def press(key, command=False):
        raise KeyError(key)

    monkeypatch.setattr(fake_desk, "press", press)
    assert failure(new_service(), approving(), "press_key", key="hyper").kind == "invalid_arguments"


def test_unknown_tools_and_arguments_are_refused_before_anything_runs(jev_home, fake_desk, tmp_path):
    service, caller = new_service(), approving()
    assert failure(service, caller, "format_disk").kind == "invalid_arguments"
    assert failure(service, caller, "scroll", lines=3, speed=9).kind == "invalid_arguments"
    assert fake_desk.calls == []


def test_a_refused_app_is_forbidden(jev_home, fake_desk, tmp_path):
    fake_desk.front = "cmd.exe"
    assert failure(new_service(), approving(), "press_key", key="return").kind == "forbidden"
    assert fake_desk.calls == []


def test_a_call_while_another_runs_is_busy_and_not_queued(jev_home, fake_desk, tmp_path):
    service = new_service()
    service._lock.acquire()
    try:
        assert failure(service, approving(), "frontmost_app").kind == "busy"
    finally:
        service._lock.release()
    assert call(service, approving(), "frontmost_app")["data"] == {"app": "notepad.exe", "pid": 42}


def test_a_cancel_reaches_the_call_it_names(jev_home, fake_desk, tmp_path, monkeypatch):
    service = new_service()

    def cancelled_mid_call(caller):
        service.cancel(1)
        desktop.check_abort()

    monkeypatch.setattr(service, "_frontmost_app", cancelled_mid_call)
    error = failure(service, approving(), "frontmost_app")
    assert (error.kind, error.message) == ("aborted", "cancelled by the client")


def test_run_goal_needs_a_typesafe_key(jev_home, fake_desk, tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert failure(new_service(), approving(), "run_goal", goal="x").kind == "not_configured"


def test_run_goal_acts_and_returns_what_the_run_recorded(jev_home, fake_desk, tmp_path, monkeypatch, clean_env):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(service_module.Writer, "open", classmethod(lambda cls: None))
    seen = {}

    def fake_run(cfg, ctx_factory):
        seen["cfg"], seen["ctx"] = cfg, ctx_factory(None, [])
        record = {"goal": cfg.goal, "outcome": "done", "answer": "saved", "goal_achieved": True, "history": ["clicked 'Save'"]}
        (cfg.out / "run.json").parent.mkdir(parents=True, exist_ok=True)
        (cfg.out / "run.json").write_text(json.dumps(record))
        return SimpleNamespace(view=None)

    monkeypatch.setattr(service_module, "run", fake_run)
    out = call(new_service(), approving(), "run_goal", goal="save the file", steps=7)
    assert seen["cfg"].act is True and seen["cfg"].steps == 7
    assert seen["cfg"].out.parent == jev_home / "runs"
    assert seen["ctx"].ask is None
    assert out["data"]["outcome"] == "done" and out["data"]["answer"] == "saved"
    assert out["data"]["run_folder"] == str(seen["cfg"].out)


class BusyThenFree:
    """Refuses the desktop once, then allows it, and counts releases."""

    def __init__(self) -> None:
        self.tries = 0
        self.releases = 0

    def try_acquire(self) -> bool:
        self.tries += 1
        return self.tries > 1

    def release(self) -> None:
        self.releases += 1


def test_a_busy_desktop_returns_busy_and_frees_the_process_lock(jev_home, fake_desk):
    desk = BusyThenFree()
    service = new_service(desk)
    assert failure(service, approving(), "frontmost_app").kind == "busy"
    assert fake_desk.calls == []
    assert call(service, approving(), "frontmost_app")["data"] == {"app": "notepad.exe", "pid": 42}
    assert desk.releases == 1


def test_a_call_that_raises_releases_both_locks(jev_home, fake_desk, monkeypatch):
    desk = OpenLock()
    released = []
    release_lock = desk.release

    def release():
        released.append("desk")
        release_lock()

    desk.release = release
    service = new_service(desk)
    frontmost = service._frontmost_app

    def boom(caller):
        raise RuntimeError("broke")

    monkeypatch.setattr(service, "_frontmost_app", boom)
    with pytest.raises(RuntimeError, match="broke"):
        call(service, approving(), "frontmost_app")
    assert released == ["desk"]
    monkeypatch.setattr(service, "_frontmost_app", frontmost)
    assert call(service, approving(), "frontmost_app")["data"]["app"] == "notepad.exe"
