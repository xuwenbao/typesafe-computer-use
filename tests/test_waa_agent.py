"""jev behind WAA's agent interface, offline: the real runner on a worker thread, and no VM."""

import json
from io import BytesIO
from pathlib import Path

import pytest
from conftest import busy_page
from world import FakeTypeSafe, scripted

from typesafe_computer_use import runner
from typesafe_computer_use.parse import detect as parse_detect
from typesafe_computer_use.parse import ocr as parse_ocr
from typesafe_computer_use.platform_adapter import current, host
from typesafe_computer_use.waa import agent as agent_module
from typesafe_computer_use.waa.agent import JevAgent, browser_for

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "waa" / "edge-address.json").read_text())
DISPLAY = (1440, 900)
GOAL = "save the page"
STEP_SECONDS = 10.0


def png() -> bytes:
    out = BytesIO()
    busy_page(DISPLAY).save(out, format="PNG")
    return out.getvalue()


SCREENSHOT = png()


def obs() -> dict:
    return {"screenshot": SCREENSHOT}


def always(kind: str, target=None):
    return lambda state, questions: (kind, target)


def run_json(out_root: Path) -> dict:
    return json.loads((out_root / "jev" / "run.json").read_text())


def calls(agent: JevAgent, observation: dict) -> tuple[str, list[str]]:
    response, actions, logs, update = agent.predict(GOAL, observation)
    assert logs == "" and update is None
    assert "FAIL" not in actions
    return response, actions


@pytest.fixture
def jev(monkeypatch, tmp_path):
    made: list[JevAgent] = []

    def make(policy, *, lines=(), **kwargs) -> JevAgent:
        monkeypatch.setattr(runner, "TypeSafeClient", lambda: FakeTypeSafe(policy))
        monkeypatch.setattr(parse_ocr, "recognize_text", lambda image: list(lines))
        monkeypatch.setattr(parse_detect, "detect", lambda image, *args, **kwargs: [])
        kwargs.setdefault("writer", None)
        agent = JevAgent(out_root=tmp_path, step_seconds=STEP_SECONDS, **kwargs)
        made.append(agent)
        return agent

    yield make
    for agent in made:
        worker = agent.worker
        agent.reset()
        assert worker is None or not worker.is_alive()
    assert current() is host


def finish(agent: JevAgent) -> None:
    agent.worker.join(STEP_SECONDS)
    assert not agent.worker.is_alive()


def test_done_is_a_four_tuple_ending_in_done(jev, tmp_path):
    agent = jev(scripted(("done", None)))

    assert calls(agent, obs()) == ("jev ended: done", ["DONE"])
    assert calls(agent, obs()) == ("jev ended: done", ["DONE"])
    assert run_json(tmp_path)["steps_taken"] == 0


BUTTON = [("保存", 0.99, (10.0, 10.0, 50.0, 30.0))]


def test_the_step_budget_hands_out_max_steps_lists_and_ends_in_done(jev, tmp_path):
    agent = jev(always("click_item", "保存"), lines=BUTTON, max_steps=2)

    assert calls(agent, obs())[0] == "click_item '保存' (0.90)"
    response, actions = calls(agent, obs())

    assert actions[-1] == "DONE"
    assert "FAIL" not in actions
    assert response.startswith("click_item '保存' (0.90); jev ended:")
    finish(agent)


def test_an_exception_in_the_loop_is_raised_from_predict(jev, tmp_path):
    def broken(state, questions):
        raise RuntimeError("the classifier broke")

    agent = jev(broken)

    with pytest.raises(RuntimeError, match="the classifier broke"):
        calls(agent, obs())
    assert run_json(tmp_path)["outcome"] == "crashed"
    assert calls(agent, obs())[1] == ["DONE"]


def test_reset_stops_a_waiting_worker_and_the_next_task_starts_clean(jev, tmp_path):
    agent = jev(always("click_item", "保存"), lines=BUTTON)
    calls(agent, obs())
    worker = agent.worker
    assert worker.is_alive()

    agent.reset()

    assert not worker.is_alive() and agent.worker is None
    assert current() is host
    assert run_json(tmp_path)["outcome"] == "aborted (reset)"

    agent.out_root = tmp_path / "task-2"
    assert calls(agent, obs())[1] != ["DONE"]
    assert agent.worker is not worker


def test_the_browser_follows_the_domain_and_the_run_records_it(jev, tmp_path, monkeypatch):
    assert browser_for("msedge") == "Microsoft Edge"
    assert browser_for("chrome") == "Google Chrome"
    assert browser_for("vlc") == "Google Chrome"
    monkeypatch.setenv(agent_module.COMMIT, "abc123")
    monkeypatch.setenv(agent_module.DIGEST, "sha256:def")
    agent = jev(scripted(("done", None)), browser=browser_for("msedge"))

    calls(agent, obs())
    finish(agent)

    waa = run_json(tmp_path)["waa"]
    assert waa["browser"] == "Microsoft Edge"
    assert waa["commit"] == "abc123"
    assert waa["image_digest"] == "sha256:def"
    assert waa["tree"] == "jev-uia"
    assert waa["tree_fetches"] == 0


class VM:
    http_server = "http://vm:5000"

    def __init__(self) -> None:
        self.scripts: list[str] = []


def test_the_vm_script_is_posted_as_python_c_and_its_tree_is_read(jev, tmp_path, monkeypatch):
    vm = VM()

    class Response:
        ok = True
        status_code = 200
        text = ""

        def json(self):
            return {"returncode": 0, "output": json.dumps(FIXTURE)}

    def post(url, json, timeout):
        assert url == "http://vm:5000/execute"
        assert json["command"][:2] == ["python", "-c"]
        assert "import pyautogui" not in json["command"][2].splitlines()[0]
        vm.scripts.append(json["command"][2])
        return Response()

    monkeypatch.setattr(agent_module.requests, "post", post)
    agent = jev(scripted(("click_item", "保存"), ("done", None)), controller=lambda: vm)

    response, actions = calls(agent, obs())
    assert actions == ["pyautogui.click(30, 20)"]
    assert response == "click_item '保存' (0.90)"
    calls(agent, obs())
    finish(agent)

    waa = run_json(tmp_path)["waa"]
    assert waa["tree_fetches"] >= 1
    assert waa["tree_failures"] == 0
    assert "def walk" in vm.scripts[0]


def test_run_on_reports_a_failed_execute(monkeypatch):
    class Response:
        ok = False
        status_code = 500
        text = "no"

        def json(self):
            return {"message": "syntax error"}

    monkeypatch.setattr(agent_module.requests, "post", lambda *args, **kwargs: Response())

    class Controller:
        http_server = "http://vm:5000"

    code, out = agent_module.run_on(Controller())("print(1)", 3)
    assert code == 500
    assert out == "syntax error"
