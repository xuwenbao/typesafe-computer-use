"""The guard in conftest.py: a test that forgets to patch the machine fails instead of driving it."""

import os
import socket
import subprocess
import urllib.request
from pathlib import Path

import pytest

from yuanji_computer_use.browser import cdp
from yuanji_computer_use.browser.cdp import Chrome, Session
from yuanji_computer_use.helper import desk_lock as helper_desk
from yuanji_computer_use.platforms import macos, windows


@pytest.mark.parametrize(
    "touch",
    [
        lambda: macos.MacDesktop.click_at((200.0, 200.0)),
        lambda: macos.MacDesktop.press("return"),
        lambda: macos.MacDesktop.type_text("hi"),
        lambda: macos.MacDesktop.scroll(-5),
        lambda: macos.MacDesktop.open_url("Google Chrome", "https://example.com"),
        lambda: macos.MacDesktop.activate("Finder"),
        lambda: macos.MacDesktop.screenshot(),
        lambda: macos.MacDesktop.element_state(object()),
    ],
)
def test_an_unpatched_call_refuses_instead_of_reaching_the_machine(touch):
    # Off macOS the stand-in Quartz refuses first, before the guard is reached.
    with pytest.raises(RuntimeError, match=r"real machine|unavailable on this OS"):
        touch()


def test_the_pointer_reads_as_mid_screen_so_no_test_aborts_by_chance():
    assert macos.MacDesktop.mouse_location() == (500.0, 500.0)
    macos.MacDesktop.check_abort()


@pytest.mark.parametrize(
    "touch",
    [
        lambda: windows.WindowsDesktop.click_at((200.0, 200.0)),
        lambda: windows.WindowsDesktop.press("return"),
        lambda: windows.WindowsDesktop.press("[", command=True),
        lambda: windows.WindowsDesktop.type_text("hi"),
        lambda: windows.WindowsDesktop.scroll(-5),
        lambda: windows.WindowsDesktop.open_url("Google Chrome", "https://example.com"),
        lambda: windows.WindowsDesktop.activate("notepad.exe"),
        lambda: windows.WindowsDesktop.open_path(Path("state.txt")),
        lambda: windows.WindowsDesktop.screenshot(),
        lambda: windows.WindowsDesktop.ax_press(object()),
        lambda: windows.WindowsDesktop.ax_focus(object()),
        lambda: windows.WindowsDesktop.ax_set_value(object(), "hi"),
        lambda: windows.WindowsDesktop.ax_operate(object(), "toggle"),
        lambda: windows.WindowsDesktop.element_state(object()),
        lambda: windows.WindowsDesktop.element_frame(object()),
    ],
)
def test_an_unpatched_windows_call_refuses_instead_of_reaching_the_machine(touch):
    # Off Windows a stand-in module may refuse first, before the guard is reached.
    with pytest.raises(RuntimeError, match=r"real machine|unavailable on this OS"):
        touch()


@pytest.mark.parametrize(
    "touch",
    [
        lambda: helper_desk.DeskLock.open_mutex(),
    ],
)
def test_the_desktop_mutex_is_not_created_during_tests(touch):
    with pytest.raises(RuntimeError, match="real machine"):
        touch()


def test_the_windows_pointer_reads_as_mid_screen_too():
    assert windows.WindowsDesktop.mouse_location() == (500.0, 500.0)
    windows.WindowsDesktop.check_abort()


def test_an_unpatched_session_call_refuses_instead_of_opening_one():
    from yuanji_computer_use.platforms import windows_session

    for touch in (
        windows_session.WindowsSession.enable_child_session,
        windows_session.WindowsSession.open_child_window,
        windows_session.WindowsSession.connect_child_session,
        lambda: windows_session.WindowsSession._remember_credentials("user", "secret"),
        lambda: windows_session.WindowsSession.wait_for_child_explorer(lambda: None, 0, None),
        lambda: windows_session.WindowsSession.launch_in_session("jev-session"),
        windows_session.WindowsSession.attach_input_desktop,
        lambda: windows_session.WindowsSession.create_session_pipe("jev-session"),
        lambda: windows_session.WindowsSession.attach_session_pipe("jev-session"),
        windows_session.WindowsSession.in_console_session,
        windows_session.WindowsSession.collect_facts,
    ):
        with pytest.raises(RuntimeError, match="real machine"):
            touch()


# ------------------------------------------------------------- the browser backend
@pytest.mark.parametrize(
    "touch",
    [
        lambda tmp: Chrome(port=9, profile=str(tmp)).start(),
        lambda tmp: cdp.Chrome.find_chrome(),
        lambda tmp: cdp.Chrome._get_json("http://127.0.0.1:9/json/version"),
        lambda tmp: Session("ws://127.0.0.1:9/devtools/page/X", origin="http://127.0.0.1:9"),
        lambda tmp: subprocess.Popen(["true"]),
        lambda tmp: subprocess.run(["true"]),
        lambda tmp: os.system("true"),
        lambda tmp: socket.create_connection(("93.184.215.14", 443), timeout=1),
        lambda tmp: socket.getaddrinfo("example.com", 443),
        lambda tmp: urllib.request.urlopen("https://example.com", timeout=1),
    ],
)
def test_no_browser_process_or_network_is_reachable(touch, tmp_path):
    with pytest.raises(RuntimeError, match="real machine"):
        touch(tmp_path)


def test_a_server_the_test_started_on_loopback_is_still_reachable():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.create_connection(server.getsockname(), timeout=1):
            pass
