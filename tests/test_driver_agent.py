"""The driver's tool loop, scripted so no test calls a model endpoint."""

import json
from pathlib import Path

import httpx
import openai
from agents.exceptions import MaxTurnsExceeded
from agents.run_config import CallModelData, ModelInputData
from agents.tracing import get_trace_provider
from PIL import Image
from scripted_driver import ScriptedModel

from yuanji_computer_use import driver_agent, macos
from yuanji_computer_use.agents.session import Session
from yuanji_computer_use.config import reset_settings
from yuanji_computer_use.driver import load_driver_record, take_over
from yuanji_computer_use.driver_agent import (
    INSTRUCTIONS,
    KEEP_SCREENS,
    TOOLS,
    Turn,
    keep_recent_screens,
    make_agent,
    run_segment,
    screen_message,
    screen_packet,
)
from yuanji_computer_use.models import Abort, Item
from yuanji_computer_use.platform_adapter import desktop
from yuanji_computer_use.runner import RunConfig
from yuanji_computer_use.writer import PLACEHOLDER_KEY

_MAC_ELEMENT_TEXT = macos.element_text
LARGE = (Path(__file__).parent / "fixtures" / "largefile.txt").read_text(encoding="utf-8")


def _request():
    return httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


def test_importing_the_driver_disables_tracing():
    import yuanji_computer_use.driver  # noqa: F401

    assert get_trace_provider()._disabled is True


def test_a_custom_endpoint_does_not_receive_the_openai_key(clean_env):
    clean_env.setenv("CLICKER_WRITER_API", "openai")
    clean_env.setenv("CLICKER_WRITER_BASE_URL", "http://127.0.0.1:9/v1")
    clean_env.setenv("OPENAI_API_KEY", "sk-official")
    clean_env.delenv("CLICKER_WRITER_API_KEY", raising=False)
    reset_settings()

    client = driver_agent.driver_client()

    assert client.api_key == PLACEHOLDER_KEY
    assert client.max_retries == 1
    assert "sk-official" not in str(client.api_key)


def test_macos_element_text_is_the_accessibility_value(monkeypatch):
    monkeypatch.setattr(macos, "element_text", _MAC_ELEMENT_TEXT)
    monkeypatch.setattr(macos, "ax_value", lambda ref: "hello" if ref == "there" else None)

    assert macos.element_text("there") == "hello"
    assert macos.element_text("missing") is None


def _turn(text: str = "Save", ref=None) -> Turn:
    from yuanji_computer_use.models import Screen

    shown = Screen(image=Image.new("RGB", (20, 20)), scale=1.0, app="Notes", field=None, url=None)
    item = Item(0, text, 1.0, 0, 0, 20, 20)
    if ref is not None:
        shown.ax_refs[0] = ref
    return Turn(shown, [item])


def test_read_text_prefers_accessibility_then_ocr_and_names_a_missing_item(monkeypatch):
    monkeypatch.setattr(desktop, "element_text", lambda ref: "from the control" if ref == "node" else None)
    turn = _turn("from the picture", ref="node")
    segment = run_segment(ScriptedModel([{"action": "read_text", "item": 0}]), [{"role": "user", "content": "go"}], turn, None)
    assert segment.tools[0]["result"].startswith("source=accessibility")
    assert "from the control" in segment.tools[0]["result"]

    pictured = _turn("from the picture")
    segment = run_segment(
        ScriptedModel([{"action": "read_text", "item": 0}]), [{"role": "user", "content": "go"}], pictured, None
    )
    assert segment.tools[0]["result"].startswith("source=ocr")
    assert "from the picture" in segment.tools[0]["result"]

    missing = run_segment(
        ScriptedModel([{"action": "read_text", "item": 4}]), [{"role": "user", "content": "go"}], _turn("from the picture"), None
    )
    assert next(tool["result"] for tool in missing.tools if tool["name"] == "read_text").startswith("refused")


def test_read_text_truncates_past_the_limit(monkeypatch):
    monkeypatch.setattr(driver_agent, "MAX_READ", 8)
    monkeypatch.setattr(desktop, "element_text", lambda ref: "abcdefghijklmnop")
    turn = _turn("short", ref="node")
    segment = run_segment(ScriptedModel([{"action": "read_text", "item": 0}]), [{"role": "user", "content": "go"}], turn, None)
    assert "truncated" in segment.tools[0]["result"]
    assert "abcdefgh" in segment.tools[0]["result"]
    assert "ijklmnop" not in segment.tools[0]["result"]


def test_count_of_example_in_the_notepad_fixture_is_22():
    turn = _turn("unused")
    script = {"action": "count", "word": "example", "text": LARGE}
    segment = run_segment(ScriptedModel([script]), [{"role": "user", "content": "go"}], turn, None)
    assert segment.tools[0]["result"] == "22"


def test_calculate_allows_arithmetic_and_refuses_names_and_calls():
    def once(expression: str) -> str:
        script = {"action": "calculate", "expression": expression}
        segment = run_segment(ScriptedModel([script]), [{"role": "user", "content": "go"}], _turn("unused"), None)
        return next(tool["result"] for tool in segment.tools if tool["name"] == "calculate")

    assert once("(230-1)*2") == "458"
    assert once("__import__('os')").startswith("error:")
    assert once("files + 1").startswith("error:")


def test_a_second_screen_tool_is_refused_and_the_first_is_kept():
    turn = _turn("Save")
    script = [
        {"action": "click", "item": "0", "target": "Save"},
        {"action": "click", "item": "1", "target": "Other"},
    ]
    segment = run_segment(ScriptedModel([script]), [{"role": "user", "content": "go"}], turn, None)
    assert segment.move is not None and segment.move.action == "click" and segment.move.target == "Save"
    assert segment.tools[0]["result"] == "accepted"
    assert segment.tools[1]["result"].startswith("refused")


def test_the_segment_stops_when_a_click_follows_a_count():
    turn = _turn("Save")
    scripts = [
        {"action": "count", "word": "a", "text": "a a"},
        {"action": "click", "item": "0", "target": "Save", "reason": "open it"},
    ]
    segment = run_segment(ScriptedModel(scripts), [{"role": "user", "content": "go"}], turn, None)
    assert [tool["name"] for tool in segment.tools] == ["count", "click"]
    assert segment.move is not None and segment.move.action == "click" and segment.move.reason == "open it"
    assert segment.calls == 2


def test_the_agent_stops_on_a_screen_tool_and_can_reason():
    agent = make_agent(ScriptedModel([]), "low")
    assert agent.model_settings.parallel_tool_calls is False
    assert agent.model_settings.reasoning.effort == "low"
    stopped = set(agent.tool_use_behavior["stop_at_tool_names"])
    assert {"click", "done", "give_up", "shortcut"} <= stopped
    assert "read_text" not in stopped and "count" not in stopped
    assert "your_notes" not in INSTRUCTIONS
    assert {tool.name for tool in TOOLS} >= stopped


def test_a_screen_message_carries_the_picture_only_when_vision_is_on(screen, clean_env):
    packet = screen_packet("save", screen, [], [], "stuck", [], [], None, 3)
    assert packet["kind"] == "screen"
    clean_env.setenv("CLICKER_WRITER_VISION", "true")
    reset_settings()
    seen = screen_message(packet, Image.new("RGB", (4, 4), "white"))
    assert any(part["type"] == "input_image" for part in seen["content"])
    clean_env.setenv("CLICKER_WRITER_VISION", "false")
    reset_settings()
    bare = screen_message(packet, Image.new("RGB", (4, 4), "white"))
    assert [part["type"] for part in bare["content"]] == ["input_text"]


def test_only_the_last_two_screens_keep_their_picture(clean_env):
    clean_env.setenv("CLICKER_WRITER_VISION", "true")
    reset_settings()
    messages = []
    for index in range(5):
        packet = {
            "kind": "screen",
            "frontmost_app": f"app{index}",
            "last_action": {"did": str(index), "refused": False},
            "focused_field": None,
            "screen_items": [{"i": 0, "text": "row"}],
        }
        messages.append(screen_message(packet, Image.new("RGB", (4, 4), "white")))
        if index == 0:
            messages.append({"type": "function_call_output", "call_id": "count", "output": "22"})
    kept = keep_recent_screens(
        CallModelData(
            model_data=ModelInputData(input=messages, instructions="stay"),
            agent=make_agent(ScriptedModel([]), None),
            context=None,
        )
    )
    pictures = [
        item
        for item in kept.input
        if isinstance(item, dict)
        and any(part.get("type") == "input_image" for part in item.get("content", []) if isinstance(part, dict))
    ]
    assert len(pictures) == KEEP_SCREENS
    assert any(item.get("output") == "22" for item in kept.input if isinstance(item, dict))
    assert kept.instructions == "stay"


def _drive(monkeypatch, tmp_path, screen, scripts):
    monkeypatch.setattr(desktop, "check_abort", lambda: None)
    monkeypatch.setattr(desktop, "sleep_watching", lambda seconds: None)
    clicks = []
    monkeypatch.setattr(desktop, "click_at", lambda *args, **kwargs: clicks.append(args))
    from yuanji_computer_use import runner

    def look(cfg, ctx, state, log):
        state.view = (screen, [])
        return (screen, [])

    monkeypatch.setattr(runner, "look", look)
    monkeypatch.setattr(driver_agent, "model_for", lambda: ScriptedModel(list(scripts)))
    lines = []
    state = Session(outcome="nothing helps")
    take_over(RunConfig(goal="save", out=tmp_path, driver_steps=4), type("W", (), {"writer": object()})(), state, lines.append, 0)
    return state, clicks, lines


def test_an_api_error_fails_the_takeover(monkeypatch, tmp_path, screen):
    error = openai.APIError("the endpoint refused", request=_request(), body=None)
    state, clicks, lines = _drive(monkeypatch, tmp_path, screen, [error])
    assert state.outcome == "driver failed"
    assert clicks == []
    assert any("the endpoint refused" in line for line in lines)


def test_a_reasoning_rejection_is_retried_without_it(monkeypatch, tmp_path, screen, clean_env):
    clean_env.setenv("CLICKER_ANSWER_REASONING", "low")
    reset_settings()
    response = httpx.Response(400, request=_request(), content=b"reasoning")
    error = openai.BadRequestError("unsupported reasoning_effort", response=response, body=None)
    state, _, lines = _drive(monkeypatch, tmp_path, screen, [error, {"action": "give_up", "reason": "nowhere"}])
    assert state.outcome == "driver gave up"
    assert any("without it" in line for line in lines)


def test_text_only_and_too_many_reads_fail_on_the_second_try(monkeypatch, tmp_path, screen):
    state, clicks, _ = _drive(
        monkeypatch,
        tmp_path,
        screen,
        [{"action": "text"}, {"action": "text"}, {"action": "click", "item": "0", "target": "Save"}],
    )
    assert state.outcome == "driver failed"
    assert clicks == []

    state, clicks, _ = _drive(monkeypatch, tmp_path, screen, [MaxTurnsExceeded("limit"), MaxTurnsExceeded("limit")])
    assert state.outcome == "driver failed"
    assert clicks == []


def test_a_cancel_during_the_request_is_not_clicked(monkeypatch, tmp_path):
    from world import FakeWriter, Page, World, drive

    world = World([Page(name="home", items=["Go"], app="Files")])
    writer = FakeWriter(
        reviews=[{"achieved": False, "answer": "not yet", "route": "driver"}], drives=[{"action": "click", "item": "Go"}]
    )

    def cancel():
        monkeypatch.setattr(desktop, "check_abort", lambda: (_ for _ in ()).throw(Abort("the session was cancelled")))

    writer.on_drive_request = cancel
    state = drive(
        world,
        lambda state, questions: ("none", None),
        goal="open it",
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        writer=writer,
        driver_steps=3,
    )
    assert state.outcome.startswith("aborted")
    assert "click:Go" not in world.log


def test_an_anthropic_writer_answers_without_the_driver(monkeypatch, tmp_path):
    from world import FakeWriter, Page, World, drive

    monkeypatch.setenv("CLICKER_WRITER_API", "anthropic")
    reset_settings()
    writer = FakeWriter(
        reviews=[{"achieved": False, "answer": "the file is still closed", "route": "driver"}],
        drives=[{"action": "click", "item": "Go"}],
    )
    state = drive(
        World([Page(name="home", items=["Go"], app="Files")]),
        lambda s, q: ("none", None),
        goal="open it",
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        writer=writer,
        driver_steps=3,
    )
    assert state.answer is not None and state.answer.text
    assert not state.outcome.startswith("driver")
    assert writer.drive_packets == []
    assert "CLICKER_WRITER_API=openai" in (tmp_path / "run" / "run.log").read_text(encoding="utf-8")


def test_an_offline_takeover_records_the_read_the_count_and_the_action(monkeypatch, tmp_path):
    from world import FakeWriter, Page, World, drive

    writer = FakeWriter(
        reviews=[{"achieved": False, "answer": "count it", "route": "driver"}],
        drives=[
            [
                {"action": "read_text", "item": 0},
                {"action": "count", "word": "example", "item": 0},
                {"action": "shortcut", "key": "s"},
            ],
            {"action": "done", "achieved": True, "reason": "wrote 22"},
        ],
    )
    state = drive(
        World([Page(name="note", items=[LARGE], app="Notepad")]),
        lambda s, q: ("none", None),
        goal="count example and save it",
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        writer=writer,
        driver_steps=4,
    )
    assert state.outcome == "driver done"
    records = [load_driver_record(path) for path in (tmp_path / "run").glob("step-*-driver.json")]
    counted = next(record for record in records if record.get("tools") and record["tools"][0]["name"] == "read_text")
    assert [tool["name"] for tool in counted["tools"]] == ["read_text", "count", "shortcut"]
    assert counted["tools"][1]["result"] == "22"
    assert counted["tools"][0]["result"].startswith("source=")
    summary = json.loads((tmp_path / "run" / "run.json").read_text(encoding="utf-8"))
    assert summary["driver_tool_calls"] == {"read_text": 1, "count": 1, "shortcut": 1, "done": 1}
    assert (tmp_path / "run").is_dir()


def test_a_takeover_records_the_seconds_the_driver_spent(monkeypatch, tmp_path):
    from world import FakeWriter, Page, World, drive

    from yuanji_computer_use import driver

    clock = {"t": 0.0}

    def tick():
        clock["t"] += 1.0
        return clock["t"]

    monkeypatch.setattr(driver.time, "perf_counter", tick)
    writer = FakeWriter(
        reviews=[{"achieved": False, "answer": "count it", "route": "driver"}],
        drives=[{"action": "done", "achieved": True, "reason": "wrote 22"}],
    )
    state = drive(
        World([Page(name="note", items=["example"], app="Notepad")]),
        lambda s, q: ("none", None),
        goal="count example",
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        writer=writer,
        driver_steps=3,
    )
    assert state.outcome == "driver done"
    assert state.calls.seconds["driver"] == 1.0
    assert state.calls.count["driver"] >= 1
