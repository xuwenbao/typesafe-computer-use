"""The gate in front of every input the helper sends, and the adapter that applies it."""

import threading

import pytest
from helper_fakes import FakeDesktop

from typesafe_computer_use import windows
from typesafe_computer_use.helper import approvals
from typesafe_computer_use.helper.approvals import Denied, Gate, app_key, host_apps
from typesafe_computer_use.helper.guard import CANCELLED, GuardedDesktop
from typesafe_computer_use.models import Abort


def test_the_browser_names_are_the_windows_adapters():
    assert approvals.BROWSER_EXES == windows.BROWSER_EXES


@pytest.mark.parametrize(
    ("app", "key"),
    [
        ("notepad.exe", "notepad"),
        ("Notepad.EXE", "notepad"),
        (" notepad ", "notepad"),
        ("Google Chrome", "chrome"),
        ("msedge.exe", "msedge"),
    ],
)
def test_an_app_has_one_name(app, key):
    assert app_key(app) == key


def test_the_agent_and_its_launchers_are_host_apps_but_the_desktop_is_not():
    assert host_apps(["python.exe", "uv.exe", "Cursor.exe", "explorer.exe", "svchost.exe"]) == {"python", "uv", "cursor"}


@pytest.mark.parametrize(
    "app", ["cmd.exe", "WindowsTerminal.exe", "powershell.exe", "consent.exe", "jev.exe", "SystemSettings.exe"]
)
def test_never_driven_apps_are_refused_without_a_question(app):
    with pytest.raises(Denied, match="never driven"):
        Gate().check(app)


def test_the_host_agent_is_never_driven():
    gate = Gate(host=frozenset({"cursor"}))
    with pytest.raises(Denied, match="never driven"):
        gate.check("Cursor.exe")


def test_an_ide_may_be_clicked_but_not_typed_into():
    Gate().check("Code.exe")
    with pytest.raises(Denied, match="only be clicked"):
        Gate().check("Code.exe", typing=True)


def test_an_ordinary_app_is_driven_without_asking():
    Gate().check("notepad.exe")
    Gate().check("notepad.exe", typing=True)


def test_no_app_in_front_is_refused():
    with pytest.raises(Denied):
        Gate().check("")


def test_a_denial_is_an_abort_so_a_run_stops_before_acting():
    assert issubclass(Denied, Abort)


def guarded(front="notepad.exe"):
    inner = FakeDesktop(front)
    cancel = threading.Event()
    return GuardedDesktop(inner, Gate(), cancel), inner, cancel


def test_reads_pass_straight_through():
    desk, _, _ = guarded()
    assert desk.screenshot() == "pixels"


def test_an_ordinary_app_is_driven_on_every_input():
    desk, inner, _ = guarded()
    desk.click_at((10.0, 20.0))
    desk.type_text("hi")
    desk.ax_press("ref")
    assert [c[0] for c in inner.calls] == ["click_at", "type_text", "ax_press"]


def test_a_never_driven_app_gets_no_input():
    desk, inner, _ = guarded(front="cmd.exe")
    with pytest.raises(Denied):
        desk.click_at((10.0, 20.0))
    assert inner.calls == []


def test_typing_into_a_terminal_never_reaches_it():
    desk, inner, _ = guarded(front="WindowsTerminal.exe")
    for act in (lambda: desk.press("return"), lambda: desk.type_text("rm -rf"), lambda: desk.scroll(-3)):
        with pytest.raises(Denied):
            act()
    assert inner.calls == []


def test_activate_and_open_url_check_the_app_they_bring_forward():
    desk, inner, _ = guarded(front="explorer.exe")
    desk.activate("mspaint.exe")
    desk.open_url("Google Chrome", "https://example.com")
    assert ("activate", "mspaint.exe") in inner.calls
    assert ("open_url", "Google Chrome", "https://example.com") in inner.calls


def test_a_cancel_stops_at_the_next_abort_check():
    desk, inner, cancel = guarded()
    cancel.set()
    with pytest.raises(Abort, match=CANCELLED):
        desk.check_abort()
    with pytest.raises(Abort, match=CANCELLED):
        desk.click_at((1.0, 1.0))
    with pytest.raises(Abort, match=CANCELLED):
        desk.sleep_watching(5)
    assert inner.calls == []


def test_the_corner_still_aborts():
    desk, inner, _ = guarded()
    inner.corner = True
    with pytest.raises(Abort, match="corner"):
        desk.click_at((1.0, 1.0))


def test_the_helper_opens_no_files():
    desk, *_ = guarded()
    with pytest.raises(Abort):
        desk.open_path("state.txt")
