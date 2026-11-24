"""规划这个角色。看当前画面，要一份计划，然后交回分类器去执行。

读不成的计划不重试。检查不通过的计划带着理由再写一次，这次计入规划次数，不算重规划。
子目标按剩余名额截断，超出的不再执行。
"""

from __future__ import annotations

import json
import time

from ..config import settings
from .base import register
from .planner import Plan, check_plan, plan_from
from .status import Status


@register("plan")
class PlanAgent:
    def handle(self, session) -> Status:
        """向写手要一份计划。检查不通过就退回重写一次；仍不行，或名额用完，交给写手。"""
        ctx, log = session.ctx, session.log
        screen, items = _screen(session)
        started = time.perf_counter()
        plan = _compose(session, screen, items, None)
        session.planner_calls += 1
        reasons = _reasons(plan, screen, items)
        # 整次运行的子目标有上限。这一份只留下还没用完的名额。
        room = settings().max_subgoals - session.executed_subgoals
        # 不合格只退回一次。这次重写计入 planner_calls，不计入 replans。
        if reasons and room > 0 and ctx.writer is not None and screen is not None:
            plan = _compose(session, screen, items, reasons)
            session.planner_calls += 1
            reasons = _reasons(plan, screen, items)
        session.planner_seconds += time.perf_counter() - started
        if not plan.subgoals or room <= 0 or reasons:
            # 读不成、重写后仍不合格，或名额已经用完：交给写手。last_plan 仍留着上一份。
            if not plan.subgoals or reasons:
                session.history.append("planner: unusable plan")
                reason = "; ".join(reasons) if reasons else getattr(plan_from, "reason", "")
                log(f"\nplanner: unusable plan ({reason})" if reason else "\nplanner: unusable plan")
            if session.outcome in ("", "crashed"):
                session.outcome = "replanned"
            session.plan = None
            return Status.WRITER
        kept = plan.subgoals[:room]
        session.plan = Plan(goal_state=plan.goal_state, subgoals=tuple(kept))
        # 当前计划执行完会被清掉。last_plan 留下来，运行结束时写进 run.json。
        session.last_plan = session.plan
        session.subgoal = 0
        # 上一份计划留下的「连续没变化」不能跟着新计划走，否则这一步还没动手就又停了。
        # last 不动：下一张画面仍和动作前的那张比。
        session.idle = 0
        session.repeats = 0
        session.last_press = None
        _write(session, screen, items, started)
        log(f"\nplanner: {len(kept)} subgoal{'s' if len(kept) != 1 else ''}")
        return Status.POLICY


def _compose(session, screen, items, rejected: list[str] | None):
    """向写手要一份计划。没有写手或没有画面时，返回空计划。"""
    if session.ctx.writer is None or screen is None:
        return plan_from({})
    from ..writer import compose_plan
    from .strategies import banned_offscreen

    return compose_plan(
        session.ctx.writer,
        session.cfg.goal,
        screen,
        items,
        session.history,
        session.ctx.guidance,
        session.verdicts,
        rejected=rejected,
        banned_offscreen=banned_offscreen(session) or None,
    )


def _reasons(plan: Plan, screen, items) -> list[str]:
    """空计划是读不成，不在这里再列原因。有子目标才检查能不能执行。"""
    if not plan.subgoals or screen is None:
        return []
    return check_plan(plan, items, list(getattr(screen, "offscreen", []) or []))


def _screen(session):
    """用这一步已经看过的画面。还没有，就现截一张。"""
    if session.view is not None:
        return session.view
    from .strategies import _runner

    cfg, ctx = session.cfg, session.ctx
    screen = _runner().capture(cfg.image, cfg.app, cfg.url, ctx.browser, {})
    items = _runner().perceive(screen, settings().max_options, cfg.goal, {})
    session.view = (screen, items)
    return screen, items


def _write(session, screen, items, started: float) -> None:
    """step-NNN-plan.json：这次规划看见了什么、给出了什么、花了多久。"""
    payload = {
        "goal": session.cfg.goal,
        "items": len(items),
        "app": screen.app,
        "previous_subgoals": list(session.verdicts),
        "plan": dump_plan(session.plan),
        "seconds": round(time.perf_counter() - started, 3),
    }
    path = session.cfg.out / f"step-{session.step:03d}-plan.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def dump_plan(plan: Plan | None) -> dict | None:
    """计划写成 run.json 和步骤文件里的那一份。没有计划就是 null。"""
    if plan is None:
        return None

    def space(item) -> dict:
        out = {}
        if item.role:
            out["role"] = item.role
        if item.controls:
            out["controls"] = list(item.controls)
        if item.label:
            out["label"] = item.label
        return out

    def predicate(item) -> dict:
        out = {}
        for name in ("checked", "expanded", "current", "exists"):
            value = getattr(item, name)
            if value is not None:
                out[name] = value
        if item.label:
            out["label"] = item.label
        return out

    return {
        "goal_state": {"space": space(plan.goal_state.space), "require": predicate(plan.goal_state.require)},
        "subgoals": [
            {
                "do": sub.do,
                "space": space(sub.space),
                "verify": predicate(sub.verify),
                "on_fail": sub.on_fail,
                **({"key": sub.key} if sub.key else {}),
            }
            for sub in plan.subgoals
        ],
    }
