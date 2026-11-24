"""接管这个角色。导入本模块不会加载模型 SDK；`handle` 真正开始接管时才导入 `driver`。"""

from __future__ import annotations

from .base import register
from .status import Status


@register("driver")
class DriverAgent:
    def handle(self, session) -> Status:
        cfg, ctx, log = session.cfg, session.ctx, session.log
        try:
            from ..runner import _driver

            takeover = _driver()
        except (ImportError, OSError) as exc:
            # 冻结包里可能缺 SDK 要读的数据文件。分类器已经答过了，不能因为导入失败把这次运行丢掉。
            session.driver_error = str(exc)
            log(f"takeover unavailable: {exc}")
            return Status.FINISH
        if takeover.take_over(cfg, ctx, session, log, session.step + 1):
            return Status.FINAL_ANSWER
        return Status.FINISH
