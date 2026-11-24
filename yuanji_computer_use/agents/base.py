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
    "plan": "yuanji_computer_use.agents.plan",
    "writer": "yuanji_computer_use.agents.writer",
    "driver": "yuanji_computer_use.agents.driver",
}


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
