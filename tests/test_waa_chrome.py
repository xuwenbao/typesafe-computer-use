"""The Chrome debugging launch a WAA task is handed, rewritten before the VM sees it."""

import inspect
import json
from pathlib import Path

from typesafe_computer_use.waa.chrome import CHROME_USER_DATA, _open_chrome_tabs, chrome_cdp_config

EXAMPLES = (
    Path(__file__).resolve().parent.parent
    / ".waa/WindowsAgentArena/src/win-arena-container/client/evaluation_examples_windows/examples/chrome"
)


def task(name: str) -> list:
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))["config"]


def test_a_cdp_task_kills_chrome_and_uses_a_private_profile():
    original = task("7a5a7856-f1b6-42a4-ade9-1ca81ca0f263-wos.json")
    rewritten = chrome_cdp_config(original)
    assert rewritten[0]["parameters"]["command"] == ["taskkill", "/F", "/IM", "chrome.exe"]
    assert rewritten[1] == {"type": "sleep", "parameters": {"seconds": 1}}
    command = rewritten[2]["parameters"]["command"]
    assert command[:3] == ["google-chrome", "--remote-debugging-port=1337", "--force-renderer-accessibility"]
    assert command[-1] == f"--user-data-dir={CHROME_USER_DATA}"
    assert rewritten[3:] == original[1:]
    assert "--user-data-dir" not in json.dumps(original[0])


def test_opening_tabs_leaves_the_first_one_which_deadlocks_the_connection():
    source = inspect.getsource(_open_chrome_tabs)
    assert "connect_over_cdp" in source
    assert ".close(" not in source


def test_a_profile_task_keeps_the_default_chrome_profile():
    original = task("2ae9ba84-3a0d-4d4c-8338-3a1478dc5fe3-wos.json")
    assert chrome_cdp_config(original) == original
    assert chrome_cdp_config([{"type": "launch", "parameters": {"command": ["notepad"]}}]) == [
        {"type": "launch", "parameters": {"command": ["notepad"]}}
    ]
