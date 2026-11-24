"""The planner matches a control by its pattern and label, and reads state without a model."""

import pytest

from yuanji_computer_use.agents.planner import Predicate, Space, matching
from yuanji_computer_use.models import Item


def _item(
    index: int, text: str, *, role: str = "", source: str = "ax", controls: tuple[str, ...] = (), y: float = 100, **state
) -> Item:
    return Item(index, text, 1.0, 100, y, 400, y + 30, role=role, source=source, controls=controls, **state)


def test_a_row_title_and_a_switch_of_the_same_name_are_not_the_same_control():
    title = _item(0, "虚拟网卡模式", role="button", controls=("invoke",))
    switch = _item(1, "虚拟网卡模式", role="button", controls=("toggle",), y=100)
    junk = _item(2, "a loading or buffering indicator.", role="image", source="icon", y=400)
    space = Space(controls=("toggle",), label="TUN|虚拟网卡")

    found = matching(space, [title, switch, junk])

    assert found == [switch]


def test_an_icon_whose_caption_does_not_match_is_left_out():
    icon = _item(0, "a loading or buffering indicator.", role="image", source="icon", y=100)
    assert matching(Space(label="TUN|虚拟网卡"), [icon]) == []


def test_an_ocr_item_makes_a_checked_assertion_unknown():
    item = _item(0, "虚拟网卡模式", source="ocr", checked=False)
    assert Predicate(checked=False).evaluate([item]) == "unknown"


def test_a_prose_flag_and_a_role_used_as_a_pattern_do_not_drop_the_plan():
    from yuanji_computer_use.agents.planner import plan_from

    plan = plan_from(
        {
            "goal_state": {"require": {"exists": "ok_dialog_dismissed"}},
            "subgoals": [
                {
                    "do": "click",
                    "space": {"role": "switch", "controls": ["button"], "label": "OK"},
                    "verify": {"exists": False},
                }
            ],
        }
    )

    assert plan_from.reason == ""
    assert len(plan.subgoals) == 1
    assert plan.subgoals[0].space.role is None
    assert plan.subgoals[0].space.controls == ()
    assert plan.subgoals[0].space.label == "OK"
    assert plan.goal_state.require.exists is None


def test_a_kind_and_expect_predicate_does_not_drop_the_plan():
    from yuanji_computer_use.agents.planner import plan_from

    plan = plan_from(
        {
            "goal_state": {
                "require": {
                    "kind": "exists",
                    "target": "Desktop folder view with 'MyFolder' visible",
                    "expect": True,
                }
            },
            "subgoals": [
                {
                    "do": "click",
                    "space": {"label": "Documents"},
                    "verify": {"kind": "exists", "expect": False},
                }
            ],
        }
    )

    assert plan_from.reason == ""
    assert plan.goal_state.require.exists is True
    assert plan.subgoals[0].verify.exists is False


def test_a_plan_that_names_states_as_words_still_parses():
    from yuanji_computer_use.agents.planner import plan_from

    plan = plan_from(
        {
            "goal_state": {"require": "checked: false", "space": {"label": "Notifications"}},
            "subgoals": [
                {"do": "click", "space": {"role": "button", "label": "Start"}, "verify": "exists"},
                {
                    "do": "ensure",
                    "space": {"role": "checkbox", "controls": "toggle", "label": "Notifications"},
                    "verify": "checked: false",
                },
            ],
        }
    )

    assert plan.goal_state.require.checked is False
    assert plan.subgoals[0].verify.exists is True
    assert plan.subgoals[1].space.controls == ("toggle",)
    assert plan.subgoals[1].verify.checked is False


def test_an_empty_role_is_the_same_as_leaving_the_role_out():
    from yuanji_computer_use.agents.planner import plan_from

    plan = plan_from(
        {
            "goal_state": {"description": "the dialog is gone", "verify": {"exists": True}},
            "subgoals": [
                {"do": "press_enter", "space": {"role": "button", "label": "OK"}, "verify": {"exists": False}},
                {"do": "wait", "space": {"role": "", "label": "Settings|System"}, "verify": {"exists": True}},
            ],
        }
    )

    assert len(plan.subgoals) == 2
    assert plan.subgoals[1].space.role is None
    assert plan.goal_state.require.exists is None


def test_a_key_press_is_not_deferred_because_the_item_vote_is_split():
    from types import SimpleNamespace

    from yuanji_computer_use.agents.session import Session
    from yuanji_computer_use.agents.strategies import _defer_first_step, _margin
    from yuanji_computer_use.decide import Decision

    kind = SimpleNamespace(choice="press_escape", confidence=0.67, probabilities={"press_escape": 0.67, "click_item": 0.18})
    item = SimpleNamespace(choice="7", confidence=0.49, probabilities={"7": 0.49, "3": 0.21})
    site = SimpleNamespace(choice="none", confidence=1.0, probabilities={"none": 1.0})
    decision = Decision(kind=kind, item=item, site=site)
    items = [_item(7, "OK", role="button"), _item(3, "X", role="button", y=200)]

    margin = _margin(decision, items)

    assert margin == pytest.approx(0.49)
    assert _defer_first_step(Session(step=1), margin) is False


def test_a_goal_state_that_is_not_an_object_keeps_the_subgoals():
    from yuanji_computer_use.agents.planner import plan_from

    plan = plan_from(
        {
            "goal_state": "the clock shows Kyoto",
            "subgoals": [{"do": "click", "space": {"label": "Kyoto"}, "verify": {"exists": True}}],
        }
    )

    assert plan_from.reason == ""
    assert len(plan.subgoals) == 1
    assert plan.subgoals[0].space.label == "Kyoto"
    assert plan.goal_state.require.exists is None


def test_a_shortcut_subgoal_keeps_its_key():
    from yuanji_computer_use.agents.planner import plan_from

    plan = plan_from(
        {
            "goal_state": {},
            "subgoals": [{"do": "shortcut", "key": "V", "verify": {"exists": True}}],
        }
    )

    assert plan_from.reason == ""
    assert plan.subgoals[0].do == "shortcut"
    assert plan.subgoals[0].key == "v"

    dropped = plan_from({"goal_state": {}, "subgoals": [{"do": "shortcut", "key": "q"}]})
    assert dropped.subgoals == ()
    assert "shortcut" in plan_from.reason


def test_a_missing_item_makes_exists_false_and_state_unknown():
    assert Predicate(exists=True).evaluate([]) == "false"
    assert Predicate(checked=False).evaluate([]) == "unknown"


def test_a_checkbox_space_widens_to_the_label_when_the_switch_is_a_button(monkeypatch, tmp_path):
    """The planner guessed checkbox. The real switch is a button that toggles, so the label is enough."""
    from types import SimpleNamespace

    from PIL import Image

    from yuanji_computer_use.agents.planner import Plan, Subgoal
    from yuanji_computer_use.agents.session import Session, StepContext
    from yuanji_computer_use.agents.strategies import SpaceDecide
    from yuanji_computer_use.decide import Decision
    from yuanji_computer_use.models import Screen
    from yuanji_computer_use.runner import RunConfig

    switch = _item(4, "TUN", role="button", controls=("toggle",), checked=True)
    other = _item(5, "Other", role="checkbox", y=200)
    asked = []

    def ask(_cfg, _ctx, _state, _sc, candidates):
        asked.append(None if candidates is None else [item.index for item in candidates])
        answer = SimpleNamespace(choice="4", confidence=0.9, probabilities={"4": 0.9})
        kind = SimpleNamespace(choice="click_item", confidence=0.9, probabilities={"click_item": 0.9})
        site = SimpleNamespace(choice="content", confidence=1.0, probabilities={"content": 1.0})
        return Decision(kind=kind, item=answer, site=site)

    monkeypatch.setattr("yuanji_computer_use.agents.strategies._ask", ask)
    monkeypatch.setattr("yuanji_computer_use.agents.strategies._margin", lambda *_a, **_k: 0.9)
    state = Session()
    state.plan = Plan(subgoals=(Subgoal(do="click", space=Space(role="checkbox", label="TUN")),))
    screen = Screen(image=Image.new("RGB", (8, 8)), scale=1, app="Settings", field=None, url=None)
    step = StepContext(step=1, screen=screen, items=[switch, other])

    SpaceDecide().run(RunConfig(goal="turn TUN off", out=tmp_path), None, state, step, lambda _line: None)

    assert asked == [[4]]
    assert state.widened == 1


def test_a_small_margin_stops_at_the_label_and_does_not_ask_the_whole_screen(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from PIL import Image

    from yuanji_computer_use.agents.planner import Plan, Subgoal
    from yuanji_computer_use.agents.session import Session, StepContext
    from yuanji_computer_use.agents.strategies import SpaceDecide
    from yuanji_computer_use.decide import Decision
    from yuanji_computer_use.models import Screen
    from yuanji_computer_use.runner import RunConfig

    edit = _item(0, "Edit", role="button")
    delete = _item(1, "Delete", role="button", y=200)
    asked = []

    def ask(_cfg, _ctx, _state, _sc, candidates):
        asked.append(None if candidates is None else [item.index for item in candidates])
        answer = SimpleNamespace(choice="0", confidence=0.2, probabilities={"0": 0.2, "1": 0.16})
        kind = SimpleNamespace(choice="click_item", confidence=0.2, probabilities={"click_item": 0.2})
        site = SimpleNamespace(choice="content", confidence=1.0, probabilities={"content": 1.0})
        return Decision(kind=kind, item=answer, site=site)

    monkeypatch.setattr("yuanji_computer_use.agents.strategies._ask", ask)
    monkeypatch.setattr("yuanji_computer_use.agents.strategies._margin", lambda *_a, **_k: 0.04)
    state = Session()
    state.plan = Plan(subgoals=(Subgoal(do="click", space=Space(role="button", label="Edit|Delete")),))
    screen = Screen(image=Image.new("RGB", (8, 8)), scale=1, app="Settings", field=None, url=None)
    step = StepContext(step=1, screen=screen, items=[edit, delete])

    SpaceDecide().run(RunConfig(goal="edit", out=tmp_path), None, state, step, lambda _line: None)

    assert asked == [[0, 1], [0, 1]]
    assert state.widened == 2
    assert step.keep_going is False
    assert state.outcome == "low confidence"


def test_a_predicate_label_reads_another_control():
    from yuanji_computer_use.agents.plan import dump_plan
    from yuanji_computer_use.agents.planner import evaluate, plan_from

    hour = _item(0, "Hour")
    running = _item(1, "Timer running", y=200)

    assert evaluate(Predicate(exists=True, label="Timer running"), Space(label="Hour"), [hour]) == "false"
    assert evaluate(Predicate(exists=True, label="Timer running"), Space(label="Hour"), [hour, running]) == "true"
    assert evaluate(Predicate(exists=True), Space(label="Hour"), [hour]) == "true"

    plan = plan_from(
        {
            "goal_state": {"space": {"label": "Timer running"}, "require": {"exists": True}},
            "subgoals": [{"do": "click", "space": {"label": "Hour"}, "verify": {"label": "Timer running", "exists": True}}],
        }
    )
    assert plan.subgoals[0].verify.label == "Timer running"
    assert dump_plan(plan)["subgoals"][0]["verify"]["label"] == "Timer running"
    assert dump_plan(plan)["goal_state"]["require"] == {"exists": True}


def test_check_plan_rejects_an_empty_goal_a_labelless_click_and_a_self_exists():
    from types import SimpleNamespace

    from yuanji_computer_use.agents.planner import check_plan, plan_from

    hour = _item(0, "Hour")
    nodes = [SimpleNamespace(label="Hidden")]
    empty_goal = plan_from(
        {"goal_state": {}, "subgoals": [{"do": "click", "space": {"label": "Hour"}, "verify": {"checked": False}}]}
    )
    no_label = plan_from(
        {
            "goal_state": {"space": {"label": "Hour"}, "require": {"exists": True}},
            "subgoals": [{"do": "click", "verify": {"checked": False}}],
        }
    )
    self_exists = plan_from(
        {
            "goal_state": {"space": {"label": "Hour"}, "require": {"exists": True}},
            "subgoals": [{"do": "click", "space": {"label": "Hour"}, "verify": {"exists": True}}],
        }
    )
    missing = plan_from(
        {
            "goal_state": {"space": {"label": "saved"}, "require": {"exists": True}},
            "subgoals": [{"do": "click", "space": {"label": "Nope"}, "verify": {"exists": False}}],
        }
    )
    offscreen = plan_from(
        {
            "goal_state": {"space": {"label": "saved"}, "require": {"exists": True}},
            "subgoals": [{"do": "press_offscreen", "space": {"label": "Hidden"}, "verify": {"exists": False}}],
        }
    )

    assert any("goal_state.require" in reason for reason in check_plan(empty_goal, [hour], []))
    assert any("needs a label" in reason for reason in check_plan(no_label, [hour], []))
    assert any("own control exists" in reason for reason in check_plan(self_exists, [hour], []))
    assert any("matches nothing" in reason for reason in check_plan(missing, [hour], nodes))
    assert check_plan(offscreen, [hour], nodes) == []


def test_check_plan_accepts_another_label_and_a_state():
    from yuanji_computer_use.agents.planner import check_plan, plan_from

    hour = _item(0, "Hour")
    other = plan_from(
        {
            "goal_state": {"require": {"label": "Timer running", "exists": True}},
            "subgoals": [{"do": "open", "space": {"label": "Hour"}, "verify": {"label": "Timer running", "exists": True}}],
        }
    )
    state = plan_from(
        {
            "goal_state": {"space": {"label": "Hour"}, "require": {"checked": False}},
            "subgoals": [{"do": "click", "space": {"label": "Hour"}, "verify": {"exists": False}}],
        }
    )

    assert check_plan(other, [hour], []) == []
    assert check_plan(state, [hour], []) == []


def test_a_label_that_matches_nothing_replans_instead_of_asking_the_whole_screen(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from PIL import Image

    from yuanji_computer_use.agents.planner import Plan, Subgoal
    from yuanji_computer_use.agents.session import Session, StepContext
    from yuanji_computer_use.agents.strategies import SpaceDecide
    from yuanji_computer_use.decide import Decision
    from yuanji_computer_use.models import Screen
    from yuanji_computer_use.runner import RunConfig

    asked = []

    def ask(*_args):
        asked.append(True)
        answer = SimpleNamespace(choice="0", confidence=0.9, probabilities={"0": 0.9})
        kind = SimpleNamespace(choice="click_item", confidence=0.9, probabilities={"click_item": 0.9})
        site = SimpleNamespace(choice="content", confidence=1.0, probabilities={"content": 1.0})
        return Decision(kind=kind, item=answer, site=site)

    monkeypatch.setattr("yuanji_computer_use.agents.strategies._ask", ask)
    state = Session()
    state.plan = Plan(subgoals=(Subgoal(do="click", space=Space(label="Missing"), verify=Predicate(exists=False)),))
    screen = Screen(image=Image.new("RGB", (8, 8)), scale=1, app="Settings", field=None, url=None)
    step = StepContext(step=2, screen=screen, items=[_item(0, "Other")])

    SpaceDecide().run(RunConfig(goal="open it", out=tmp_path), None, state, step, lambda _line: None)

    assert asked == []
    assert state.plan is None
    assert state.followup == "plan"
    assert state.verdicts == [{"do": "click", "verdict": "unknown"}]
    assert step.keep_going is False


def test_a_changed_screen_drops_refused_offscreen_labels():
    from PIL import Image

    from yuanji_computer_use.agents.session import Session
    from yuanji_computer_use.agents.strategies import screen_moved
    from yuanji_computer_use.models import Screen, signature

    state = Session()
    state.history = ["pressed 'Hidden'"]
    state.compared = 1
    gone = Screen(image=Image.new("RGB", (8, 8)), scale=1, app="Clock", field=None, url=None)
    state.last = ("Settings", None, None, (), ())
    state.refused_offscreen[state.last] = {"Hidden"}

    assert screen_moved(state, gone, [_item(0, "Hour")], lambda _line: None) is True
    assert state.refused_offscreen == {}

    same = Screen(image=Image.new("RGB", (8, 8)), scale=1, app="Clock", field=None, url=None)
    items = [_item(0, "Hour")]
    state.last = signature(same, items)
    state.refused_offscreen[state.last] = {"Hidden"}
    state.compared = len(state.history)
    assert screen_moved(state, same, items, lambda _line: None) is True
    assert list(state.refused_offscreen.values()) == [{"Hidden"}]
