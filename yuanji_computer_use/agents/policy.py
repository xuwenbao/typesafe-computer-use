"""分类器：一步一决策。步数用完由这里判定，然后交给写手，不再自己继续。"""

from __future__ import annotations

from .base import register
from .status import Status
from .strategies import run_step


@register("policy")
class PolicyAgent:
    def handle(self, session) -> Status:
        cfg, log = session.cfg, session.log
        step = session.step + 1
        if step > cfg.steps:
            log(f"\nstopped after {cfg.steps} steps")
            session.outcome = "step limit"
            return Status.WRITER
        session.step = step
        if run_step(cfg, session.ctx, session, step, log):
            return Status.POLICY
        return Status.WRITER
