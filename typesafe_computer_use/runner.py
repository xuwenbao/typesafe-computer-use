"""主循环：每一步截屏 → 让分类器选动作 → 执行，并把每一步的记录写进运行目录。

几个角色：
- 分类器（classifier）：TypeSafe 模型，每一步从固定选项里挑一个动作。
- 写手（writer）：大语言模型，分类器停下时读屏幕、回答用户，必要时给分类器一个“焦点”让它继续。
- 宿主（host）：真操作开始前，只负责把目标应用打开或调到前台，然后把同一条目标交给分类器。
- 接管（driver）：分类器彻底做不下去时，由更强的模型一步一步接着操作。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from typesafe_sdk import TypeSafeClient

from . import driver
from .actions import Context, forget_clipboard, perform
from .bound import CaptureTimeout
from .calls import Calls, MeteredClassifier, MeteredWriter
from .config import settings
from .decide import Decision, decide, offscreen_records
from .host import prepare
from .models import Abort, Guidance, Item, Screen, Signature, same_screen, signature
from .parse.marks import marks
from .perception import ParseCache, capture, ocr_region, perceive, pipeline_record
from .platform_adapter import desktop
from .report import Log, annotate, ax_count, render_payload, top
from .timing import format_timing, phase, summarize
from .writer import Answer, WriterError, compose_answer, denied_listed_names

# ---- 判断“卡住了”的阈值 ----
# 判断一个动作有没有效果，看的是动作之后的截图有没有变化，而不是看历史记录里写了什么。
MAX_IDLE = 3  # 连续 3 次动作后屏幕都没变化，就判定卡住（例如：动作被拒、一直在等加载、已滚到底还在滚）
MAX_REPEATS = 2  # 在同一个屏幕上连续 2 次重复以前做过的动作，就判定卡住（例如：两个页面来回跳、按钮点了没反应）
MAX_STALLS = 3  # 没到过新页面的情况下第 3 次卡住，运行直接结束（前两次写手给的提示都没用，再试也是白费）

# ---- 写手回答相关 ----
EARLIER_LINES = 600  # 除了最后一屏，写手最多还能看之前屏幕上的多少行文字，用来找答案
MAX_QUESTIONS = 3  # 一次运行里写手最多向用户提问几次；用户回了空内容会提前结束提问

# ---- 历史记录里的“无变化”标记 ----
UNCHANGED = "; the screen did not change"  # 动作没让屏幕变化时，追加到该动作历史记录末尾的文字（发给模型看，所以是英文）
# 这些动作即使屏幕没变也不加上面的标记：
# - wait：等待本来就不该改变屏幕；
# - type_text / type_email：输入后会读回文本框内容并写进记录，而且多一行字会被当作“屏幕基本没变”。
UNMARKED = ("wait", "type_text", "type_email")

# ---- 置信度略低时的放行规则 ----
# 分类器的最高选项低于置信度门槛（--min-confidence）时，满足以下两条仍然执行：
LEAD = 2.0  # 1）最高选项的概率至少是第二名的 2 倍
FLOOR_FRACTION = 0.75  # 2）最高选项的概率至少达到门槛的 75%

# 分类器停下的各种原因，以及发给写手的英文说明（写手会据此向用户解释）。
# 演练（dry run，不真的操作）和用户中止不在这里，它们不交给写手。
STOPPED = {
    "done": "the classifier judged the goal already achieved on this screen",
    "nothing helps": "the classifier found nothing on this screen that helps with the goal",
    "low confidence": "the classifier was not confident enough in any next action",
    "stalled": "the last actions changed nothing",
    "stuck": (
        "the agent is stuck: its last actions changed nothing, as they did twice before on these same pages, "
        "and the focus given each time did not help, so the run ends with this answer"
    ),
    "step limit": "the run used every step it was allowed",
}


@dataclass
class RunConfig:
    """一次运行的参数。"""

    goal: str  # 任务目标，一句话
    out: Path  # 运行目录，截图和日志都存这里
    act: bool = False  # True 才真的操作机器；False 只演练，告诉你会做什么
    steps: int = field(default_factory=lambda: settings().steps)  # 分类器最多走多少步
    min_confidence: float = field(default_factory=lambda: settings().min_confidence)  # 置信度低于它就停下
    delay: float = field(default_factory=lambda: settings().delay)  # 每个动作后等几秒再截图
    handoffs: int = field(default_factory=lambda: settings().handoffs)  # 写手最多能让分类器“继续干”几次；0 表示一停就结束
    driver_steps: int = 0  # 接管模型最多执行多少个动作；0 表示不接管
    driver_batch: int = field(default_factory=lambda: settings().driver_batch)  # 一次回复最多几个动作
    image: Path | None = None  # 用一张保存的截图重放（只读不操作）
    app: str | None = None  # 重放时假装的前台应用名
    url: str | None = None  # 重放时假装的浏览器网址
    # 是否在截图的同时，提前在后台线程里跑 OCR（见 ParseCache.read_ahead）。
    # 查询应用、窗口、网址很慢时才有用，比如通过 WAA 虚拟机；本机查询很快，所以默认关。
    read_ahead: bool = False

    @property
    def replay(self) -> bool:
        return self.image is not None


@dataclass(frozen=True)
class Handoff:
    """一次“交回”：分类器停下后，写手给了提示，让分类器继续干。"""

    step: int  # 在第几步停下的
    outcome: str  # 分类器停下的原因
    focus: str  # 写手给的提示（下一步该关注什么）
    actions: int  # 当时一共做了多少个动作；之后若没再增加，说明这个提示没起作用
    page: tuple[str, str | None] = ("", None)  # 交回时这一屏的（应用名，网址）


@dataclass
class RunState:
    """一次运行过程中不断更新的状态。"""

    history: list[str] = field(default_factory=list)  # 做过的每个动作的文字描述，按顺序
    timings: list[dict[str, float]] = field(default_factory=list)  # 每一步各阶段的耗时
    idle: int = 0  # 连续几次动作没让屏幕变化（到 MAX_IDLE 就停）
    repeats: int = 0  # 连续几次在同一屏幕上重复旧动作（到 MAX_REPEATS 就停）
    stalls: int = 0  # 上次到达新页面以来卡住了几次（到 MAX_STALLS 就结束）
    pages: set[tuple[str, str | None]] = field(default_factory=set)  # 到过的所有页面，记为（应用名，网址）
    last: Signature | None = None  # 上一个动作执行时的屏幕特征
    compared: int = 0  # 上次比较屏幕时的动作总数，用来判断这期间有没有新动作
    markable: bool = False  # 上一个动作没效果时，能否在它的记录后面加“无变化”标记（见 UNMARKED）
    quiet: bool = False  # 上一个动作是剪贴板快捷键（如复制）：屏幕本来就不会变，不算没效果
    seen: list[tuple[Signature, str | None]] = field(default_factory=list)  # 每个动作及其当时的屏幕；等待的动作记为 None
    outcome: str = "crashed"  # 运行结局；正常结束都会改写它，还是 crashed 说明中途抛了异常
    parse_cache: ParseCache = field(default_factory=ParseCache)  # 屏幕识别结果的缓存，下一步可复用
    view: tuple[Screen, list[Item]] | None = None  # 最新截图和识别出的元素；执行动作后置空，表示已过期
    answer: Answer | None = None  # 写手最近一次的回答，运行结束时就是最终答案
    guidance: Guidance = field(default_factory=Guidance)  # 写手当前给的提示，以及用户对提问的回复
    handoffs: list[Handoff] = field(default_factory=list)  # 所有“交回”记录
    route: str = ""  # 写手最后一次回答的去向：classifier、driver 或 end
    calls: Calls = field(default_factory=Calls)  # 整次运行里调用各模型的次数和用量
    image_refusals: list[dict] = field(default_factory=list)  # 截图被模型拒收、改成不带图重问的步骤及原因
    driver_actions: int = 0  # 接管实际执行的动作数，含被拒绝的
    driver_calls: int = 0  # 接管问模型的次数


def run(cfg: RunConfig, ctx_factory) -> RunState:
    """跑完整次运行。

    流程：真操作时宿主先打开目标应用；分类器一步步走；它停下时交给写手；写手可能让它继续，也可能给出最终回答；
    如果目标没完成又没路可走，再交给接管模型。无论怎么结束，最后都写 run.json。
    ctx_factory(typesafe, history) 负责构造执行动作用的 Context。
    """
    cfg.out.mkdir(parents=True, exist_ok=True)
    log = Log(cfg.out / "run.log")
    forget_clipboard()
    log(f"run folder: {cfg.out}")
    if cfg.act:
        log(f"driving the machine. abort: {desktop.abort_hint()}.")

    state = RunState()
    started = time.time()
    try:
        with TypeSafeClient() as typesafe:
            ctx = metered(ctx_factory(typesafe, state.history), state.calls)
            prepare(cfg, ctx, state, log)
            for step in range(1, cfg.steps + 1):
                if run_step(cfg, ctx, state, step, log):
                    continue
                if not hand_off(cfg, ctx, state, step, log):
                    break
                ctx = replace(ctx, guidance=state.guidance)
            else:
                log(f"\nstopped after {cfg.steps} steps")
                state.outcome = "step limit"
                hand_off(cfg, ctx, state, cfg.steps, log)
            if wants_driver(cfg, ctx, state) and driver.take_over(cfg, ctx, state, log, step + 1):
                answer_last(cfg, ctx, state, log)
    except (KeyboardInterrupt, Abort) as e:
        state.outcome = f"aborted ({e or 'Ctrl-C'})"
        log(f"\n{state.outcome} after {len(state.history)} actions")
    finally:
        summary = {
            "goal": cfg.goal,
            "act": cfg.act,
            "steps_taken": len(state.history),
            "outcome": state.outcome,
            "answer": state.answer.text if state.answer else None,
            "goal_achieved": state.answer.achieved if state.answer else None,
            "seconds": round(time.time() - started, 1),
            "calls": state.calls.summary(),
            "usage": state.calls.tokens(),
            "handoffs": [asdict(h) for h in state.handoffs],
            "questions": [asdict(e) for e in state.guidance.exchanges],
            "timing": summarize(state.timings),
            "perception": pipeline_record(),
            "image_refusals": state.image_refusals,
            "driver_actions": state.driver_actions,
            "driver_calls": state.driver_calls,
            "history": state.history,
            "config": {k: str(v) for k, v in asdict(cfg).items()},
        }
        (cfg.out / "run.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        log(f"{state.calls.line()}  handoffs {len(state.handoffs)}  questions {len(state.guidance.exchanges)}")
        log(f"run folder: {cfg.out}")
    return state


def driver_ready(cfg: RunConfig, ctx: Context) -> bool:
    """接管开着、是真操作、而且有写手可问。演练和重放不接管。"""
    return cfg.driver_steps > 0 and cfg.act and not cfg.replay and ctx.writer is not None


def wants_driver(cfg: RunConfig, ctx: Context, state: RunState) -> bool:
    """要不要交给接管模型。以下条件全部满足才接管：

    - 接管功能开着（driver_steps > 0），而且是真操作（不是演练或重放）；
    - 写手可用，并且判断目标还没完成，去向也不是结束；
    - 分类器停在 STOPPED 里的任一结局，包括把 --steps 走满。接管用自己的步数，不占用 --steps。
    """
    return (
        driver_ready(cfg, ctx)
        and state.answer is not None
        and not state.answer.achieved
        and state.route != "end"
        and state.outcome in STOPPED
    )


def answer_last(cfg: RunConfig, ctx: Context, state: RunState, log: Log) -> None:
    """接管结束后，让写手看最后的屏幕给出最终回答。到这里运行就结束了，不会再交回分类器。"""
    try:
        answer = review(cfg, ctx, state, driver.STOPPED[state.outcome], can_ask=False)
    except WriterError as e:
        log(f"\nno answer: the writer failed ({e})")
        return
    state.answer = answer
    log(f"\nanswer ({verdict(answer)}):\n  {answer.text}")


def look(cfg: RunConfig, ctx: Context, state: RunState, log: Log) -> tuple[Screen, list[Item]] | None:
    """接管模型每一步用的截屏和识别。

    和分类器的每一步一样，也会检查“连续几次动作屏幕都没变”。卡住了就返回 None（见 screen_moved）。
    """
    screen = capture(cfg.image, cfg.app, cfg.url, ctx.browser)
    items = perceive(screen, settings().max_options, cfg.goal)
    state.view = (screen, items)
    return (screen, items) if screen_moved(state, screen, items, log) else None


def stops_so_far(state: RunState) -> list[dict]:
    """之前每次“分类器停下 → 写手让它继续”的记录：第几个动作后停的、为什么停、写手给了什么提示。"""
    return [{"after_action": h.actions, "why": STOPPED[h.outcome], "focus_given": h.focus} for h in state.handoffs]


def metered(ctx: Context, calls: Calls) -> Context:
    """给分类器和写手包一层计数器，统计整次运行调用了它们多少次。"""
    return replace(
        ctx,
        typesafe=MeteredClassifier(ctx.typesafe, calls),
        writer=MeteredWriter(ctx.writer, calls) if ctx.writer is not None else None,
    )


def hand_off(cfg: RunConfig, ctx: Context, state: RunState, step: int, log: Log) -> bool:
    """分类器停下后，交给写手处理。返回 True 表示写手让分类器继续干，False 表示运行到此结束。

    为什么需要写手：分类器只会选动作，不会读页面内容；而且目标只有一句话，它有时分不清下一步该做哪个。
    写手看当前屏幕后，会给出一个回答，并且可能：
    - 目标已完成 → 回答就是最终结果；
    - 目标没完成 → 给分类器一个提示（focus），让它带着提示继续；
    - 信息不够 → 先问用户一个问题，拿到回复后再看一遍同一屏幕。

    防止来回空转的规则：
    - 上一个提示没让分类器做出任何动作，就直接用上次的回答结束；
    - 卡住次数到 MAX_STALLS 也直接结束（见 stuck）。
    """
    stuck(state, log)
    stopped = STOPPED.get(state.outcome)
    if stopped is None:
        return False
    if ctx.writer is None:
        log("\nno answer: the writer is disabled (set OPENAI_API_KEY, or ANTHROPIC_API_KEY when CLICKER_WRITER_API=anthropic)")
        return False
    if state.handoffs and state.handoffs[-1].actions == len(state.history) and state.answer is not None:
        log(f"\nanswer ({verdict(state.answer)}; the focus led to no action, so the last answer stands):\n  {state.answer.text}")
        return False

    may_resume = step < cfg.steps and len(state.handoffs) < cfg.handoffs and state.outcome != "stuck"
    reviews: list[dict] = []
    while True:
        can_ask = may_resume and ctx.ask is not None and len(state.guidance.exchanges) < MAX_QUESTIONS
        started = time.perf_counter()
        try:
            answer = review(cfg, ctx, state, stopped, can_ask)
        except WriterError as e:
            log(f"\nno answer: the writer failed ({e})")
            return False
        state.answer = answer
        seconds = time.perf_counter() - started
        record = {"outcome": state.outcome, "seconds": round(seconds, 3), **asdict(answer), "reply": None, "handed_back": False}
        reviews.append(record)
        try:
            resuming = may_resume and not answer.achieved
            if resuming and can_ask and answer.question:
                log(f"\nreview ({verdict(answer)}, {seconds:.1f}s):\n  {answer.text}\n  the writer asks: {answer.question}")
                reply = ctx.ask(answer.question).strip()
                record["reply"] = reply
                log(f"  > {reply}", echo=False)  # 只写日志不打印，用户刚在终端里打过这些字
                if not reply:
                    log("  no reply, so the answer stands")
                    return False
                state.guidance = state.guidance.heard(answer.question, reply)
                if cfg.act and not cfg.replay and state.view is not None:
                    desktop.activate(state.view[0].app)  # 用户在终端回答问题后，把原来操作的应用切回前台
                continue
            state.route = answer.route
            screen = state.view[0] if state.view else None
            page = (screen.app, screen.url) if screen is not None else ("", None)
            # 同一应用、同一网址上已经交回过一次。接管开着就换人，不再把同一页交回分类器。
            if (
                resuming
                and answer.route == "classifier"
                and answer.focus
                and any(earlier.page == page for earlier in state.handoffs)
                and driver_ready(cfg, ctx)
            ):
                state.route = "driver"
                log(
                    f"\nreview ({verdict(answer)}, {seconds:.1f}s):\n  {answer.text}\n"
                    "  the classifier already stopped on this page after a focus; the driver takes over"
                )
                return False
            if resuming and answer.route == "classifier" and answer.focus:
                log(
                    f"\nreview ({verdict(answer)}, {seconds:.1f}s):\n  {answer.text}\n  back to the classifier, focus: {answer.focus}"
                )
                record["handed_back"] = True
                state.handoffs.append(Handoff(step, state.outcome, answer.focus, len(state.history), page))
                state.guidance = state.guidance.focused(answer.focus)
                state.idle = state.repeats = 0  # 有了新提示，卡住计数清零重来
                return True
            log(f"\nanswer ({verdict(answer)}, route {answer.route}, {seconds:.1f}s):\n  {answer.text}")
            return False
        finally:
            (cfg.out / f"step-{step:03d}-review.json").write_text(json.dumps(reviews, indent=2), encoding="utf-8")


def stuck(state: RunState, log: Log) -> None:
    """记录一次卡住；没到过新页面就第 MAX_STALLS（3）次卡住时，把结局改成 stuck，运行结束。

    为什么是 3 次：卡住后写手给的提示通常能让分类器继续。但如果在同样的页面上又卡住，说明提示没用，
    再试只会多花一次写手调用和几个无效动作。在 Chrome 任务上实测：第 3 次卡住的运行最后全部失败，
    成功的运行从没卡住超过 2 次。
    到达新页面会把计数清零，所以长任务偶尔卡一下不会被误判。
    """
    if state.outcome != "stalled":
        return
    state.stalls += 1
    if state.stalls >= MAX_STALLS:
        log(f"  stuck: {state.stalls} stalls without reaching a new page; the run ends here")
        state.outcome = "stuck"


def verdict(answer: Answer) -> str:
    return "goal achieved" if answer.achieved else "goal not achieved"


def review(cfg: RunConfig, ctx: Context, state: RunState, stopped: str, can_ask: bool) -> Answer:
    """让写手看当前屏幕，给出回答。

    如果上一步截图后没做过动作，直接用那张截图；否则重新截一张，并存成 answer-raw.png，方便事后核对回答。
    """
    if state.view is None:
        desktop.check_abort()
        screen = capture(cfg.image, cfg.app, cfg.url, ctx.browser)
        screen.image.save(cfg.out / "answer-raw.png")
        state.view = (screen, perceive(screen, settings().max_options, cfg.goal))
    screen, items = state.view
    earlier = earlier_screens(state, signature(screen, items))
    earlier_stops = stops_so_far(state)
    answer = compose_answer(
        ctx.writer, cfg.goal, screen, items, state.history, stopped, earlier, state.guidance, earlier_stops, can_ask
    )
    # 空白预览不能把名单上的名字说成没有。否定了它们就丢掉截图再判一次，第二次为准。
    denied = denied_listed_names(f"{answer.text}\n{answer.focus}", answer.achieved, items)
    if not denied:
        return answer
    return compose_answer(
        ctx.writer,
        cfg.goal,
        screen,
        items,
        state.history,
        stopped,
        earlier,
        state.guidance,
        earlier_stops,
        can_ask,
        picture=False,
        denied=denied,
    )


def earlier_screens(state: RunState, final: Signature, budget: int = EARLIER_LINES) -> list[dict]:
    """最后一屏之前经过的其他屏幕上的文字，按时间从早到晚排列，一并发给写手。

    原因：答案可能出现在中途的页面上（比如价格在商品列表页，结账页上没有）。
    规则：重复的屏幕只发一次；从最新的往前取，总行数超过 budget 就不再往前取。
    """
    out: list[Signature] = []
    lines_left = budget
    for seen, _ in reversed(state.seen):
        if same_screen(seen, final) or any(same_screen(seen, kept) for kept in out):
            continue
        lines_left -= len(seen[3])
        if lines_left < 0:
            break
        out.append(seen)
    return [{"app": app, "url": url, "text": [text for text, _ in lines]} for app, url, _, lines in reversed(out)]


def run_step(cfg: RunConfig, ctx: Context, state: RunState, step: int, log: Log) -> bool:
    """分类器的一步：截屏 → 识别元素 → 分类器选动作 → 执行。返回 True 表示继续下一步。

    每一步在运行目录里留下 step-NNN-*.png / payload.txt / answers.json，方便事后复盘。
    """
    desktop.check_abort()
    timing: dict[str, float] = {}
    started = time.perf_counter()
    cache = None if cfg.replay else state.parse_cache
    try:
        with phase(timing, "capture"):
            screen = capture(cfg.image, cfg.app, cfg.url, ctx.browser, timing, ahead=cache if cfg.read_ahead else None)
    except CaptureTimeout:
        # 截屏没有在时限内返回。这一步没有画面，不交给写手或接管；收尾仍会写 run.json。
        log("capture timed out")
        state.outcome = "capture timed out"
        timing["total"] = round(time.perf_counter() - started, 3)
        state.timings.append(timing)
        return False
    items = perceive(screen, settings().max_options, cfg.goal, timing, cache)
    state.view = (screen, items)
    if not screen_moved(state, screen, items, log):
        return False
    tried = tried_here(state)
    prefix = cfg.out / f"step-{step:03d}"  # 步号补齐三位（001、002…），文件按名字排序就是步骤顺序
    screen.image.save(prefix.with_name(prefix.name + "-raw.png"))
    picture = marks(screen.image, items, ocr_region(screen))
    sent = prefix.with_name(prefix.name + "-sent.png")
    sent.write_bytes(picture.png)
    prefix.with_name(prefix.name + "-payload.txt").write_text(
        render_payload(cfg.goal, screen, items, state.history, ctx.browser, ctx.email, tried, ctx.guidance, picture, sent.name),
        encoding="utf-8",
    )

    with phase(timing, "decide"):
        decision = decide(
            ctx.typesafe, cfg.goal, screen, items, state.history, ctx.browser, ctx.email, tried, ctx.guidance, picture
        )
    if decision.image_refused is not None:
        log(f"  the screen picture was refused, and the step asked again without it: {decision.image_refused}")
        state.image_refusals.append({"step": step, "reason": decision.image_refused})
    by_index = {str(it.index): it for it in items}
    annotate(screen, items, decision.chosen, prefix.with_suffix(".png"))

    field_desc = f" field={screen.field.role}:{screen.field.label!r}" if screen.field else ""
    log(
        f"\nstep {step}: app={screen.app!r}{field_desc} url={screen.url!r} items={len(items)} ax={ax_count(items)} "
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

    keep_going = resolve(cfg, ctx, state, screen, items, decision, timing, log)
    timing.setdefault("act", 0.0)
    timing["total"] = round(time.perf_counter() - started, 3)
    state.timings.append(timing)

    prefix.with_name(prefix.name + "-answers.json").write_text(
        json.dumps(answers(decision, screen, items, timing, tried, state), indent=2), encoding="utf-8"
    )
    log(
        f"  files: {prefix.name}-raw.png, {prefix.name}.png, {prefix.name}-sent.png, {prefix.name}-payload.txt, "
        f"{prefix.name}-answers.json"
    )
    log(format_timing(timing))

    if state.view is None:  # 这一步执行了动作，等 delay 秒让页面稳定后再截下一张
        desktop.sleep_watching(cfg.delay)
    return keep_going


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


def resolve(
    cfg: RunConfig,
    ctx: Context,
    state: RunState,
    screen: Screen,
    items: list[Item],
    decision: Decision,
    timing: dict[str, float],
    log: Log,
) -> bool:
    """先检查要不要停下，不停就执行动作。返回 True 表示继续下一步。

    停下的情况：分类器说“完成了”或“没办法”；置信度太低（且不满足破例条件）；演练模式。
    """
    if decision.stops:
        log(f"  model says {decision.kind.choice!r}; stopping")
        state.outcome = "done" if decision.kind.choice == "done" else "nothing helps"
        return False
    if decision.confidence < cfg.min_confidence:
        # 屏幕元素很多时，概率会分散到上百个选项上，即使目标很明确，第一名也可能略低于门槛。
        # 所以第一名明显领先时放行；如果前两名差不多（两个都像对的），照样停下——这正是门槛要拦的情况。
        if not _lead_covers(decision, cfg.min_confidence):
            log(f"  confidence {decision.confidence:.2f} below {cfg.min_confidence}; stopping")
            state.outcome = "low confidence"
            return False
        log(f"  confidence {decision.confidence:.2f} below {cfg.min_confidence}, but it leads the next option; continuing")
    if not cfg.act or cfg.replay:
        log(f"  would do: {decision.chosen}. dry run (pass --act without --image to drive the machine)")
        state.outcome = "dry run"
        return False

    state.quiet = False
    with phase(timing, "act"):
        what = perform(decision, screen, items, ctx)
    clipboard = "to the clipboard" in what
    state.quiet = clipboard
    state.view = None
    state.history.append(what)
    from .windows_session import report_action

    report_action(what)
    state.markable = decision.kind.choice not in UNMARKED and not clipboard
    log(f"  did: {what}")
    return not repeating(state, what, decision.kind.choice == "wait", log)


def screen_moved(state: RunState, screen: Screen, items: list[Item], log: Log) -> bool:
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


def tried_here(state: RunState) -> list[str]:
    """在当前这个屏幕上已经做过哪些动作（按先后顺序），告诉分类器避免重复。"""
    return [what for seen, what in state.seen if what is not None and state.last is not None and same_screen(seen, state.last)]


def repeating(state: RunState, what: str, waiting: bool, log: Log) -> bool:
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
    decision: Decision, screen: Screen, items: list[Item], timing: dict[str, float], tried: list[str], state: RunState
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
