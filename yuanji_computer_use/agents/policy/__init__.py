"""分类器：一步一决策。停下就交给写手。"""

from __future__ import annotations

from ..base import BaseAgent, register
from ..session import StepContext
from ..status import Status
from .stages import ActStrategy, DecideStrategy, MemoryStrategy, PerceiveStrategy


@register("policy")
class PolicyAgent(BaseAgent):
    def handle(self, session) -> Status:
        """走一步分类器。还要继续就再走一步；停下，或步数用完，交给写手。"""
        self.bind(session)
        cfg, log = self.cfg, self.log
        step = session.step + 1
        # 步数用完不再交回分类器。写手看到 step limit，也不会再把运行交回来。
        if step > cfg.steps:
            log(f"\nstopped after {cfg.steps} steps")
            session.outcome = "step limit"
            return Status.WRITER
        session.step = step
        if self.run_step(cfg, session.ctx, session, step, log):
            return Status.POLICY
        return Status.WRITER

    def run_step(self, cfg, ctx, state, step: int, log) -> bool:
        """分类器的一步：感知 → 决策 → 执行 → 记忆。写文件、计时和上报在中间件里。"""
        from .pipeline import StepPipeline

        pipeline = StepPipeline(PerceiveStrategy(), DecideStrategy(), ActStrategy(), MemoryStrategy())
        return pipeline.run(cfg, ctx, state, StepContext(step=step), log)
