"""The guard in conftest.py: a test that forgets to patch the machine fails instead of driving it."""

import os
import socket
import subprocess
import urllib.request
from pathlib import Path

import pytest

from typesafe_computer_use import macos, windows
from typesafe_computer_use.browser import cdp
from typesafe_computer_use.browser.cdp import Chrome, Session
from typesafe_computer_use.helper import desk_lock as helper_desk


@pytest.mark.parametrize(
    "touch",
    [
        lambda: macos.click_at((200.0, 200.0)),
        lambda: macos.press("return"),
        lambda: macos.type_text("hi"),
        lambda: macos.scroll(-5),
        lambda: macos.open_url("Google Chrome", "https://example.com"),
        lambda: macos.activate("Finder"),
        lambda: macos.screenshot(),
        lambda: macos.element_state(object()),
    ],
)
def test_an_unpatched_call_refuses_instead_of_reaching_the_machine(touch):
    # Off macOS the stand-in Quartz refuses first, before the guard is reached.
    with pytest.raises(RuntimeError, match=r"real machine|unavailable on this OS"):
        touch()


def test_the_pointer_reads_as_mid_screen_so_no_test_aborts_by_chance():
    assert macos.mouse_location() == (500.0, 500.0)
    macos.check_abort()


@pytest.mark.parametrize(
    "touch",
    [
        lambda: windows.click_at((200.0, 200.0)),
        lambda: windows.press("return"),
        lambda: windows.press("[", command=True),
        lambda: windows.type_text("hi"),
        lambda: windows.scroll(-5),
        lambda: windows.open_url("Google Chrome", "https://example.com"),
        lambda: windows.activate("notepad.exe"),
        lambda: windows.open_path(Path("state.txt")),
        lambda: windows.screenshot(),
        lambda: windows.ax_press(object()),
        lambda: windows.ax_focus(object()),
        lambda: windows.ax_set_value(object(), "hi"),
        lambda: windows.ax_operate(object(), "toggle"),
        lambda: windows.element_state(object()),
        lambda: windows.element_frame(object()),
    ],
)
def test_an_unpatched_windows_call_refuses_instead_of_reaching_the_machine(touch):
    # Off Windows a stand-in module may refuse first, before the guard is reached.
    with pytest.raises(RuntimeError, match=r"real machine|unavailable on this OS"):
        touch()


@pytest.mark.parametrize(
    "touch",
    [
        lambda: helper_desk.open_mutex(),
    ],
)
def test_the_desktop_mutex_is_not_created_during_tests(touch):
    with pytest.raises(RuntimeError, match="real machine"):
        touch()


def test_the_windows_pointer_reads_as_mid_screen_too():
    assert windows.mouse_location() == (500.0, 500.0)
    windows.check_abort()


def test_an_unpatched_session_call_refuses_instead_of_opening_one():
    from typesafe_computer_use import windows_session

    for touch in (
        windows_session.enable_child_session,
        windows_session.open_child_window,
        windows_session.connect_child_session,
        lambda: windows_session._remember_credentials("user", "secret"),
        lambda: windows_session.wait_for_child_explorer(lambda: None, 0, None),
        lambda: windows_session.launch_in_session("jev-session"),
        windows_session.attach_input_desktop,
        lambda: windows_session.create_session_pipe("jev-session"),
        lambda: windows_session.attach_session_pipe("jev-session"),
        windows_session.in_console_session,
        windows_session.collect_facts,
    ):
        with pytest.raises(RuntimeError, match="real machine"):
            touch()


# ------------------------------------------------------------- the browser backend
@pytest.mark.parametrize(
    "touch",
    [
        lambda tmp: Chrome(port=9, profile=str(tmp)).start(),
        lambda tmp: cdp.find_chrome(),
        lambda tmp: cdp._get_json("http://127.0.0.1:9/json/version"),
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
