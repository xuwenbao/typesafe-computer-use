"""分类器：一步一决策。有计划时按当前子目标装配这一步；停下时还能规划就先去规划。"""

from __future__ import annotations

from ..config import settings
from .base import register
from .status import Status
from .strategies import run_step

# 这几种停下说明分类器自己走不下去。done 和步数用完不在里面，它们直接交给写手。
_PLAN_ON = frozenset({"low confidence", "nothing helps", "stalled"})


@register("policy")
class PolicyAgent:
    def handle(self, session) -> Status:
        """走一步分类器，再按这一步留下的 followup 决定交给谁。

        上一步可能已经写好去向：目标成立、子目标跳过，或要重规划。那种情况不再截屏。
        这一步自己停下时，低置信、没有可用动作、卡住，还能规划就先规划。
        分类器说完成，或步数用完，直接交给写手。
        """
        cfg, log = session.cfg, session.log
        # 上一步收尾时写的去向。用过即清，避免下一步又走同一条。
        pending = session.followup
        session.followup = ""
        if pending == "writer":
            return Status.WRITER
        if pending == "plan":
            return _to_plan(session)
        step = session.step + 1
        # 步数用完不再交回分类器。写手看到 step limit，也不会再把运行交回来。
        if step > cfg.steps:
            log(f"\nstopped after {cfg.steps} steps")
            session.outcome = "step limit"
            return Status.WRITER
        session.step = step
        keep = run_step(cfg, session.ctx, session, step, log)
        # 这一步内部决定的去向，优先于“继续走”或“按停下原因交接”。
        pending = session.followup
        session.followup = ""
        if pending == "plan":
            return _to_plan(session)
        if pending == "writer":
            return Status.WRITER
        if pending == "policy":
            return Status.POLICY
        if keep:
            return Status.POLICY
        # done 不在 _PLAN_ON 里，落到下面，交给写手。
        if session.outcome in _PLAN_ON:
            return _to_plan(session)
        return Status.WRITER


def _to_plan(session) -> Status:
    """去规划。第一次不算重规划；重规划用完了就交给写手，并丢掉还没执行的子目标。"""
    cfg = settings()
    # 规划关掉。低置信、卡住这些停下直接交给写手。
    if not cfg.planner:
        return Status.WRITER
    if session.planner_calls >= 1:
        if session.replans >= cfg.max_replans:
            if session.outcome in ("", "crashed"):
                session.outcome = "replanned"
            return Status.WRITER
        session.replans += 1
        session.plan = None
    return Status.PLAN
