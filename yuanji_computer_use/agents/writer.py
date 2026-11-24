"""写手这个角色。模块路径是 `agents.writer`，和包外的 `writer`（生成回答的函数）不是同一个文件。

交回、要不要接管、以及接管结束后的最终回答都从这里走。
"""

from __future__ import annotations

from dataclasses import replace

from .base import register
from .status import Status


@register("writer")
class WriterAgent:
    def handle(self, session) -> Status:
        """分类器停下后的一次交回。继续就回到分类器；要接管就交给接管；否则结束。"""
        from ..runner import hand_off, wants_driver

        cfg, ctx, log = session.cfg, session.ctx, session.log
        if hand_off(cfg, ctx, session, session.step, log):
            # 写手看过画面，说目标还没完成。留下的断言不能把下一步再判成完成，否则这个提示做不出动作。
            session.plan = None
            session.goal_state_held = False
            session.ctx = replace(ctx, guidance=session.guidance)
            return Status.POLICY
        if wants_driver(cfg, ctx, session, log):
            return Status.DRIVER
        return Status.FINISH

    def conclude(self, session) -> Status:
        """接管已经跑完。写手看最后一屏作答，然后结束，不再交回分类器。"""
        from ..runner import answer_last

        answer_last(session.cfg, session.ctx, session, session.log)
        return Status.FINISH
