"""分类器一步里能单独调用的部分：感知之后的判断、执行、重复检查，以及整步 `run_step`。

四个阶段的类包着这些函数。`run_step` 仍按原来的顺序调用它们，日志和文件的写法不变。
"""

from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace

from ..actions import perform
from ..bound import CaptureTimeout
from ..config import settings
from ..decide import Decision, decide, merged_margin, offscreen_records
from ..models import Item, Screen, same_screen, signature
from ..platform_adapter import desktop
from ..report import Log, ax_count, top
from ..timing import phase
from .session import Session, StepContext


def _runner():
    """用到时再取主循环模块。截屏和识别都从它上面找，测试打的补丁才盖得住。"""
    from .. import runner

    return runner


MAX_IDLE = 3  # 连续 3 次动作后屏幕都没变化，就判定卡住
MAX_REPEATS = 2  # 在同一个屏幕上连续 2 次重复以前做过的动作，就判定卡住
UNCHANGED = "; the screen did not change"  # 动作没让屏幕变化时，追加到历史末尾
UNMARKED = ("wait", "type_text", "type_email")  # 这几种动作屏幕没变也不加标记
LEAD = 2.0  # 置信度低于门槛时，第一名至少是第二名的这么多倍才放行
FLOOR_FRACTION = 0.75  # 并且第一名至少达到门槛的这个比例


class PerceiveStrategy:
    """截屏、识别元素，并判断上一个动作有没有让屏幕变化。

    截屏超时，或连续多次没有变化，就把这一步停掉。停在这里时还没有步文件。
    """

    def run(self, cfg, ctx, state: Session, sc: StepContext, log: Log) -> None:
        desktop.check_abort()
        cache = None if cfg.replay else state.parse_cache
        if state.reuse_view and state.view is not None:
            # 上一步的断言已经看过动作之后的画面。这里直接用，避免连拍两张。
            state.reuse_view = False
            screen, items = state.view
        else:
            try:
                with phase(sc.timing, "capture"):
                    screen = _runner().capture(
                        cfg.image, cfg.app, cfg.url, ctx.browser, sc.timing, ahead=cache if cfg.read_ahead else None
                    )
            except CaptureTimeout:
                # 截屏没有在时限内返回。这一步没有画面，不交给写手或接管；收尾仍会写 run.json。
                log("capture timed out")
                state.outcome = "capture timed out"
                sc.keep_going = False
                return
            items = _runner().perceive(screen, settings().max_options, cfg.goal, sc.timing, cache)
            state.view = (screen, items)
        sc.screen = screen
        sc.items = items
        if not screen_moved(state, screen, items, log):
            sc.keep_going = False
            return
        sc.tried = tried_here(state) + list(state.press_tried)
        # 步号补齐三位（001、002…），文件按名字排序就是步骤顺序。文件本身由 StepFiles 写。
        sc.prefix = cfg.out / f"step-{sc.step:03d}"


class DecideStrategy:
    """问分类器选一个动作。它说完成、没办法，或置信度不够时，这一步不再执行。"""

    def run(self, cfg, ctx, state: Session, sc: StepContext, log: Log) -> None:
        # 没有计划时，上一屏没变化的那一次点击也在这里换按法，不再问分类器。
        if _take_alt(state, sc, log):
            return
        decision = _ask(cfg, ctx, state, sc, None)
        _record_decision(state, sc, decision, log)
        if _refuses(cfg, state, decision, log):
            sc.keep_going = False
            return
        margin = _margin(decision, sc.items)
        # 只拦第一次、还没有计划的那一步。分差不够就先规划，这一下不执行。
        if _defer_first_step(state, margin):
            sc.keep_going = False
            state.followup = "plan"
            log(f"  merged margin {margin:.2f} below {settings().plan_skip_margin}; planning instead of acting")


class ActStrategy:
    """执行分类器选的动作。没有 --act，或是演练时，只记下会做什么。"""

    def run(self, cfg, ctx, state: Session, sc: StepContext, log: Log) -> None:
        decision: Decision = sc.decision
        if not cfg.act or cfg.replay:
            log(f"  would do: {decision.chosen}. dry run (pass --act without --image to drive the machine)")
            state.outcome = "dry run"
            sc.keep_going = False
            return
        state.quiet = False
        with phase(sc.timing, "act"):
            sc.what = perform(decision, sc.screen, sc.items, ctx, method=sc.method)
        clipboard = "to the clipboard" in sc.what
        state.quiet = clipboard
        state.view = None
        state.markable = decision.kind.choice not in UNMARKED and not clipboard


class MemoryStrategy:
    """把做过的动作写进历史，并检查是不是在同一屏上重复。"""

    def run(self, state: Session, sc: StepContext, log: Log) -> None:
        state.history.append(sc.what)
        _remember_press(state, sc)
        _remember_refused(state, sc)
        log(f"  did: {sc.what}")
        if repeating(state, sc.what, sc.decision.kind.choice == "wait", log):
            sc.keep_going = False


def run_step(cfg, ctx, state: Session, step: int, log: Log) -> bool:
    """分类器的一步。没有计划时仍是感知 → 决策 → 执行 → 记忆。

    有计划时，决策换成候选空间，执行之后再感知一次并断言。`ensure` 只感知和断言。
    写文件、计时和上报在中间件里。
    """
    from .middleware import StepPipeline

    sub = _current(state)
    if state.plan is None:
        # 没有计划：感知、问分类器、执行、记历史。
        pipeline = StepPipeline(PerceiveStrategy(), DecideStrategy(), ActStrategy(), MemoryStrategy())
    elif sub is not None and sub.do == "ensure":
        # ensure 只核对状态。决策和执行仍挂在流水线上，Gate 会在问分类器之前把这一步停掉。
        pipeline = StepPipeline(
            PerceiveStrategy(), DecideStrategy(), ActStrategy(), MemoryStrategy(), gate=GateStrategy(), verify=VerifyStrategy()
        )
    else:
        # 有子目标：决策换成候选空间，动作之后再断言。子目标用完时，Gate 看到空位就去重规划。
        pipeline = StepPipeline(
            PerceiveStrategy(), SpaceDecide(), ActStrategy(), MemoryStrategy(), gate=GateStrategy(), verify=VerifyStrategy()
        )
    return pipeline.run(cfg, ctx, state, StepContext(step=step), log)


def _lead_covers(decision: Decision, floor: float) -> bool:
    """置信度低于门槛时，判断能不能破例放行（第一名明显领先第二名就放行）。

    点击类动作分两层：先选“做什么”（如点击），再选“点哪个”。
    只有“点哪个”这一层可以破例，“做什么”这一层必须本身就过门槛——连该不该点都没把握时，照样停下。
    """
    if decision.clicking:
        return decision.kind.confidence >= floor and _leads(decision.item, floor)
    if decision.pressing_offscreen:
        return decision.kind.confidence >= floor and _leads(decision.offscreen, floor)
    if decision.shortcutting:
        return decision.kind.confidence >= floor and _leads(decision.key, floor)
    return _leads(decision.kind, floor)


def _leads(answer, floor: float) -> bool:
    """第一名的概率 ≥ 门槛 * FLOOR_FRACTION，并且 ≥ 第二名 * LEAD。"""
    ranked = sorted((getattr(answer, "probabilities", None) or {}).values(), reverse=True)
    if len(ranked) < 2 or ranked[1] <= 0:
        return False
    return ranked[0] >= floor * FLOOR_FRACTION and ranked[0] >= ranked[1] * LEAD


def _refuses(cfg, state: Session, decision: Decision, log: Log) -> bool:
    """分类器自己要求停下，或置信度不够且没有明显领先。停下时写好结局。"""
    if decision.stops:
        log(f"  model says {decision.kind.choice!r}; stopping")
        state.outcome = "done" if decision.kind.choice == "done" else "nothing helps"
        return True
    if decision.confidence < cfg.min_confidence:
        # 屏幕元素很多时，概率会分散到上百个选项上，即使目标很明确，第一名也可能略低于门槛。
        # 所以第一名明显领先时放行；如果前两名差不多（两个都像对的），照样停下——这正是门槛要拦的情况。
        if not _lead_covers(decision, cfg.min_confidence):
            log(f"  confidence {decision.confidence:.2f} below {cfg.min_confidence}; stopping")
            state.outcome = "low confidence"
            return True
        log(f"  confidence {decision.confidence:.2f} below {cfg.min_confidence}, but it leads the next option; continuing")
    return False


def resolve(
    cfg,
    ctx,
    state: Session,
    screen: Screen,
    items: list[Item],
    decision: Decision,
    timing: dict[str, float],
    log: Log,
) -> bool:
    """先检查要不要停下，不停就执行动作。返回 True 表示继续下一步。

    停下的情况：分类器说“完成了”或“没办法”；置信度太低（且不满足破例条件）；演练模式。
    """
    if _refuses(cfg, state, decision, log):
        return False
    sc = StepContext(step=0, timing=timing, screen=screen, items=items, decision=decision)
    ActStrategy().run(cfg, ctx, state, sc, log)
    if sc.what:
        MemoryStrategy().run(state, sc, log)
    return sc.keep_going


def screen_moved(state: Session, screen: Screen, items: list[Item], log: Log) -> bool:
    """检查上一个动作后屏幕有没有变化。连续 MAX_IDLE 次没变化就判定卡住，返回 False。

    做了什么事：
    1. 拿新截图和上一个动作时的截图比较；没变化则 idle 加 1，有变化则清零。
       （没变化可能是：动作被拒、页面还在加载、已经滚到底。偶尔一两次没关系。）
    2. 没变化时，在上一个动作的历史记录末尾加上 UNCHANGED 标记，
       这样分类器和写手下次看历史时就知道这个动作没效果（UNMARKED 里的动作除外）。
    3. 到达没去过的页面时，把卡住次数 stalls 清零（见 stuck）。
    例外：上一个动作是复制粘贴这类快捷键（quiet）时，屏幕本来就不会变，不计数也不加标记。
    """
    now = signature(screen, items)
    if now[:2] not in state.pages:
        state.pages.add(now[:2])
        state.stalls = 0
    acted = len(state.history) > state.compared
    state.compared = len(state.history)
    if state.last is not None:
        unchanged = same_screen(now, state.last)
        if not unchanged:
            state.swapped.clear()
            state.alt = None
            state.last_press = None
            # 屏外控件的拒绝只对动作那一屏有效。画面换了，旧的屏蔽不再适用。
            state.refused_offscreen.clear()
        if not (unchanged and state.quiet):
            state.idle = state.idle + 1 if unchanged else 0
        if unchanged and acted and state.markable:
            state.history[-1] += UNCHANGED
        if unchanged and acted:
            # 有没有计划都一样：同一条目在这一屏只换一次按法。
            _offer_other_press(state, items, log)
    state.last = now
    if state.idle >= MAX_IDLE:
        log(f"  the last {MAX_IDLE} actions changed nothing on screen; stopping")
        state.outcome = "stalled"
        return False
    return True


def tried_here(state: Session) -> list[str]:
    """在当前这个屏幕上已经做过哪些动作（按先后顺序），告诉分类器避免重复。"""
    return [what for seen, what in state.seen if what is not None and state.last is not None and same_screen(seen, state.last)]


def repeating(state: Session, what: str, waiting: bool, log: Log) -> bool:
    """检查是不是在重复旧动作。在同一屏幕上连续 MAX_REPEATS 次做以前做过的动作，就判定卡住，返回 True。

    为什么这算卡住：同一屏幕上做同一个动作，上次的结果就是又回到了这里——
    要么是在两个页面之间来回跳，要么是按钮点了没反应。
    等待（wait）例外：它本来就会反复出现，所以不算重复，也不计入“已尝试”；
    连续等待太多次由 screen_moved 的 MAX_IDLE 来限制。
    """
    if waiting:
        state.seen.append((state.last, None))
        return False
    state.repeats = state.repeats + 1 if what in tried_here(state) else 0
    state.seen.append((state.last, what))
    if state.repeats >= MAX_REPEATS:
        log(f"  {MAX_REPEATS} actions in a row already taken on the same screen; stopping")
        state.outcome = "stalled"
        return True
    return False


def answers(
    decision: Decision, screen: Screen, items: list[Item], timing: dict[str, float], tried: list[str], state: Session
) -> dict:
    """这一步的完整记录（写进 step-NNN-answers.json）：分类器的选择和各选项概率、耗时、卡住计数、识别出的元素等。"""
    return {
        "kind": decision.kind.choice,
        "kind_confidence": decision.kind.confidence,
        "kind_probabilities": decision.kind.probabilities,
        "item": decision.item.choice if decision.item else None,
        "item_confidence": decision.item.confidence if decision.item else None,
        "item_probabilities": decision.item.probabilities if decision.item else None,
        "site": decision.site.choice,
        "site_probabilities": decision.site.probabilities,
        "offscreen": decision.offscreen.choice if decision.offscreen else None,
        "offscreen_probabilities": decision.offscreen.probabilities if decision.offscreen else None,
        "key": decision.key.choice if decision.key else None,
        "key_confidence": decision.key.confidence if decision.key else None,
        "key_probabilities": decision.key.probabilities if decision.key else None,
        "offscreen_controls": offscreen_records(screen.offscreen),
        "chosen": decision.chosen,
        "confidence": decision.confidence,
        "already_tried_on_this_screen": tried,
        "idle_actions": state.idle,
        "repeated_actions": state.repeats,
        "timing": timing,
        "items": [asdict(it) for it in items],
        "field": screen.field.record() if screen.field else None,
        "app": screen.app,
        "url": screen.url,
    }


def classifier_subgoal(state: Session) -> dict | None:
    """当前子目标交给分类器的那一份。没有计划时是 None，状态包里就不出现这一项。"""
    from .planner import subgoal_packet

    sub = _current(state)
    if sub is None:
        return None
    return subgoal_packet(sub)


def _ask(cfg, ctx, state: Session, sc: StepContext, candidates: list[Item] | None) -> Decision:
    """问一次分类器。candidates 是子目标的空间留下的条目，编号仍是全列表里的原编号。

    有计划时把当前子目标的文字一并送去。没有计划时不传，状态包与原来一样。
    """
    with phase(sc.timing, "decide"):
        return decide(
            ctx.typesafe,
            cfg.goal,
            sc.screen,
            sc.items,
            state.history,
            ctx.browser,
            ctx.email,
            sc.tried,
            ctx.guidance,
            sc.picture,
            candidates=candidates,
            subgoal=classifier_subgoal(state),
            banned_offscreen=banned_offscreen(state) or None,
        )


def _record_decision(state: Session, sc: StepContext, decision: Decision, log: Log) -> None:
    """把分类器的选择写进日志。有计划时和没有计划时用同一段，方便对照。"""
    screen, items = sc.screen, sc.items
    sc.decision = decision
    if decision.image_refused is not None:
        log(f"  the screen picture was refused, and the step asked again without it: {decision.image_refused}")
        state.image_refusals.append({"step": sc.step, "reason": decision.image_refused})
    by_index = {str(it.index): it for it in items}
    field_desc = f" field={screen.field.role}:{screen.field.label!r}" if screen.field else ""
    log(
        f"\nstep {sc.step}: app={screen.app!r}{field_desc} url={screen.url!r} items={len(items)} ax={ax_count(items)} "
        f"offscreen={len(screen.offscreen)} kind={decision.kind.choice} ({decision.kind.confidence:.2f}) "
        f"site={decision.site.choice}"
    )
    for key, p in top(decision.kind, 4):
        log(f"  {p:5.2f}  {key}")
    if decision.item is not None:
        log(f"  item ({decision.item.confidence:.2f}):")
        for key, p in top(decision.item, 4):
            log(f"  {p:5.2f}  [{key}] {by_index[key].text!r}")
    if decision.offscreen is not None:
        log(f"  offscreen ({decision.offscreen.confidence:.2f}):")
        for key, p in top(decision.offscreen, 3):
            log(f"  {p:5.2f}  [{key}] {screen.offscreen[int(key)].label!r}")
    if decision.shortcutting:
        log(f"  key ({decision.key.confidence:.2f}): {decision.key.choice}")


def _margin(decision: Decision, items: list[Item]) -> float:
    """合并同义候选之后的分差。只有真正要点的那一层才看条目分差。

    按 Escape、回车、等待并不落到某个条目上。条目题分差再小，也不该因此放弃一个已经明确的按键。
    """
    if decision.clicking and decision.item is not None:
        return merged_margin(decision.item, items)
    if decision.pressing_offscreen and decision.offscreen is not None:
        return _spread(decision.offscreen.probabilities)
    if decision.shortcutting and decision.key is not None:
        return _spread(decision.key.probabilities)
    return _spread(decision.kind.probabilities)


def _spread(probabilities: dict) -> float:
    ranked = sorted(probabilities.values(), reverse=True)
    second = ranked[1] if len(ranked) > 1 else 0.0
    return (ranked[0] if ranked else 0.0) - second


def _defer_first_step(state: Session, margin: float) -> bool:
    """首步分差不够时，只跳过这一下，先规划。之后的步不再用这条。"""
    return (
        settings().planner
        and state.plan is None
        and state.step == 1
        and state.planner_calls == 0
        and margin < settings().plan_skip_margin
    )


def _current(state: Session):
    """当前还没执行的子目标。计划用完或没有计划时是 None。"""
    plan = state.plan
    if plan is None or state.subgoal >= len(plan.subgoals):
        return None
    return plan.subgoals[state.subgoal]


def _levels(space) -> list:
    """候选空间的放宽：角色或控件加标签，然后只按标签。

    有标签就停在这里，不再放到整屏。没有标签时才有整屏那一级。
    """
    from .planner import Space

    out: list = []
    narrowed = bool(space.role or space.controls or space.label)
    if narrowed:
        out.append(space)
    if space.label and (space.role or space.controls):
        out.append(Space(label=space.label))
    if not space.label:
        out.append(None)
    return out


def banned_offscreen(state: Session) -> set[str]:
    """这一屏上被拒绝过的屏外控件。画面换了，screen_moved 会把记录清掉。"""
    if state.last is None:
        return set()
    found: set[str] = set()
    for sig, labels in state.refused_offscreen.items():
        if same_screen(sig, state.last):
            found |= set(labels)
    return found


class SpaceDecide:
    """有计划时的决策。先按子目标的空间收窄候选，分差不够或一个都没有，就逐级放宽。

    有标签时最多放到「只按标签」。这一屏一个都对不上，就丢掉计划去重规划，不再问整屏。
    """

    def run(self, cfg, ctx, state: Session, sc: StepContext, log: Log) -> None:
        from .planner import Space, matching, offscreen_matching

        sub = _current(state)
        space = sub.space if sub is not None else None
        # 屏外点击对的是屏外控件，不在屏上条目里。对不上就重规划；对得上就问这一次。
        if sub is not None and sub.do == "press_offscreen" and space is not None and space.label:
            nodes = sc.screen.offscreen if sc.screen is not None else []
            if not offscreen_matching(space.label, nodes):
                _label_missed(state, sc, log)
                return
            decision = _ask(cfg, ctx, state, sc, None)
            _record_decision(state, sc, decision, log)
            if _refuses(cfg, state, decision, log):
                sc.keep_going = False
            return
        if space is not None and space.label and not matching(Space(label=space.label), sc.items):
            _label_missed(state, sc, log)
            return
        decision = None
        # 从窄到宽问。有标签时没有整屏那一级。某一级没有条目，或分差低于 decide_margin，就放宽再问。
        for narrowed in _levels(space) if space is not None else [None]:
            candidates = None if narrowed is None else matching(narrowed, sc.items)
            if narrowed is not None and not candidates:
                # 这一级一个控件都对不上，问了也没有选项。放宽，这次还没调用分类器。
                state.widened += 1
                log("  space matched nothing; widening")
                continue
            decision = _ask(cfg, ctx, state, sc, candidates)
            if narrowed is None:
                break
            margin = _margin(decision, sc.items)
            if margin >= settings().decide_margin:
                break
            state.widened += 1
            log(f"  merged margin {margin:.2f} below {settings().decide_margin}; widening")
        if decision is None:
            # 有标签却仍然没有决策，不能退到整屏。没有标签的子目标才问全部候选。
            if space is not None and space.label:
                _label_missed(state, sc, log)
                return
            decision = _ask(cfg, ctx, state, sc, None)
        _record_decision(state, sc, decision, log)
        if _refuses(cfg, state, decision, log):
            sc.keep_going = False


def _label_missed(state: Session, sc: StepContext, log: Log) -> None:
    """标签在这一屏对不上。记下未知，丢掉计划，交给重规划。"""
    _note(state, "unknown")
    state.plan = None
    state.followup = "plan"
    sc.keep_going = False
    log("  label matched nothing on this screen; replanning")


class GateStrategy:
    """动作之前核对新画面。目标已成立就结束；只有 ensure 在动作前求值；换按法不再问分类器。"""

    def run(self, cfg, ctx, state: Session, sc: StepContext, log: Log) -> None:
        sub = _current(state)
        if _goal_true(state, sc):
            # 目标状态已经在这张画面上成立。不再动作，交给写手收尾。
            _finish_goal(state, sc, log)
            return
        if sub is None:
            # 计划还在、子目标已经走完。去重规划，不在没有子目标时继续问分类器。
            if state.plan is not None:
                state.followup = "plan"
                sc.keep_going = False
            return
        if sub.do == "ensure":
            # 点击类子目标不在这里求值。目标控件往往一开始就在画面上，提前判成立会把点击跳掉。
            verdict = _subgoal_verdict(state, sc)
            if verdict == "true":
                # 还没动作，条件已经成立。跳过，下一步仍看这张画面。
                _advance(state, "true", log, skipped=True)
                state.followup = "policy"
            elif verdict == "false":
                # ensure 只负责检查。为假就没有按法可换，丢掉计划重来。
                state.followup = "plan"
            else:
                # 未知不当成失败。ensure 没有别的动作可做，进入下一个子目标。
                _advance(state, "unknown", log, skipped=True)
                state.followup = "policy"
            sc.keep_going = False
            return
        # 上一步没改画面，这一步换按法。条目还在就直接按，不再问分类器；条目没了就照常决策。
        _take_alt(state, sc, log)


class VerifyStrategy:
    """动作之后再感知一次，对着新画面断言。这次画面留给下一步复用。"""

    def run(self, cfg, ctx, state: Session, sc: StepContext, log: Log) -> None:
        if not sc.what:
            return
        # 屏外点击被拒之后，不要把它当成「点了没变化、换一种按法」。规划开着就马上重规划。
        if sc.what.startswith("press_offscreen refused") and settings().planner:
            _note(state, "unknown")
            state.plan = None
            state.last_press = None
            state.followup = "plan"
            log("  off-screen press refused; replanning")
            return
        _recapture(cfg, ctx, state, sc)
        if _goal_true(state, sc):
            _finish_goal(state, sc, log)
            return
        sub = _current(state)
        if sub is None:
            return
        verdict = _subgoal_verdict(state, sc)
        if verdict == "true":
            # 断言成立，进入下一个子目标。
            _advance(state, "true", log, skipped=False)
            state.followup = "policy"
            return
        # fresh 是动作之后的指纹，last 是动作之前的。文字容差在 same_screen 里，控件状态不容差。
        changed = state.fresh is not None and state.last is not None and not same_screen(state.fresh, state.last)
        item = _acted_item(sc)
        if verdict == "unknown" and changed:
            # 画面变了，新状态却读不出来。不当失败，也不当成功，下一个子目标对着新画面继续。
            _advance(state, "unknown", log, skipped=False)
            state.followup = "policy"
            return
        if item is not None and item.index not in state.swapped and verdict in {"false", "unknown"} and not changed:
            # 换按法在下一步的 screen_moved 里做，有没有计划走同一条。这里先不重规划。
            state.followup = "policy"
            return
        if item is not None and item.index in state.swapped:
            # 两种按法都试过了。记进已尝试，下一次分类器不要再选它们。
            _extend_tried(state, item)
            state.alt = None
            state.last_press = None
        # 画面变了条件仍不成立，或换过按法仍不成立：剩下的子目标是按旧画面写的，丢掉并重规划。
        _note(state, verdict)
        state.plan = None
        state.last_press = None
        state.followup = "plan"
        log(f"  subgoal did not hold ({verdict}); replanning")


def _recapture(cfg, ctx, state: Session, sc: StepContext) -> None:
    """再截一次。last 保持动作前的指纹，下一步的 screen_moved 才能看出这一下改没改画面。"""
    cache = None if cfg.replay else state.parse_cache
    screen = _runner().capture(cfg.image, cfg.app, cfg.url, ctx.browser, sc.timing)
    items = _runner().perceive(screen, settings().max_options, cfg.goal, sc.timing, cache)
    state.view = (screen, items)
    state.reuse_view = True
    state.fresh = signature(screen, items)
    sc.screen = screen
    sc.items = items


def _goal_true(state: Session, sc: StepContext) -> bool:
    """目标状态在当前画面上成立。未知不算成立，macOS 读不到开关时就是未知。"""
    plan = state.plan
    if plan is None:
        return False
    from .planner import evaluate

    return evaluate(plan.goal_state.require, plan.goal_state.space, sc.items) == "true"


def _finish_goal(state: Session, sc: StepContext, log: Log) -> None:
    state.goal_state_held = True
    state.outcome = "done"
    state.last_press = None
    state.followup = "writer"
    sc.keep_going = False
    log("  goal state holds")


def _subgoal_verdict(state: Session, sc: StepContext) -> str:
    sub = _current(state)
    if sub is None:
        return "unknown"
    from .planner import evaluate

    return evaluate(sub.verify, sub.space, sc.items)


def _advance(state: Session, verdict: str, log: Log, *, skipped: bool) -> None:
    sub = _current(state)
    state.verdicts.append({"do": sub.do, "verdict": verdict})
    state.executed_subgoals += 1
    state.subgoal += 1
    state.last_press = None
    if skipped:
        # 跳过没有动作。下一步看同一张画面时，不要把它算成一次没效果的操作。
        state.quiet = True
        log(f"  subgoal already {verdict}; skipping")


def _note(state: Session, verdict: str) -> None:
    """重规划要带上这一步的结果。子目标作废，下标留给下一份计划从 0 开始。"""
    sub = _current(state)
    if sub is None:
        return
    state.verdicts.append({"do": sub.do, "verdict": verdict})
    state.executed_subgoals += 1


def _remember_refused(state: Session, sc: StepContext) -> None:
    """屏外点击被拒一次，这一屏就不再把这个控件交给分类器或规划器。

    规划开着时马上重规划。没开规划，就只屏蔽，其余和原来一样。
    """
    if not sc.what.startswith("press_offscreen refused"):
        return
    label = _refused_offscreen_label(sc)
    if label and state.last is not None:
        state.refused_offscreen.setdefault(state.last, set()).add(label)
    if settings().planner:
        state.followup = "plan"


def _refused_offscreen_label(sc: StepContext) -> str | None:
    """被拒的是分类器选中的那个屏外控件。序号对不上时没有标签可记。"""
    decision = sc.decision
    choice = getattr(getattr(decision, "offscreen", None), "choice", None)
    nodes = sc.screen.offscreen if sc.screen is not None else []
    if isinstance(choice, str) and choice.isdigit() and int(choice) < len(nodes):
        return nodes[int(choice)].label
    return None


def _remember_press(state: Session, sc: StepContext) -> None:
    """点过的条目先记着。下一屏没变时，screen_moved 换成另一种按法。不是点击就清掉。"""
    item = _acted_item(sc)
    if item is not None and sc.decision is not None and sc.decision.clicking:
        state.last_press = {"index": item.index, "text": item.text, "what": sc.what}
        return
    state.last_press = None


def _offer_other_press(state: Session, items: list[Item], log: Log) -> None:
    """同一屏同一条目只换一次按法。两种都试过就记进已尝试，交给分类器另选。

    没有计划时，只把「选中了、画面却没变」换成鼠标点击。普通点击没变化仍按原来的停顿规则，
    不再自动改走无障碍，否则每一次没效果的点击都会多出一步。
    """
    press = state.last_press
    state.last_press = None
    if press is None:
        return
    index = press["index"]
    if not isinstance(index, int) or index >= len(items) or items[index].text != press["text"]:
        return
    if index in state.swapped:
        _extend_tried(state, items[index])
        state.alt = None
        return
    via_accessibility = "via accessibility" in press["what"]
    if not via_accessibility and state.plan is None:
        return
    state.swapped.add(index)
    state.alt = {"index": index, "method": _other_method(press["what"]), "text": press["text"]}
    log(f"  {press['text']!r} did not change; the next press uses {state.alt['method']}")


def _extend_tried(state: Session, item: Item) -> None:
    for line in _both_presses(item):
        if line not in state.press_tried:
            state.press_tried.append(line)


def _take_alt(state: Session, sc: StepContext, log: Log) -> bool:
    """换按法不再问分类器。条目还在就直接按；条目没了就照常决策。"""
    alt = state.alt
    if alt is None:
        return False
    if alt["index"] >= len(sc.items) or sc.items[alt["index"]].text != alt["text"]:
        state.alt = None
        return False
    item = sc.items[alt["index"]]
    sc.method = alt["method"]
    sc.skip_decide = True
    sc.decision = _choice(item)
    state.alt = None
    log(f"  pressing [{item.index}] {item.text!r} via {sc.method}, without asking again")
    return True


def _acted_item(sc: StepContext) -> Item | None:
    decision = sc.decision
    chosen = getattr(decision, "item", None)
    if chosen is None:
        return None
    by_index = {str(it.index): it for it in sc.items}
    return by_index.get(chosen.choice)


def _other_method(what: str) -> str:
    """这一下走了辅助功能，下一次改点坐标。反过来也一样。每条目每屏只换一次。"""
    return "pointer" if "via accessibility" in what else "accessibility"


def _both_presses(item: Item) -> list[str]:
    """两种按法都试过了，告诉下一次分类器不要再选它们。"""
    return [f"clicked {item.text!r}", f"clicked {item.text!r} via accessibility"]


def _choice(item: Item) -> Decision:
    """换按法时不再问分类器。造一个只点这一条的决定，编号仍是它在全列表里的原编号。"""
    answer = SimpleNamespace(choice=str(item.index), confidence=1.0, probabilities={str(item.index): 1.0})
    kind = SimpleNamespace(choice="click_item", confidence=1.0, probabilities={"click_item": 1.0})
    site = SimpleNamespace(choice="content", confidence=1.0, probabilities={"content": 1.0})
    return Decision(kind=kind, item=answer, site=site)
