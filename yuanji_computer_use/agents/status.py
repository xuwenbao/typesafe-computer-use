"""一次运行的状态，以及谁可以交给谁。

`Status` 只回答下一步该哪个角色来。为什么停，仍写在 `Session.outcome` 里。
这些边对应 `run`、`hand_off`、`wants_driver`。
"""

from __future__ import annotations

from enum import Enum


class Status(Enum):
    HOST = "host"  # 把目标应用调到前台
    POLICY = "policy"  # 分类器走一步
    WRITER = "writer"  # 分类器停下后的交回
    DRIVER = "driver"  # 写手判定还没完成、分类器也没路时接管
    FINAL_ANSWER = "final_answer"  # 接管结束后再让写手看最后一屏
    FINISH = "finish"  # 运行结束，主循环退出


# 每条边是一次合法交接。主循环发现表外的转移会直接报错。
TRANSITIONS: dict[Status, frozenset[Status]] = {
    # 宿主做完，总是进入分类器。
    Status.HOST: frozenset({Status.POLICY}),
    # 继续下一步；停下后交给写手；演练或截屏超时直接结束。
    Status.POLICY: frozenset({Status.POLICY, Status.WRITER, Status.FINISH}),
    # 写手给了焦点就回到分类器；要接管就交给接管；否则结束。
    Status.WRITER: frozenset({Status.POLICY, Status.DRIVER, Status.FINISH}),
    # 接管跑完再作答。模块导入失败则结束，分类器已经给出的回答仍留在 run.json。
    Status.DRIVER: frozenset({Status.FINAL_ANSWER, Status.FINISH}),
    # 最终回答之后不再交回分类器。
    Status.FINAL_ANSWER: frozenset({Status.FINISH}),
}


def allows(source: Status, target: Status) -> bool:
    """`source` 的角色结束后，能不能交到 `target`。没有出边的状态（比如结束）交不出去。"""
    return target in TRANSITIONS.get(source, frozenset())
