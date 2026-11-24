"""The hand-off table: every legal edge, and the ones that must not exist."""

import pytest

from yuanji_computer_use.agents.status import TRANSITIONS, Status, allows

LEGAL = (
    (Status.HOST, Status.POLICY),
    (Status.POLICY, Status.POLICY),
    (Status.POLICY, Status.PLAN),
    (Status.POLICY, Status.WRITER),
    (Status.POLICY, Status.FINISH),
    (Status.PLAN, Status.POLICY),
    (Status.PLAN, Status.WRITER),
    (Status.WRITER, Status.POLICY),
    (Status.WRITER, Status.DRIVER),
    (Status.WRITER, Status.FINISH),
    (Status.DRIVER, Status.FINAL_ANSWER),
    (Status.DRIVER, Status.FINISH),
    (Status.FINAL_ANSWER, Status.FINISH),
)


def test_the_table_has_thirteen_edges():
    assert sum(len(targets) for targets in TRANSITIONS.values()) == 13
    assert all(allows(source, target) for source, target in LEGAL)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (Status.POLICY, Status.DRIVER),
        (Status.HOST, Status.WRITER),
        (Status.WRITER, Status.FINAL_ANSWER),
        (Status.DRIVER, Status.POLICY),
        (Status.FINAL_ANSWER, Status.POLICY),
        (Status.FINISH, Status.POLICY),
    ],
)
def test_a_skip_is_refused(source, target):
    assert not allows(source, target)


def test_the_step_limit_goes_to_the_writer_and_is_not_handed_back(tmp_path):
    """用完步数只交给写手。这一步已经是最后一步，写手不能再交回分类器。"""
    from yuanji_computer_use.actions import Context
    from yuanji_computer_use.agents.policy import PolicyAgent
    from yuanji_computer_use.agents.session import Session
    from yuanji_computer_use.agents.writer import WriterAgent
    from yuanji_computer_use.runner import RunConfig

    cfg = RunConfig(goal="toggle it", out=tmp_path, steps=2)
    ctx = Context(goal="toggle it", browser="", email=None, typesafe=None, writer=None, history=[])
    session = Session(cfg=cfg, ctx=ctx, log=lambda _message: None, step=2, outcome="low confidence")

    assert PolicyAgent().handle(session) is Status.WRITER
    assert session.outcome == "step limit"
    assert allows(Status.POLICY, Status.WRITER)
    assert WriterAgent().handle(session) is not Status.POLICY
    assert WriterAgent().handle(session) is Status.FINISH
