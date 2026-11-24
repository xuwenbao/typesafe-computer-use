"""The step pipeline writes the same five files, and still records a timing when a stage raises."""

from types import SimpleNamespace

import pytest

from yuanji_computer_use.actions import Context
from yuanji_computer_use.agents.middleware import SessionReport, StepFiles, StepLog, StepPipeline, Timing
from yuanji_computer_use.agents.session import Session, StepContext
from yuanji_computer_use.agents.strategies import ActStrategy, DecideStrategy, MemoryStrategy, PerceiveStrategy
from yuanji_computer_use.decide import Decision
from yuanji_computer_use.runner import RunConfig


def _decision() -> Decision:
    kind = SimpleNamespace(choice="done", confidence=0.9, probabilities={"done": 0.9})
    return Decision(kind=kind, item=None, site=SimpleNamespace(choice="none", confidence=1.0, probabilities={"none": 1.0}))


def _ctx() -> Context:
    return Context(goal="toggle it", browser="Google Chrome", email=None, typesafe=None, writer=None, history=[])


class _Open:
    def run(self, cfg, ctx, session, sc, log):
        sc.screen = sc.screen
        sc.prefix = cfg.out / "step-001"
        sc.tried = []
        sc.keep_going = True


class _Choose:
    def __init__(self, decision):
        self.decision = decision

    def run(self, cfg, ctx, session, sc, log):
        sc.decision = self.decision


class _Do:
    def run(self, cfg, ctx, session, sc, log):
        sc.what = "clicked 'TUN'"


class _Remember:
    def run(self, session, sc, log):
        session.history.append(sc.what)


class _Boom:
    def run(self, cfg, ctx, session, sc, log):
        raise RuntimeError("stage failed")


def test_one_step_writes_the_five_files_and_reports_the_action(monkeypatch, tmp_path, screen):
    reported = []
    monkeypatch.setattr("yuanji_computer_use.agents.middleware.report_action", reported.append)
    decision = _decision()
    state = Session()
    sc = StepContext(step=1, screen=screen, items=[])
    pipe = StepPipeline(_Open(), _Choose(decision), _Do(), _Remember())
    cfg = RunConfig(goal="toggle it", out=tmp_path)

    assert pipe.run(cfg, _ctx(), state, sc, lambda _line: None) is True

    names = sorted(path.name for path in tmp_path.iterdir())
    assert names == [
        "step-001-answers.json",
        "step-001-payload.txt",
        "step-001-raw.png",
        "step-001-sent.png",
        "step-001.png",
    ]
    assert reported == ["clicked 'TUN'"]
    assert state.timings and "total" in state.timings[0]


def test_a_stage_that_raises_still_records_the_timing(tmp_path, screen):
    state = Session()
    sc = StepContext(step=1, screen=screen, items=[])
    pipe = StepPipeline(_Boom(), _Choose(_decision()), ActStrategy(), MemoryStrategy())
    cfg = RunConfig(goal="toggle it", out=tmp_path)

    with pytest.raises(RuntimeError, match="stage failed"):
        pipe.run(cfg, _ctx(), state, sc, lambda _line: None)

    assert state.timings and "total" in state.timings[0]
    assert list(tmp_path.glob("step-*")) == []


def test_the_default_pipeline_is_the_four_stages_and_four_middlewares():
    pipe = StepPipeline(PerceiveStrategy(), DecideStrategy(), ActStrategy(), MemoryStrategy())
    assert [type(mw) for mw in pipe.middleware] == [Timing, StepFiles, SessionReport, StepLog]
