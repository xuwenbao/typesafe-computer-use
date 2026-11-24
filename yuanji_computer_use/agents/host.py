"""宿主：把目标应用放到前台。真正的选择仍在 `host.prepare`，这里只是运行循环里的一个角色。"""

from __future__ import annotations

from .base import register
from .status import Status


@register("host")
class HostAgent:
    def handle(self, session) -> Status:
        from ..host import prepare

        prepare(session.cfg, session.ctx, session, session.log)
        return Status.POLICY
