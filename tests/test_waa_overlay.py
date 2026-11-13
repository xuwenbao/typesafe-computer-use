"""The files `scripts/waa setup` copies into WindowsAgentArena, checked offline.

The checkout is not here, so the overlay is read as source, never imported. A line of the copy that
differs from the saved upstream file carries a `jev:` mark, and the header names the commit
`scripts/waa` pins.
"""

from __future__ import annotations

import ast
import difflib
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OVERLAY = REPO / "waa_overlay"
SCRIPT = REPO / "scripts" / "waa"


def pinned(name: str) -> str:
    match = re.search(rf"^{name}=(\S+)$", SCRIPT.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, f"scripts/waa pins {name}"
    return match.group(1)


def added_lines(name: str) -> list[str]:
    upstream = (OVERLAY / "upstream" / name).read_text(encoding="utf-8").splitlines()
    ours = (OVERLAY / name).read_text(encoding="utf-8").splitlines()
    added = []
    for line in difflib.unified_diff(upstream, ours, n=0):
        if line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
    return added


def test_the_copies_name_the_pinned_commit_and_mark_every_change():
    commit = pinned("WAA_COMMIT")
    assert re.fullmatch(r"[0-9a-f]{40}", commit)
    digest = pinned("WAA_IMAGE_DIGEST")
    assert digest.startswith("sha256:") and len(digest) == len("sha256:") + 64
    for name in ("run.py", "start_client.sh"):
        text = (OVERLAY / name).read_text(encoding="utf-8")
        assert commit in text.splitlines()[0] or commit in text.splitlines()[1]
        unmarked = [line for line in added_lines(name) if "jev:" not in line]
        assert unmarked == [], unmarked


def test_the_runner_builds_only_jev_hands_it_the_controller_and_does_not_ask_waa_for_a_tree():
    module = ast.parse((OVERLAY / "run.py").read_text(encoding="utf-8"))
    agents = [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "JevAgent"
    ]
    assert len(agents) == 1
    controller = {kw.arg: ast.unparse(kw.value) for kw in agents[0].keywords}.get("controller")
    assert controller == "lambda: env.controller"
    assert "NaviAgent" not in (OVERLAY / "run.py").read_text(encoding="utf-8")
    (env,) = [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "DesktopEnv"
    ]
    keywords = {kw.arg: ast.unparse(kw.value) for kw in env.keywords}
    assert keywords["action_space"] == "'pyautogui'"
    assert keywords["require_a11y_tree"] == "False"


def test_the_agent_module_reexports_jev():
    module = ast.parse((OVERLAY / "mm_agents" / "jev" / "agent.py").read_text(encoding="utf-8"))
    imports = [node for node in ast.walk(module) if isinstance(node, ast.ImportFrom)]
    assert {node.module for node in imports} == {"typesafe_computer_use.waa.agent"}
    names = sorted(alias.name for node in imports for alias in node.names)
    assert names == ["JevAgent", "browser_for"]


def test_start_client_uses_the_client_venv():
    text = (OVERLAY / "start_client.sh").read_text(encoding="utf-8")
    assert "/opt/waa/bin/python" in text
    assert "\npython " not in text
