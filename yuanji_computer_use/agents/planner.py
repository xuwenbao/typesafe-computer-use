"""一份计划：目标状态、最多几个子目标，以及子目标怎么在屏幕上对上号。

匹配和断言都是代码做的，不再问模型屏幕上写了什么。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..models import ROLE_WORDS, Item
from ..uia_patterns import PATTERN_IDS

# 分类器已有的动作，再加 ensure：只断言，不操作。
DO = frozenset({"click", "open", "press_offscreen", "scroll", "press_enter", "press_escape", "go_back", "wait", "ensure"})
MAX_PLAN_SUBGOALS = 3


@dataclass(frozen=True)
class Space:
    """子目标要找的那一类控件。角色和操作方式满足一个即可，标签两边都看。"""

    role: str | None = None
    controls: tuple[str, ...] = ()
    label: str | None = None

    def matches(self, item: Item, items: list[Item]) -> bool:
        if not _label_ok(self.label, item, items):
            return False
        role_ok = self.role is None or item.role == self.role
        controls_ok = _controls_ok(self.controls, item, items)
        if self.role and self.controls:
            return role_ok or controls_ok
        if self.role:
            return role_ok
        if self.controls:
            return controls_ok
        return True


@dataclass(frozen=True)
class Predicate:
    """对匹配到的条目读状态。结果是 true、false 或 unknown。"""

    checked: bool | None = None
    expanded: bool | None = None
    current: str | None = None
    exists: bool | None = None

    def evaluate(self, matches: list[Item]) -> str:
        """没有匹配条目时，exists 为假，其余条件为未知。文字条目和没报告的状态也是未知。"""
        verdicts: list[bool | None] = []
        if self.exists is not None:
            verdicts.append(bool(matches) is self.exists)
        if not matches:
            if self.checked is None and self.expanded is None and self.current is None:
                return _combine(verdicts)
            return "unknown" if self.exists is None else _combine([*verdicts, None])
        for item in matches:
            for name in ("checked", "expanded", "current"):
                wanted = getattr(self, name)
                if wanted is None:
                    continue
                actual = getattr(item, name)
                if not item.from_ax or actual is None:
                    verdicts.append(None)
                else:
                    verdicts.append(actual == wanted)
        return _combine(verdicts)


@dataclass(frozen=True)
class Subgoal:
    do: str
    space: Space = field(default_factory=Space)
    verify: Predicate = field(default_factory=Predicate)
    on_fail: str = "widen"


@dataclass(frozen=True)
class GoalState:
    space: Space = field(default_factory=Space)
    require: Predicate = field(default_factory=Predicate)


@dataclass(frozen=True)
class Plan:
    goal_state: GoalState = field(default_factory=GoalState)
    subgoals: tuple[Subgoal, ...] = ()


def plan_from(data: dict) -> Plan:
    """把模型的回复收成计划。不合规格就返回空计划，调用方不再重试。"""
    try:
        return Plan(goal_state=_goal(data.get("goal_state")), subgoals=_subgoals(data.get("subgoals")))
    except (TypeError, ValueError, re.error):
        return Plan()


def _goal(raw: object) -> GoalState:
    if not isinstance(raw, dict):
        raise ValueError
    return GoalState(space=_space(raw.get("space")), require=_predicate(raw.get("require")))


def _subgoals(raw: object) -> tuple[Subgoal, ...]:
    if not isinstance(raw, list) or len(raw) > MAX_PLAN_SUBGOALS:
        raise ValueError
    return tuple(_one_subgoal(item) for item in raw)


def _one_subgoal(raw: object) -> Subgoal:
    if not isinstance(raw, dict) or raw.get("do") not in DO:
        raise ValueError
    return Subgoal(
        do=str(raw["do"]),
        space=_space(raw.get("space")),
        verify=_predicate(raw.get("verify")),
        on_fail=raw.get("on_fail") if isinstance(raw.get("on_fail"), str) else "widen",
    )


def _space(raw: object) -> Space:
    if raw is None:
        return Space()
    if not isinstance(raw, dict):
        raise ValueError
    role = raw.get("role")
    if role is not None and role not in ROLE_WORDS.values():
        raise ValueError
    controls = raw.get("controls") or []
    if not isinstance(controls, list) or any(name not in PATTERN_IDS for name in controls):
        raise ValueError
    label = raw.get("label")
    if label is not None:
        if not isinstance(label, str):
            raise ValueError
        re.compile(label)
    return Space(role=role, controls=tuple(controls), label=label)


def _predicate(raw: object) -> Predicate:
    if raw is None:
        return Predicate()
    if not isinstance(raw, dict) or any(key not in {"checked", "expanded", "current", "exists"} for key in raw):
        raise ValueError
    checked = _bool(raw.get("checked"))
    expanded = _bool(raw.get("expanded"))
    exists = _bool(raw.get("exists"))
    current = raw.get("current")
    if current is not None and not isinstance(current, str):
        raise ValueError
    return Predicate(checked=checked, expanded=expanded, current=current, exists=exists)


def _bool(value: object) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError
    return value


def matching(space: Space, items: list[Item]) -> list[Item]:
    """按空间留下的条目，顺序和编号都不变。"""
    return [item for item in items if space.matches(item, items)]


def _label_ok(pattern: str | None, item: Item, items: list[Item]) -> bool:
    if not pattern:
        return True
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error:
        return False
    blob = " ".join([item.text, *_beside(item, items)])
    return compiled.search(blob) is not None


def _beside(item: Item, items: list[Item]) -> list[str]:
    """同一行上的其他文字。没名字的开关靠这一行被认出来。"""
    half = max(1.0, item.y2 - item.y1) / 2
    return [other.text for other in items if other is not item and abs(other.center[1] - item.center[1]) < half]


def _controls_ok(wanted: tuple[str, ...], item: Item, items: list[Item]) -> bool:
    """这一屏没人报告操作方式时（macOS），操作方式条件算满足。报告了，就得带上其中一种。"""
    if not wanted:
        return True
    if not any(other.controls for other in items):
        return True
    return any(name in item.controls for name in wanted)


def _combine(verdicts: list[bool | None]) -> str:
    if not verdicts:
        return "unknown"
    if any(item is False for item in verdicts):
        return "false"
    if any(item is None for item in verdicts):
        return "unknown"
    return "true"
