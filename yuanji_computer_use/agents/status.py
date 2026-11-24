"""一次运行的状态，以及谁可以交给谁。

`Status` 只回答下一步该哪个角色来。为什么停，仍写在 `Session.outcome` 里。
阶段一的十条边对应今天 `run`、`hand_off`、`wants_driver` 里的分支。
"""

from __future__ import annotations

from enum import Enum


class Status(Enum):
    HOST = "host"
    POLICY = "policy"
    WRITER = "writer"
    DRIVER = "driver"
    FINAL_ANSWER = "final_answer"
    FINISH = "finish"


# 每个状态可以转到的下一个状态。不在表里的转移是程序错误。
TRANSITIONS: dict[Status, frozenset[Status]] = {
    Status.HOST: frozenset({Status.POLICY}),
    Status.POLICY: frozenset({Status.POLICY, Status.WRITER, Status.FINISH}),
    Status.WRITER: frozenset({Status.POLICY, Status.DRIVER, Status.FINISH}),
    Status.DRIVER: frozenset({Status.FINAL_ANSWER, Status.FINISH}),
    Status.FINAL_ANSWER: frozenset({Status.FINISH}),
}


def allows(source: Status, target: Status) -> bool:
    """`source` 的角色结束后，能不能交到 `target`。没有出边的状态（比如结束）交不出去。"""
    return target in TRANSITIONS.get(source, frozenset())
