"""The Chrome debugging launch a WAA task is handed, rewritten before the VM sees it."""

import builtins
import inspect
import json
import os
from pathlib import Path

from yuanji_computer_use.waa.chrome import (
    CHROME_USER_DATA,
    ChromeSetup,
)

EXAMPLES = (
    Path(__file__).resolve().parent.parent
    / ".waa/WindowsAgentArena/src/win-arena-container/client/evaluation_examples_windows/examples/chrome"
)


def task(name: str) -> list:
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))["config"]


def test_a_cdp_task_kills_chrome_and_uses_a_private_profile():
    original = task("7a5a7856-f1b6-42a4-ade9-1ca81ca0f263-wos.json")
    rewritten = ChromeSetup.chrome_cdp_config(original)
    assert rewritten[0]["parameters"]["command"] == ["taskkill", "/F", "/IM", "chrome.exe"]
    assert rewritten[1] == {"type": "sleep", "parameters": {"seconds": 1}}
    command = rewritten[2]["parameters"]["command"]
    assert command[:3] == ["google-chrome", "--remote-debugging-port=1337", "--force-renderer-accessibility"]
    assert command[-1] == f"--user-data-dir={CHROME_USER_DATA}"
    assert rewritten[3:] == original[1:]
    assert "--user-data-dir" not in json.dumps(original[0])


def test_opening_tabs_leaves_the_first_one_which_deadlocks_the_connection():
    source = inspect.getsource(ChromeSetup._open_chrome_tabs)
    assert "connect_over_cdp" in source
    assert "bring_to_front" in source
    assert ".close(" not in source


def _run_probe(script: str, local: str) -> str:
    previous = os.environ.get("LOCALAPPDATA")
    os.environ["LOCALAPPDATA"] = local
    try:
        printed: list[str] = []
        exec(script, {"__builtins__": builtins, "print": lambda line: printed.append(str(line))})
    finally:
        if previous is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = previous
    return printed[-1]


def test_the_bookmarks_script_reads_the_private_profile_when_that_file_exists(tmp_path):
    root = tmp_path / "profile"
    (root / "Default").mkdir(parents=True)
    mark = root / "Default" / "Bookmarks"
    mark.write_text("{}", encoding="utf-8")
    assert _run_probe(ChromeSetup.bookmarks_file_script(str(root)), str(tmp_path / "absent")) == str(mark)
    fallback = tmp_path / "local" / "Google" / "Chrome" / "User Data" / "Default" / "Bookmarks"
    assert _run_probe(ChromeSetup.bookmarks_file_script(str(tmp_path / "missing")), str(tmp_path / "local")) == str(fallback)


def test_windows_bookmarks_come_from_the_probe_and_other_platforms_stay_upstream():
    seen = {}

    class Controller:
        def execute_python_command(self, command):
            seen["command"] = command
            return {"output": r"C:\private\Default\Bookmarks"}

        def get_file(self, path):
            seen["path"] = path
            return '{"roots": {"bookmark_bar": {"children": []}}}'

    env = type("Env", (), {"vm_platform": "Windows", "controller": Controller()})()
    assert ChromeSetup._bookmarks(env, {})["bookmark_bar"]["children"] == []
    assert "jev-chrome-cdp" in seen["command"]
    assert seen["path"].endswith("Bookmarks")

    ChromeSetup._bookmarks.original = lambda env, config: {"kept": True}
    other = type("Env", (), {"vm_platform": "Linux"})()
    assert ChromeSetup._bookmarks(other, {}) == {"kept": True}


def test_a_profile_task_keeps_the_default_chrome_profile():
    original = task("2ae9ba84-3a0d-4d4c-8338-3a1478dc5fe3-wos.json")
    assert ChromeSetup.chrome_cdp_config(original) == original
    assert ChromeSetup.chrome_cdp_config([{"type": "launch", "parameters": {"command": ["notepad"]}}]) == [
        {"type": "launch", "parameters": {"command": ["notepad"]}}
    ]
