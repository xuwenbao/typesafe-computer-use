"""The host opens or focuses an application, and the existing loop still runs when that fails."""

import json
from types import SimpleNamespace

from world import FakeWriter, Page, World, drive, scripted

from yuanji_computer_use.agents.host import HostAgent
from yuanji_computer_use.agents.session import Session
from yuanji_computer_use.platforms.adapter import desktop
from yuanji_computer_use.report import Log
from yuanji_computer_use.runner import RunConfig


def _prepare(cfg, ctx, state, log=None):
    """Fill the session the way the loop does, then let the host agent open the app."""
    state.cfg = cfg
    state.ctx = ctx
    state.log = Log() if log is None else log
    HostAgent().handle(state)


def _reply(body: dict):
    text = json.dumps(body)

    def create(**_request):
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])

    return SimpleNamespace(messages=SimpleNamespace(create=create))


class _Launched(list):
    activated: list

    def __init__(self):
        super().__init__()
        self.activated = []


def _desktop(monkeypatch, windows, front):
    launched = _Launched()
    open_windows = list(windows)

    def launch(name):
        launched.append(name)
        process = name if name.endswith(".exe") else f"{name}.exe"
        front["app"] = process
        open_windows.append(("1", "Untitled", process))

    def activate(app, timeout=3.0):
        launched.activated.append(app)
        front["app"] = app if str(app).endswith(".exe") else f"{app}.exe"
        return True

    monkeypatch.setattr(desktop, "list_windows", lambda: list(open_windows))
    monkeypatch.setattr(desktop, "launch_app", launch)
    monkeypatch.setattr(desktop, "frontmost_app_and_pid", lambda: (front["app"], 1))
    monkeypatch.setattr(desktop, "activate", activate)
    monkeypatch.setattr(desktop, "sleep_watching", lambda seconds: None)
    monkeypatch.setattr(desktop, "check_abort", lambda: None)
    return launched


def test_an_open_window_is_focused_and_not_launched(monkeypatch, tmp_path):
    state = Session()
    shown = {"app": "explorer.exe"}
    launched = _desktop(monkeypatch, [("1", "Untitled - Notepad", "notepad.exe")], shown)
    _prepare(
        RunConfig(goal="type into notepad", out=tmp_path, act=True),
        SimpleNamespace(writer=_reply({"action": "focus", "id": "1", "executable": "", "reason": "Notepad is open"})),
        state,
        Log(),
    )
    assert launched == []
    assert shown["app"] == "notepad.exe"
    assert state.history == ["host: focused notepad.exe"]


def test_a_missing_app_is_launched_by_name(monkeypatch, tmp_path):
    state = Session()
    shown = {"app": "explorer.exe"}
    launched = _desktop(monkeypatch, [], shown)
    _prepare(
        RunConfig(goal="type into notepad", out=tmp_path, act=True),
        SimpleNamespace(writer=_reply({"action": "launch", "id": "", "executable": "notepad", "reason": "open it"})),
        state,
        Log(),
    )
    assert launched == ["notepad"]
    assert launched.activated == ["notepad"]
    assert state.history == ["host: launched notepad"]


def test_a_name_with_a_path_or_arguments_is_refused(monkeypatch, tmp_path):
    assert HostAgent.executable_name("notepad") == "notepad"
    assert HostAgent.executable_name("notepad.exe") == "notepad.exe"
    assert HostAgent.executable_name(r"C:\Windows\notepad.exe") is None
    assert HostAgent.executable_name("notepad /c calc") is None
    state = Session()
    launched = _desktop(monkeypatch, [], {"app": "explorer.exe"})
    _prepare(
        RunConfig(goal="open notepad", out=tmp_path, act=True),
        SimpleNamespace(writer=_reply({"action": "launch", "id": "", "executable": "notepad /c calc", "reason": "no"})),
        state,
        Log(),
    )
    assert launched == []
    assert state.history == ["host: refused 'notepad /c calc'"]


def test_settings_counts_as_open_when_the_frame_host_is_in_front(monkeypatch, tmp_path):
    state = Session()
    launched = _Launched()
    windows: list[tuple[str, str, str]] = []

    def launch(name):
        launched.append(name)
        windows.append(("1", "Settings", "ApplicationFrameHost.exe"))

    def activate(app, timeout=3.0):
        launched.activated.append(app)
        return any(HostAgent._same_app(app, process) for _, _, process in windows)

    monkeypatch.setattr(desktop, "list_windows", lambda: list(windows))
    monkeypatch.setattr(desktop, "launch_app", launch)
    monkeypatch.setattr(desktop, "frontmost_app_and_pid", lambda: ("explorer.exe", 1))
    monkeypatch.setattr(desktop, "activate", activate)
    monkeypatch.setattr(desktop, "sleep_watching", lambda seconds: None)
    monkeypatch.setattr(desktop, "check_abort", lambda: None)
    _prepare(
        RunConfig(goal="turn notifications off", out=tmp_path, act=True),
        SimpleNamespace(writer=_reply({"action": "launch", "id": "", "executable": "SystemSettings", "reason": "open settings"})),
        state,
        Log(),
    )
    assert launched == ["ms-settings:"]
    assert launched.activated == ["ms-settings:", "applicationframehost"]
    assert state.history == ["host: launched ms-settings:"]


def test_control_panel_counts_when_explorer_shows_that_title(monkeypatch, tmp_path):
    state = Session()
    launched = _Launched()
    windows: list[tuple[str, str, str]] = []

    def launch(name):
        launched.append(name)
        windows.append(("1", "Control Panel", "explorer.exe"))

    def activate(app, timeout=3.0):
        launched.activated.append(app)
        return any(HostAgent._same_app(app, process) for _, _, process in windows)

    monkeypatch.setattr(desktop, "list_windows", lambda: list(windows))
    monkeypatch.setattr(desktop, "launch_app", launch)
    monkeypatch.setattr(desktop, "frontmost_app_and_pid", lambda: ("explorer.exe", 1))
    monkeypatch.setattr(desktop, "activate", activate)
    monkeypatch.setattr(desktop, "sleep_watching", lambda seconds: None)
    monkeypatch.setattr(desktop, "check_abort", lambda: None)
    _prepare(
        RunConfig(goal="set the time zone", out=tmp_path, act=True),
        SimpleNamespace(writer=_reply({"action": "launch", "id": "", "executable": "control", "reason": "open it"})),
        state,
        Log(),
    )
    assert launched == ["control"]
    assert launched.activated == ["control", "explorer.exe"]
    assert state.history == ["host: launched control"]


def test_the_loop_still_runs_when_the_host_cannot_choose(monkeypatch, tmp_path):
    world = World([Page(name="desk", items=["This PC"])])
    state = drive(
        world,
        scripted(("done", None)),
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        writer=FakeWriter(host_fails=True),
    )
    assert state.outcome == "done"
    assert state.calls.count["classifier"] == 1
    assert state.history[0].startswith("host: could not choose an app")
