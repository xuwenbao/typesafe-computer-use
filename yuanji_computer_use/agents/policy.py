"""分类器：一步一决策。停下就交给写手。"""

from __future__ import annotations

from .base import register
from .status import Status
from .strategies import run_step


@register("policy")
class PolicyAgent:
    def handle(self, session) -> Status:
        """走一步分类器。还要继续就再走一步；停下，或步数用完，交给写手。"""
        cfg, log = session.cfg, session.log
        step = session.step + 1
        # 步数用完不再交回分类器。写手看到 step limit，也不会再把运行交回来。
        if step > cfg.steps:
            log(f"\nstopped after {cfg.steps} steps")
            session.outcome = "step limit"
            return Status.WRITER
        session.step = step
        if run_step(cfg, session.ctx, session, step, log):
            return Status.POLICY
        return Status.WRITER
