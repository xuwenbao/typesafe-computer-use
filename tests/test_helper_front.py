"""`clicker mcp`: the tools run in this process, behind the user's desktop lock."""

import asyncio
import sys
import threading

import pytest
from fastmcp import Client
from helper_fakes import FakeDesktop

from yuanji_computer_use.helper import desk_lock, front
from yuanji_computer_use.helper.approvals import Gate, host_apps
from yuanji_computer_use.helper.service import Caller, Service
from yuanji_computer_use.models import Abort
from yuanji_computer_use.platform_adapter import desktop, using

TOOLS = {
    "run_goal",
    "inspect_screen",
    "get_app_state",
    "click",
    "type_text",
    "set_value",
    "press_key",
    "scroll",
    "activate",
    "frontmost_app",
}


class OpenLock:
    def try_acquire(self) -> bool:
        return True

    def release(self) -> None:
        return None


class ClosedLock:
    def try_acquire(self) -> bool:
        return False

    def release(self) -> None:
        raise AssertionError("a lock that was never taken was released")


@pytest.fixture
def jev_home(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_HOME", str(tmp_path / "jev"))
    return tmp_path / "jev"


@pytest.fixture(autouse=True)
def restore_stdio():
    """The first call points stdout at the log. Put the test's own streams back afterwards."""
    out, err = sys.stdout, sys.stderr
    yield
    sys.stdout, sys.stderr = out, err


@pytest.fixture
def fake_desk():
    fake = FakeDesktop()
    with using(fake):
        yield fake


def run(coro):
    return asyncio.run(coro)


def local_with(service) -> front.Local:
    return front.Local(service=service, caller=Caller(Gate()))


def test_the_server_lists_every_tool(jev_home):
    async def go():
        async with Client(front.build_server(local_with(Service(desk_lock=OpenLock())))) as client:
            return {tool.name for tool in await client.list_tools()}

    assert run(go()) == TOOLS
    assert set(Service.TOOLS) == TOOLS


def test_an_ordinary_app_is_driven_with_no_question(jev_home, fake_desk):
    async def go():
        server = front.build_server(local_with(Service(desk_lock=OpenLock())))
        async with Client(server) as client:
            return await client.call_tool("press_key", {"key": "return"})

    result = run(go())
    assert not result.is_error, result.content
    assert fake_desk.calls == [("press", "return", False)]


def test_a_never_driven_app_is_a_tool_error_and_gets_no_input(jev_home, fake_desk):
    fake_desk.front = "cmd.exe"

    async def go():
        server = front.build_server(local_with(Service(desk_lock=OpenLock())))
        async with Client(server) as client:
            return await client.call_tool("press_key", {"key": "return"}, raise_on_error=False)

    result = run(go())
    assert result.is_error
    assert "forbidden:" in result.content[0].text
    assert fake_desk.calls == []


def test_the_host_agent_is_never_driven(jev_home, fake_desk):
    fake_desk.front = "claude.exe"
    caller = Caller(Gate(host_apps(["claude.exe"])))

    async def go():
        server = front.build_server(front.Local(service=Service(desk_lock=OpenLock()), caller=caller))
        async with Client(server) as client:
            return await client.call_tool("click", {"x": 1.0, "y": 2.0}, raise_on_error=False)

    result = run(go())
    assert result.is_error and "forbidden:" in result.content[0].text
    assert fake_desk.calls == []


def test_cancelling_a_call_stops_it_at_the_next_input(jev_home, fake_desk, monkeypatch):
    service = Service(desk_lock=OpenLock())
    started = threading.Event()
    outcome = {}

    def long_call(caller):
        started.set()
        try:
            while True:
                desktop.sleep_watching(0.05)
        except Abort as e:
            outcome["abort"] = str(e)
            raise

    monkeypatch.setattr(service, "_frontmost_app", long_call)

    async def go():
        server = front.build_server(local_with(service))
        async with Client(server) as client:
            task = asyncio.create_task(client.call_tool("frontmost_app", {}))
            while not started.is_set():
                await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            for _ in range(50):
                if "abort" in outcome:
                    return
                await asyncio.sleep(0.05)
            raise AssertionError("the call did not stop")

    run(go())
    assert outcome["abort"] == "cancelled by the client"


def test_a_taken_desktop_lock_is_busy_and_sends_no_input(jev_home, fake_desk):
    async def go():
        server = front.build_server(local_with(Service(desk_lock=ClosedLock())))
        async with Client(server) as client:
            return await client.call_tool("frontmost_app", {}, raise_on_error=False)

    result = run(go())
    assert result.is_error and "busy:" in result.content[0].text
    assert fake_desk.calls == []


def test_a_print_during_a_call_goes_to_the_log_and_not_the_reply(jev_home, fake_desk, monkeypatch, capsys):
    service = Service(desk_lock=OpenLock())

    def printing(caller):
        print("from the call")
        return {"data": {"app": "notepad.exe", "pid": 42}, "image": None}

    monkeypatch.setattr(service, "_frontmost_app", printing)

    async def go():
        server = front.build_server(local_with(service))
        async with Client(server) as client:
            return await client.call_tool("frontmost_app", {})

    result = run(go())
    assert not result.is_error
    assert "from the call" not in result.content[0].text
    assert "from the call" in (jev_home / "state" / "jev.log").read_text(encoding="utf-8")
    assert "from the call" not in capsys.readouterr().out


def test_the_service_is_created_on_the_first_call(jev_home, fake_desk, monkeypatch):
    made = []

    class Marker:
        def __init__(self) -> None:
            made.append("desk")

        def try_acquire(self) -> bool:
            return True

        def release(self) -> None:
            return None

    monkeypatch.setattr(desk_lock, "DeskLock", Marker)
    monkeypatch.setattr(front, "ancestors", lambda: ["python.exe"])
    local = front.Local()
    assert local._service is None
    assert made == []

    async def go():
        async with Client(front.build_server(local)) as client:
            return await client.call_tool("frontmost_app", {})

    result = run(go())
    assert not result.is_error, result.content
    assert made == ["desk"]
    assert isinstance(local._service, Service)
