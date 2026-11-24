"""一份计划：目标状态、最多几个子目标，以及子目标怎么在屏幕上对上号。

匹配和断言都是代码做的，不再问模型屏幕上写了什么。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..decide import SHORTCUT_KEYS
from ..models import ROLE_WORDS, Item
from ..uia_patterns import PATTERN_IDS

# 分类器已有的动作，再加 ensure：只断言，不操作。shortcut 还带一个按键。
DO = frozenset(
    {
        "click",
        "open",
        "press_offscreen",
        "scroll",
        "press_enter",
        "press_escape",
        "go_back",
        "wait",
        "ensure",
        "shortcut",
    }
)
MAX_PLAN_SUBGOALS = 3


@dataclass(frozen=True)
class Space:
    """子目标要找的那一类控件。角色和操作方式满足一个即可，标签两边都看。"""

    role: str | None = None
    controls: tuple[str, ...] = ()
    label: str | None = None

    def matches(self, item: Item, items: list[Item]) -> bool:
        # 标签必须对上。角色和操作方式同时写了时，满足一个即可：按钮靠 role，没名字的开关靠 toggle。
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
    """对匹配到的条目读状态。结果是 true、false 或 unknown。

    `label` 有值时，读的是另一个控件，不再读子目标自己的范围。
    """

    checked: bool | None = None
    expanded: bool | None = None
    current: str | None = None
    exists: bool | None = None
    label: str | None = None

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
    key: str = ""  # shortcut 才有，例如 v。其余动作为空


@dataclass(frozen=True)
class GoalState:
    space: Space = field(default_factory=Space)
    require: Predicate = field(default_factory=Predicate)


@dataclass(frozen=True)
class Plan:
    goal_state: GoalState = field(default_factory=GoalState)
    subgoals: tuple[Subgoal, ...] = ()


def plan_from(data: dict) -> Plan:
    """把模型的回复收成计划。不合规格就返回空计划，调用方不再重试。

    `plan_from.reason` 是这次没收下的原因，空字符串表示收下来了。日志用它，历史里仍只记
    `planner: unusable plan`。
    """
    try:
        plan = Plan(goal_state=_goal(data.get("goal_state")), subgoals=_subgoals(data.get("subgoals")))
    except (TypeError, ValueError, re.error) as error:
        plan_from.reason = str(error) or error.__class__.__name__
        return Plan()
    plan_from.reason = ""
    return plan


plan_from.reason = ""


def _goal(raw: object) -> GoalState:
    """不是对象就当成还没有可检查的目标状态。子目标仍留下，不再因此丢掉整份计划。"""
    if not isinstance(raw, dict):
        return GoalState()
    return GoalState(space=_space(raw.get("space")), require=_predicate(raw.get("require")))


def _subgoals(raw: object) -> tuple[Subgoal, ...]:
    if not isinstance(raw, list) or len(raw) > MAX_PLAN_SUBGOALS:
        raise ValueError("subgoals must be a list of at most 3")
    return tuple(_one_subgoal(item) for item in raw)


def _one_subgoal(raw: object) -> Subgoal:
    if not isinstance(raw, dict) or raw.get("do") not in DO:
        raise ValueError(f"do {raw.get('do') if isinstance(raw, dict) else raw!r} is not an action")
    key = ""
    if raw.get("do") == "shortcut":
        key = raw.get("key")
        if not isinstance(key, str) or key.lower() not in SHORTCUT_KEYS:
            raise ValueError(f"shortcut key {key!r} is not one of {', '.join(SHORTCUT_KEYS)}")
        key = key.lower()
    return Subgoal(
        do=str(raw["do"]),
        space=_space(raw.get("space")),
        verify=_predicate(raw.get("verify")),
        on_fail=raw.get("on_fail") if isinstance(raw.get("on_fail"), str) else "widen",
        key=key,
    )


def subgoal_packet(sub: Subgoal) -> dict:
    """分类器状态包里的当前子目标：做什么、找谁、做成什么样。没有计划时不要调用。"""
    verify = {}
    for name in ("checked", "expanded", "current", "exists"):
        value = getattr(sub.verify, name)
        if value is not None:
            verify[name] = value
    if sub.verify.label:
        verify["label"] = sub.verify.label
    packet = {"do": sub.do, "label": sub.space.label, "role": sub.space.role, "verify": verify}
    if sub.key:
        packet["key"] = sub.key
    return packet


def _space(raw: object) -> Space:
    if raw is None:
        return Space()
    if not isinstance(raw, dict):
        raise ValueError
    role = raw.get("role")
    if role == "":
        role = None
    if role is not None and role not in ROLE_WORDS.values():
        role = None
    controls = raw.get("controls") or []
    if isinstance(controls, str):
        controls = [part.strip() for part in controls.split(",") if part.strip()]
    if not isinstance(controls, list):
        controls = []
    controls = [name for name in controls if name in PATTERN_IDS]
    label = raw.get("label")
    if label is not None:
        if not isinstance(label, str):
            raise ValueError("label is not a string")
        re.compile(label)
    return Space(role=role, controls=tuple(controls), label=label)


_STATE_KEYS = frozenset({"checked", "expanded", "current", "exists"})


def _predicate(raw: object) -> Predicate:
    """读一个状态条件。模型常把条件写成别的形状，认不出的部分丢掉，不因此丢掉整份计划。"""
    if raw is None or raw == "":
        return Predicate()
    if isinstance(raw, str):
        raw = _predicate_words(raw)
    if not isinstance(raw, dict):
        return Predicate()
    label = _predicate_label(raw.get("label"))
    if not any(key in raw for key in _STATE_KEYS):
        raw = _named_state(raw)
    checked = _bool(raw.get("checked"))
    expanded = _bool(raw.get("expanded"))
    exists = _bool(raw.get("exists"))
    current = raw.get("current")
    if current is not None and not isinstance(current, str):
        current = None
    return Predicate(checked=checked, expanded=expanded, current=current, exists=exists, label=label)


def _predicate_label(raw: object) -> str | None:
    """断言要读的另一个控件。不是字符串就丢掉；正则编译不了，整份计划不要。"""
    if not isinstance(raw, str) or not raw:
        return None
    re.compile(raw)
    return raw


def _named_state(raw: dict) -> dict:
    """`{"kind": "exists", "expect": true, "target": "..."}` 收成真正的条件。target 只是说明，不参与断言。"""
    kind = str(raw.get("kind") or raw.get("name") or "").strip().lower()
    if kind not in _STATE_KEYS:
        return {}
    expect = raw.get("expect", raw.get("value"))
    if kind == "current":
        return {"current": expect} if isinstance(expect, str) else {}
    flag = _bool(expect)
    if kind == "exists":
        return {"exists": True if flag is None else flag}
    return {kind: flag} if flag is not None else {}


def _predicate_words(text: str) -> dict:
    """一个名字，或 `checked: false` 这种一行。只有名字、没有真假时不当成真，避免把“关掉”读成打开。"""
    parts = re.split(r"[:=]", text, maxsplit=1)
    name = parts[0].strip().lower()
    if name not in {"checked", "expanded", "current", "exists"}:
        return {}
    if len(parts) == 1:
        return {"exists": True} if name == "exists" else {}
    value = parts[1].strip()
    if name == "current":
        return {"current": value}
    flag = {"true": True, "yes": True, "on": True, "false": False, "no": False, "off": False}.get(value.lower())
    if flag is None:
        return {}
    return {name: flag}


def _bool(value: object) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return {"true": True, "yes": True, "on": True, "false": False, "no": False, "off": False}.get(value.strip().lower())
    return None


def evaluate(predicate: Predicate, space: Space, items: list[Item]) -> str:
    """在当前画面上对一个条件给出 true、false 或 unknown。

    条件自己写了 label，就去读那个控件，不再读调用方传来的范围。
    """
    if predicate.label:
        space = Space(label=predicate.label)
    return predicate.evaluate(matching(space, items))


_CLICKS = frozenset({"click", "open", "press_offscreen"})


def offscreen_matching(pattern: str | None, nodes: list) -> list:
    """屏外控件里，标签对得上这个正则的那些。"""
    if not pattern:
        return []
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error:
        return []
    return [node for node in nodes if compiled.search(getattr(node, "label", "") or "")]


def check_plan(plan: Plan, items: list[Item], offscreen: list) -> list[str]:
    """计划里检查不了的地方。空列表表示可以执行。

    目标状态必须能在控件上读出来。点击、打开、屏外点击要有标签，并且不能只断言
    「被点的那个控件存在」。第一个子目标的标签必须对得上当前画面：屏外点击对屏外控件，其余对屏上条目。
    """
    reasons: list[str] = []
    require = plan.goal_state.require
    labeled = require.label or plan.goal_state.space.label
    readable = require.checked is not None or require.expanded is not None or require.current is not None
    exists_labeled = require.exists is not None and bool(labeled)
    if not readable and not exists_labeled:
        reasons.append("goal_state.require must be checked, expanded, current, or exists with a label")
    for index, sub in enumerate(plan.subgoals):
        if sub.do not in _CLICKS:
            continue
        if not sub.space.label:
            reasons.append(f"subgoal {index + 1} ({sub.do}) needs a label")
        if _only_own_exists(sub):
            reasons.append(f"subgoal {index + 1} ({sub.do}) only asserts that its own control exists")
    if plan.subgoals and plan.subgoals[0].space.label and not _on_this_screen(plan.subgoals[0], items, offscreen):
        label = plan.subgoals[0].space.label
        reasons.append(f"the first subgoal label {label!r} matches nothing on this screen")
    return reasons


def _only_own_exists(sub: Subgoal) -> bool:
    """只有「自己存在」这一条。exists 为假、读另一个标签、或带状态条件，都算能检查。"""
    verify = sub.verify
    if verify.exists is not True:
        return False
    if verify.checked is not None or verify.expanded is not None or verify.current is not None:
        return False
    return not (verify.label and verify.label != sub.space.label)


def _on_this_screen(sub: Subgoal, items: list[Item], offscreen: list) -> bool:
    if sub.do == "press_offscreen":
        return bool(offscreen_matching(sub.space.label, offscreen))
    return bool(matching(Space(label=sub.space.label or ""), items))


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
    """有一条为假就是假；没有假、但有一条未知，就是未知；全真才是真。空条件是未知。"""
    if not verdicts:
        return "unknown"
    if any(item is False for item in verdicts):
        return "false"
    if any(item is None for item in verdicts):
        return "unknown"
    return "true"
