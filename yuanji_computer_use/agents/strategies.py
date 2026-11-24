"""分类器一步里能单独调用的部分：感知之后的判断、执行、重复检查，以及整步 `run_step`。

四个阶段的类包着这些函数。`run_step` 仍按原来的顺序调用它们，日志和文件的写法不变。
"""

from __future__ import annotations

from dataclasses import asdict

from ..actions import perform
from ..bound import CaptureTimeout
from ..config import settings
from ..decide import Decision, decide, offscreen_records
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
        sc.tried = tried_here(state)
        # 步号补齐三位（001、002…），文件按名字排序就是步骤顺序。文件本身由 StepFiles 写。
        sc.prefix = cfg.out / f"step-{sc.step:03d}"


class DecideStrategy:
    """问分类器选一个动作。它说完成、没办法，或置信度不够时，这一步不再执行。"""

    def run(self, cfg, ctx, state: Session, sc: StepContext, log: Log) -> None:
        screen, items = sc.screen, sc.items
        with phase(sc.timing, "decide"):
            decision = decide(
                ctx.typesafe,
                cfg.goal,
                screen,
                items,
                state.history,
                ctx.browser,
                ctx.email,
                sc.tried,
                ctx.guidance,
                sc.picture,
            )
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
        if _refuses(cfg, state, decision, log):
            sc.keep_going = False


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
            sc.what = perform(decision, sc.screen, sc.items, ctx)
        clipboard = "to the clipboard" in sc.what
        state.quiet = clipboard
        state.view = None
        state.markable = decision.kind.choice not in UNMARKED and not clipboard


class MemoryStrategy:
    """把做过的动作写进历史，并检查是不是在同一屏上重复。"""

    def run(self, state: Session, sc: StepContext, log: Log) -> None:
        state.history.append(sc.what)
        log(f"  did: {sc.what}")
        if repeating(state, sc.what, sc.decision.kind.choice == "wait", log):
            sc.keep_going = False


def run_step(cfg, ctx, state: Session, step: int, log: Log) -> bool:
    """分类器的一步：感知 → 决策 → 执行 → 记忆。返回 True 表示继续下一步。

    写文件、计时和上报在中间件里，四个阶段只负责这一步本身的判断。
    """
    from .middleware import StepPipeline

    return StepPipeline(PerceiveStrategy(), DecideStrategy(), ActStrategy(), MemoryStrategy()).run(
        cfg, ctx, state, StepContext(step=step), log
    )


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
        if not (unchanged and state.quiet):
            state.idle = state.idle + 1 if unchanged else 0
        if unchanged and acted and state.markable:
            state.history[-1] += UNCHANGED
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
