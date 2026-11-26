"""角色的约定、登记和按名字创建。

接管的模块只在 `create("driver")` 时才导入，而且那个模块本身不导入模型 SDK。
SDK 要等接管真正开始、`handle` 去调用它时才加载。
"""

from __future__ import annotations

import importlib
from typing import Protocol

from .session import Session
from .status import Status

_registry: dict[str, type] = {}

# 名字到模块。创建时才导入，所以仅仅 import 这个文件不会带上任何一个角色。
_MODULES = {
    "host": "yuanji_computer_use.agents.host",
    "policy": "yuanji_computer_use.agents.policy",
    "writer": "yuanji_computer_use.agents.writer",
    "driver": "yuanji_computer_use.agents.driver",
}


class BaseAgent:
    """角色共用的会话绑定。历史和蒙板只在这里写一次。"""

    session: Session
    cfg: object
    ctx: object
    log: object

    def bind(self, session: Session) -> None:
        self.session = session
        self.cfg = session.cfg
        self.ctx = session.ctx
        self.log = session.log

    def report(self, line: str) -> None:
        from ..platforms.windows_session import report_action

        report_action(line)

    def say(self, line: str, *, remember: bool = True) -> None:
        """写进运行日志。`remember` 时同时留下给后面的角色，并送到蒙板。"""
        self.log(line)
        if remember:
            self.session.history.append(line)
        self.report(line)

    def remember(self, line: str) -> None:
        """记进历史并送到蒙板。调用方已经用自己的措辞写过日志。"""
        self.session.history.append(line)
        self.report(line)


class Agent(Protocol):
    name: str

    def handle(self, session: Session) -> Status:
        """做这个角色的一份工作，返回下一个状态。"""


def register(name: str):
    """把一个角色类登记在 `name` 下。重复登记以最后一次为准。"""

    def decorate(cls):
        _registry[name] = cls
        cls.name = name
        return cls

    return decorate


class AgentFactory:
    @staticmethod
    def create(name: str) -> Agent:
        """按名字建一个角色。名字不存在就报 KeyError。"""
        if name not in _registry:
            module = _MODULES.get(name)
            if module is None:
                raise KeyError(name)
            importlib.import_module(module)
        if name not in _registry:
            raise KeyError(name)
        return _registry[name]()
