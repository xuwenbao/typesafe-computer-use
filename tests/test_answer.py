import base64
import io
import json
from dataclasses import replace
from types import SimpleNamespace

import anthropic
import pytest
from PIL import Image

from yuanji_computer_use import runner, writer
from yuanji_computer_use.actions import Context
from yuanji_computer_use.models import Field, Guidance, Item, Popup
from yuanji_computer_use.runner import RunConfig, RunState, answer_last, hand_off, resolve, wants_driver
from yuanji_computer_use.writer import ANSWER_IMAGE_EDGE, Answer, Fill, compose_answer

GOAL = "find the next upcoming bruno mars concert"


class FakeWriter:
    """Stands in for the Anthropic client: records each request and replies with one JSON text block."""

    def __init__(self, reply: dict):
        self.requests: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)
        self._reply = {"focus": "", "question": "", **reply} if "answer" in reply else reply

    def _create(self, **request):
        self.requests.append(request)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(self._reply))])


def context(writer=None) -> Context:
    return Context(goal=GOAL, browser="Google Chrome", email=None, typesafe=None, writer=writer, history=[])


def logged() -> tuple[list[str], runner.Log]:
    lines: list[str] = []
    return lines, lambda msg="", echo=True: lines.append(msg)


def test_the_answer_request_carries_the_capture_and_the_run(screen, make_item, monkeypatch):
    monkeypatch.setenv("CLICKER_ANSWER_MODEL", "answer-model")
    fake = FakeWriter({"achieved": True, "answer": " Sep 19, 2026 in Miami. "})

    answer = compose_answer(fake, GOAL, screen, [make_item(0, "SEP 19, 2026")], ["clicked 'TOUR'"], "the goal is achieved")

    assert answer == Answer(text="Sep 19, 2026 in Miami.", achieved=True, route="end")
    request = fake.requests[0]
    assert request["model"] == "answer-model"
    image, text = request["messages"][0]["content"]
    assert image["type"] == "image" and image["source"]["media_type"] == "image/png"
    packet = json.loads(text["text"])
    assert packet["goal"] == GOAL
    assert packet["why_the_run_stopped"] == "the goal is achieved"
    assert packet["actions_taken"] == ["clicked 'TOUR'"]
    assert packet["screen_text_in_reading_order"] == ["SEP 19, 2026"]
    assert packet["on_screen"] == {"names": ["SEP 19, 2026"], "file_rows": []}


def test_the_answer_reads_which_text_is_under_a_popup(screen, make_item):
    fake = FakeWriter({"achieved": False, "answer": "The bookmark manager is open."})
    bubble = Popup("Restore pages?", 1592.0, 103.0, 334.0, 260.0)
    items = [make_item(0, "Organise"), make_item(1, "Restore pages?")]

    compose_answer(fake, GOAL, replace(screen, covered={0: bubble}), items, [], "the classifier stopped")

    packet = json.loads(fake.requests[0]["messages"][0]["content"][-1]["text"])
    assert packet["screen_text_in_reading_order"] == ["Organise (under 'Restore pages?')", "Restore pages?"]


def test_the_answer_request_carries_what_the_run_has_learned_and_offers_both_ways_on(screen, make_item):
    fake = FakeWriter({"achieved": False, "answer": "Two sizes.", "focus": " Click '15 inch' ", "question": ""})
    guided = Guidance().heard("13 or 15 inch?", "15").focused("Open the size menu")
    stops = [{"after_action": 1, "why": "the last actions changed nothing", "focus_given": "Open the size menu"}]

    answer = compose_answer(fake, GOAL, screen, [], [], "stopped", guidance=guided, earlier_stops=stops, can_ask=True)

    assert answer == Answer(text="Two sizes.", achieved=False, focus="Click '15 inch'", question="", route="classifier")
    request = fake.requests[0]
    assert set(request["output_config"]["format"]["schema"]["required"]) == {
        "achieved",
        "answer",
        "focus",
        "question",
        "route",
        "missing",
    }
    packet = json.loads(request["messages"][0]["content"][1]["text"])
    assert packet["current_focus"] == "Open the size menu"
    assert packet["user_said"] == [{"asked": "13 or 15 inch?", "replied": "15"}]
    assert packet["earlier_stops"] == stops and packet["user_can_be_asked"] is True


def test_the_typed_text_and_the_proposed_url_are_composed_under_the_guidance(screen):
    guided = Guidance().heard("which artist?", "bruno mars")
    fake = FakeWriter({"fill": True, "text": "bruno mars", "submit": False, "ok": True, "url": "https://www.brunomars.com"})

    writer.compose_text(fake, GOAL, screen, [], [], guided)
    writer.compose_url(fake, GOAL, [], guided)

    for request in fake.requests:
        assert json.loads(request["messages"][0]["content"][0]["text"])["user_said"] == [
            {"asked": "which artist?", "replied": "bruno mars"}
        ]


@pytest.mark.parametrize(
    ("role", "reply", "expected"),
    [
        ("AXTextField", {"fill": True, "text": " Favorites ", "submit": True}, Fill("Favorites", submit=True)),
        ("AXTextField", {"fill": True, "text": "Favorites", "submit": False}, Fill("Favorites")),
        ("AXTextArea", {"fill": True, "text": "Favorites", "submit": True}, Fill("Favorites")),  # Return adds a line there
        ("AXTextField", {"fill": False, "text": "Favorites", "submit": True}, Fill("")),  # nothing typed, nothing sent
    ],
    ids=["submitted", "left", "text-area", "declined"],
)
def test_the_writer_says_whether_return_follows_the_text(screen, role, reply, expected):
    field = Field(role=role, label="Name", placeholder="", value="", x=0, y=0, w=100, h=20)
    fake = FakeWriter({**reply, "reason": "the goal names the folder"})

    assert writer.compose_text(fake, GOAL, replace(screen, field=field), [], []) == expected
    schema = fake.requests[0]["output_config"]["format"]["schema"]
    assert schema["properties"]["submit"] == {"type": "boolean"} and "submit" in schema["required"]
    assert list(schema["properties"]) == ["fill", "text", "reason", "submit"], "the reason is written before submit is decided"


def test_the_capture_is_shrunk_to_the_edge_the_model_reads_and_left_intact():
    capture = Image.new("RGBA", (3456, 2234))

    block = writer._image_block(capture)

    sent = Image.open(io.BytesIO(base64.b64decode(block["source"]["data"])))
    assert sent.format == "PNG"
    assert max(sent.size) == ANSWER_IMAGE_EDGE
    assert capture.size == (3456, 2234)


def test_requests_without_a_capture_stay_text_only_on_the_writer_model(monkeypatch):
    monkeypatch.setenv("CLICKER_WRITER_MODEL", "writer-model")
    fake = FakeWriter({"ok": True, "url": "https://www.brunomars.com"})

    assert writer.compose_url(fake, GOAL, []) == "https://www.brunomars.com"

    request = fake.requests[0]
    assert request["model"] == "writer-model"
    assert [block["type"] for block in request["messages"][0]["content"]] == ["text"]


@pytest.mark.parametrize("outcome", ["dry run", "aborted (Ctrl-C)", "crashed"])
def test_a_run_that_has_nothing_to_report_asks_for_no_answer(outcome, tmp_path, screen):
    fake = FakeWriter({"achieved": True, "answer": "unused"})
    state = RunState(outcome=outcome, view=(screen, []))
    lines, log = logged()

    hand_off(RunConfig(goal=GOAL, out=tmp_path), context(fake), state, 1, log)

    assert state.answer is None and not fake.requests and not lines


def test_without_a_writer_the_run_says_why_there_is_no_answer(tmp_path, screen):
    state = RunState(outcome="done", view=(screen, []))
    lines, log = logged()

    hand_off(RunConfig(goal=GOAL, out=tmp_path), context(), state, 1, log)

    assert state.answer is None
    assert "no answer" in lines[0] and "ANTHROPIC_API_KEY" in lines[0]


def test_the_last_capture_is_answered_from_when_nothing_acted_after_it(tmp_path, screen, make_item, monkeypatch):
    monkeypatch.setattr(runner, "capture", lambda *a, **k: pytest.fail("captured again"))
    fake = FakeWriter({"achieved": True, "answer": "Sep 19, 2026 in Miami."})
    state = RunState(outcome="done", view=(screen, [make_item(0, "SEP 19, 2026")]))
    lines, log = logged()

    hand_off(RunConfig(goal=GOAL, out=tmp_path), context(fake), state, 1, log)

    assert state.answer == Answer(text="Sep 19, 2026 in Miami.", achieved=True, route="end")
    assert "goal achieved" in lines[0] and "Sep 19, 2026 in Miami." in lines[0]
    assert not (tmp_path / "answer-raw.png").exists()
    assert "already achieved" in json.loads(fake.requests[0]["messages"][0]["content"][1]["text"])["why_the_run_stopped"]


def test_the_screen_is_captured_again_when_an_action_made_the_last_capture_stale(tmp_path, screen, make_item, monkeypatch):
    monkeypatch.setattr(runner.desktop, "check_abort", lambda: None)
    monkeypatch.setattr(runner, "capture", lambda *a, **k: screen)
    monkeypatch.setattr(runner, "perceive", lambda *a, **k: [make_item(0, "TICKETS")])
    fake = FakeWriter({"achieved": False, "answer": "No dates on screen."})
    state = RunState(outcome="step limit", view=None)
    lines, log = logged()

    hand_off(RunConfig(goal=GOAL, out=tmp_path), context(fake), state, 1, log)

    assert state.answer == Answer(text="No dates on screen.", achieved=False, route="driver")
    assert "goal not achieved" in lines[0]
    assert (tmp_path / "answer-raw.png").exists()
    assert json.loads(fake.requests[0]["messages"][0]["content"][1]["text"])["screen_text_in_reading_order"] == ["TICKETS"]


def test_a_writer_that_fails_costs_the_answer_and_not_the_run(tmp_path, screen):
    def refuse(**request):
        raise anthropic.APIConnectionError(request=SimpleNamespace())  # the error only carries the request along

    fake = FakeWriter({})
    fake.messages.create = refuse
    state = RunState(outcome="done", view=(screen, []))
    lines, log = logged()

    hand_off(RunConfig(goal=GOAL, out=tmp_path), context(fake), state, 1, log)

    assert state.answer is None
    assert "no answer: the writer failed" in lines[0]


def decision(kind: str, confidence: float = 0.9) -> SimpleNamespace:
    return SimpleNamespace(
        kind=SimpleNamespace(choice=kind),
        stops=kind in ("done", "none"),
        confidence=confidence,
        chosen=kind,
        clicking=False,
        pressing_offscreen=False,
    )


@pytest.mark.parametrize(
    ("kind", "confidence", "outcome"),
    [
        ("done", 0.9, "done"),
        ("none", 0.9, "nothing helps"),
        ("scroll_down", 0.2, "low confidence"),
        ("scroll_down", 0.9, "dry run"),
    ],
)
def test_every_stop_names_its_outcome(kind, confidence, outcome, tmp_path, screen):
    state = RunState()
    _, log = logged()

    keep_going = resolve(RunConfig(goal=GOAL, out=tmp_path), context(), state, screen, [], decision(kind, confidence), {}, log)

    assert not keep_going
    assert state.outcome == outcome
    assert (outcome in runner.STOPPED) == (outcome != "dry run")


class Replies:
    """Returns a different JSON answer on each call, so a second look can disagree with the first."""

    def __init__(self, replies: list[dict]):
        self.requests: list[dict] = []
        self._replies = list(replies)
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **request):
        self.requests.append(request)
        reply = {"focus": "", "question": "", **self._replies.pop(0)}
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(reply))])


def file_rows(*names: str) -> list[Item]:
    return [Item(i, name, 1.0, 0, i * 20, 20, 20, role="cell", controls=("selection_item",)) for i, name in enumerate(names)]


def packet_of(request: dict) -> dict:
    return json.loads(request["messages"][0]["content"][-1]["text"])


def test_a_listed_name_called_absent_is_judged_again_without_the_capture(tmp_path, screen):
    fake = Replies(
        [
            {"achieved": False, "answer": "The folder is empty. 'Doc01.docx' is not there.", "focus": "Press Return"},
            {"achieved": True, "answer": "Doc01.docx and Doc02.docx are in the folder.", "focus": ""},
        ]
    )
    state = RunState(outcome="done", view=(screen, file_rows("Doc01.docx", "Doc02.docx")))
    lines, log = logged()

    keep = hand_off(
        RunConfig(goal="move the docx files into Archive", out=tmp_path, steps=10, handoffs=3), context(fake), state, 1, log
    )

    assert keep is False
    assert state.answer == Answer(text="Doc01.docx and Doc02.docx are in the folder.", achieved=True, route="end")
    assert "goal achieved" in lines[0]
    assert len(fake.requests) == 2
    assert fake.requests[0]["messages"][0]["content"][0]["type"] == "image"
    assert [block["type"] for block in fake.requests[1]["messages"][0]["content"]] == ["text"]
    second = packet_of(fake.requests[1])
    assert second["on_screen"]["file_rows"] == ["Doc01.docx", "Doc02.docx"]
    assert second["listed_names_the_previous_answer_denied"] == ["Doc01.docx", "Doc02.docx"]


def test_files_still_in_the_place_being_cleared_stay_unfinished(tmp_path, screen):
    fake = Replies(
        [
            {"achieved": False, "answer": "The folder is empty.", "focus": "Press Return"},
            {"achieved": False, "answer": "Both files are still in Documents.", "focus": "Open Archive"},
        ]
    )
    state = RunState(outcome="done", view=(screen, file_rows("Doc01.docx", "Doc02.docx")))
    lines, log = logged()

    keep = hand_off(
        RunConfig(goal="move the docx files out of Documents", out=tmp_path, steps=10, handoffs=3), context(fake), state, 1, log
    )

    assert keep is True
    assert state.answer is not None and state.answer.achieved is False
    assert state.guidance.focus == "Open Archive"
    assert "back to the classifier" in lines[0]
    assert len(fake.requests) == 2


def test_missing_is_empty_when_the_goal_is_reached_and_an_unknown_state_is_other(screen):
    reached = compose_answer(
        FakeWriter({"achieved": True, "answer": "done", "missing": [{"name": "Doc01.docx", "state": "absent"}]}),
        GOAL,
        screen,
        [],
        [],
        "stopped",
    )
    odd = compose_answer(
        FakeWriter(
            {
                "achieved": False,
                "answer": "not yet",
                "missing": [{"name": "Save", "state": "gone"}, {"name": "", "state": "absent"}],
            }
        ),
        GOAL,
        screen,
        [],
        [],
        "stopped",
    )

    assert reached.missing == ()
    assert odd.missing == (("Save", "other"),)


def test_a_file_row_named_absent_in_missing_is_judged_again_without_the_capture(tmp_path, screen):
    fake = Replies(
        [
            {"achieved": False, "answer": "not moved", "missing": [{"name": "Doc01.docx", "state": "absent"}]},
            {"achieved": True, "answer": "they are listed", "missing": []},
        ]
    )
    state = RunState(outcome="done", view=(screen, file_rows("Doc01.docx", "Doc02.docx")))
    _, log = logged()

    hand_off(RunConfig(goal="move the docx files into Archive", out=tmp_path, steps=10, handoffs=3), context(fake), state, 1, log)

    assert len(fake.requests) == 2
    assert [block["type"] for block in fake.requests[1]["messages"][0]["content"]] == ["text"]
    assert packet_of(fake.requests[1])["listed_names_the_previous_answer_denied"] == ["Doc01.docx"]


def test_a_file_row_that_should_have_left_is_not_asked_again(tmp_path, screen):
    fake = Replies(
        [
            {
                "achieved": False,
                "answer": "Doc01.docx is still here",
                "focus": "Open Archive",
                "missing": [{"name": "Doc01.docx", "state": "still_here"}],
            }
        ]
    )
    state = RunState(outcome="done", view=(screen, file_rows("Doc01.docx")))
    _, log = logged()

    hand_off(
        RunConfig(goal="move the docx files out of Documents", out=tmp_path, steps=10, handoffs=3), context(fake), state, 1, log
    )

    assert len(fake.requests) == 1


def test_an_absent_name_that_is_not_a_file_row_is_not_asked_again(tmp_path, screen):
    fake = Replies(
        [
            {
                "achieved": False,
                "answer": "no folder",
                "focus": "Create Archive",
                "missing": [{"name": "Archive", "state": "absent"}],
            }
        ]
    )
    state = RunState(outcome="done", view=(screen, file_rows("Doc01.docx")))
    _, log = logged()

    hand_off(RunConfig(goal="move the docx files into Archive", out=tmp_path, steps=10, handoffs=3), context(fake), state, 1, log)

    assert len(fake.requests) == 1


def test_a_writer_who_disagrees_with_a_finished_driver_is_asked_once_more(tmp_path, screen):
    fake = Replies(
        [
            {"achieved": False, "answer": "not moved", "missing": []},
            {"achieved": True, "answer": "both files are in Archive", "missing": []},
        ]
    )
    state = RunState(
        outcome="driver done",
        driver_done="both files are listed",
        view=(screen, file_rows("Doc01.docx", "Doc02.docx")),
    )
    lines, log = logged()

    answer_last(RunConfig(goal="move the files into Archive", out=tmp_path), context(fake), state, log)

    assert state.answer is not None and state.answer.achieved
    assert "the driver said done; asking the writer once more" in "\n".join(lines)
    assert "driver_said_done" not in packet_of(fake.requests[0])
    assert packet_of(fake.requests[1])["driver_said_done"] == "both files are listed"
    assert fake.requests[1]["messages"][0]["content"][0]["type"] == "image"


@pytest.mark.parametrize(("driver_done", "achieved"), [("", False), ("both files are listed", True)])
def test_the_writer_is_not_asked_again_when_there_is_no_disagreement(driver_done, achieved, tmp_path, screen):
    fake = Replies([{"achieved": achieved, "answer": "the screen decides", "missing": []}])
    state = RunState(
        outcome="driver done" if driver_done else "driver gave up",
        driver_done=driver_done,
        view=(screen, file_rows("Doc01.docx")),
    )
    _, log = logged()

    answer_last(RunConfig(goal="move the files into Archive", out=tmp_path), context(fake), state, log)

    assert len(fake.requests) == 1
    assert state.answer is not None and state.answer.achieved is achieved


def test_a_missing_name_that_is_not_on_screen_is_not_asked_again(tmp_path, screen):
    fake = Replies([{"achieved": False, "answer": "'Archive' is not on this screen.", "focus": "Open Archive"}])
    state = RunState(outcome="done", view=(screen, file_rows("Doc01.docx")))
    _, log = logged()

    hand_off(RunConfig(goal="move the docx files into Archive", out=tmp_path, steps=10, handoffs=3), context(fake), state, 1, log)

    assert len(fake.requests) == 1


@pytest.mark.parametrize(
    ("reply", "route"),
    [
        ({"achieved": True, "answer": "done", "route": "driver"}, "end"),
        ({"achieved": False, "answer": "next", "focus": "", "route": "classifier"}, "driver"),
        ({"achieved": False, "answer": "next", "focus": "Click 'New'"}, "classifier"),
        ({"achieved": False, "answer": "stuck", "route": "somewhere"}, "driver"),
    ],
)
def test_a_route_is_settled_before_it_is_used(reply, route, screen):
    answer = compose_answer(FakeWriter(reply), GOAL, screen, [], [], "stopped")

    assert answer.route == route


def _ready(tmp_path, driver_steps: int = 10) -> RunConfig:
    return RunConfig(goal=GOAL, out=tmp_path, act=True, steps=10, handoffs=3, driver_steps=driver_steps)


def test_a_driver_route_ends_the_classifier_and_the_driver_may_take_over(tmp_path, screen):
    fake = FakeWriter({"achieved": False, "answer": "This needs several steps.", "route": "driver"})
    state = RunState(outcome="nothing helps", view=(screen, []))
    cfg = _ready(tmp_path)
    lines, log = logged()

    assert hand_off(cfg, context(fake), state, 1, log) is False

    assert state.handoffs == [] and state.route == "driver"
    assert wants_driver(cfg, context(fake), state)
    assert "route driver" in lines[0]


@pytest.mark.parametrize("state", ["absent", "still_here"])
def test_an_end_route_that_names_a_gap_goes_to_the_driver(state, screen):
    answer = compose_answer(
        FakeWriter(
            {
                "achieved": False,
                "answer": "not done",
                "route": "end",
                "missing": [{"name": "example.txt", "state": state}],
            }
        ),
        GOAL,
        screen,
        [],
        [],
        "stopped",
    )
    assert answer.route == "driver"
    assert answer.missing == (("example.txt", state),)


def test_an_end_route_for_something_only_the_user_can_do_stays_end(screen):
    answer = compose_answer(
        FakeWriter(
            {
                "achieved": False,
                "answer": "Only the user can sign in.",
                "route": "end",
                "missing": [{"name": "password", "state": "other"}],
            }
        ),
        GOAL,
        screen,
        [],
        [],
        "stopped",
    )
    assert answer.route == "end"
    assert answer.missing == (("password", "other"),)


def test_an_end_route_short_of_the_goal_does_not_call_the_driver(tmp_path, screen):
    fake = FakeWriter({"achieved": False, "answer": "Only the user can sign in.", "route": "end"})
    state = RunState(outcome="nothing helps", view=(screen, []))
    cfg = _ready(tmp_path)

    assert hand_off(cfg, context(fake), state, 1, logged()[1]) is False

    assert state.route == "end"
    assert not wants_driver(cfg, context(fake), state)


def test_a_second_stop_on_the_same_page_goes_to_the_driver(tmp_path, screen):
    fake = FakeWriter({"achieved": False, "answer": "Click New.", "focus": "Click 'New'", "route": "classifier"})
    state = RunState(outcome="nothing helps", view=(screen, []))
    cfg = _ready(tmp_path)
    lines, log = logged()

    assert hand_off(cfg, context(fake), state, 1, log) is True
    assert len(state.handoffs) == 1 and state.handoffs[0].page == (screen.app, screen.url)

    state.history.append("clicked 'New'")
    state.outcome = "nothing helps"
    assert hand_off(cfg, context(fake), state, 2, log) is False

    assert len(state.handoffs) == 1 and state.route == "driver"
    assert any("already stopped on this page" in line for line in lines)


def test_a_second_stop_on_the_same_page_is_handed_back_when_the_driver_is_off(tmp_path, screen):
    fake = FakeWriter({"achieved": False, "answer": "Click New.", "focus": "Click 'New'", "route": "classifier"})
    state = RunState(outcome="nothing helps", view=(screen, []))
    cfg = _ready(tmp_path, driver_steps=0)
    _, log = logged()

    assert hand_off(cfg, context(fake), state, 1, log) is True
    state.history.append("clicked 'New'")
    state.outcome = "nothing helps"

    assert hand_off(cfg, context(fake), state, 2, log) is True
    assert len(state.handoffs) == 2 and state.route == "classifier"


def test_an_action_makes_the_last_capture_stale(tmp_path, screen, monkeypatch):
    monkeypatch.setattr(runner, "perform", lambda *a: "scrolled down")
    state = RunState(view=(screen, []))
    _, log = logged()

    keep_going = resolve(
        RunConfig(goal=GOAL, out=tmp_path, act=True), context(), state, screen, [], decision("scroll_down"), {}, log
    )

    assert keep_going
    assert state.view is None
