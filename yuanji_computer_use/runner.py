"""主循环：每一步截屏 → 让分类器选动作 → 执行，并把每一步的记录写进运行目录。

几个角色按状态表交接，主循环本身不再决定下一步交给谁：
- 宿主（host）：真操作开始前，只负责把目标应用打开或调到前台，然后把同一条目标交给分类器。
- 分类器（policy）：TypeSafe 模型，每一步从固定选项里挑一个动作。
- 写手（writer）：大语言模型，分类器停下时读屏幕、回答用户，必要时给分类器一个“焦点”让它继续。
- 接管（driver）：分类器彻底做不下去时，由更强的模型一步一步接着操作。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from typesafe_sdk import TypeSafeClient

from .actions import Context, forget_clipboard
from .agents.base import AgentFactory
from .agents.session import Handoff, Session
from .agents.status import Status, allows
from .agents.strategies import (
    FLOOR_FRACTION,
    LEAD,
    MAX_IDLE,
    MAX_REPEATS,
    UNCHANGED,
    UNMARKED,
    answers,
    repeating,
    resolve,
    run_step,
    screen_moved,
    tried_here,
)

# 这些名字现在定义在 agents.strategies，仍从这里导出，测试和接管模块还在用。
__all__ = [
    "FLOOR_FRACTION",
    "LEAD",
    "MAX_IDLE",
    "MAX_REPEATS",
    "UNCHANGED",
    "UNMARKED",
    "RunConfig",
    "RunState",
    "answers",
    "repeating",
    "resolve",
    "run",
    "run_step",
    "screen_moved",
    "settle",
    "tried_here",
]
from .calls import Calls, MeteredClassifier, MeteredWriter
from .config import settings
from .models import Abort, Item, Screen, Signature, same_screen, signature
from .perception import capture, perceive, pipeline_record, settle, snapshot_session_cpu, startup_report
from .platform_adapter import desktop
from .report import Log
from .timing import summarize
from .writer import Answer, WriterError, compose_answer, denied_listed_names, listed_but_called_absent

# ---- 判断“卡住了”的阈值 ----
# 判断一个动作有没有效果，看的是动作之后的截图有没有变化，而不是看历史记录里写了什么。
MAX_STALLS = 3  # 没到过新页面的情况下第 3 次卡住，运行直接结束（前两次写手给的提示都没用，再试也是白费）

# ---- 写手回答相关 ----
EARLIER_LINES = 600  # 除了最后一屏，写手最多还能看之前屏幕上的多少行文字，用来找答案
MAX_QUESTIONS = 3  # 一次运行里写手最多向用户提问几次；用户回了空内容会提前结束提问

# 分类器停下的各种原因，以及发给写手的英文说明（写手会据此向用户解释）。
# 演练（dry run，不真的操作）和用户中止不在这里，它们不交给写手。
STOPPED = {
    "done": "the classifier judged the goal already achieved on this screen",
    "nothing helps": "the classifier found nothing on this screen that helps with the goal",
    "low confidence": "the classifier was not confident enough in any next action",
    "malformed answer": "the classifier's answer did not match the options it was offered, so nothing was done",
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
    image: Path | None = None  # 用一张保存的截图重放（只读不操作）
    app: str | None = None  # 重放时假装的前台应用名
    url: str | None = None  # 重放时假装的浏览器网址
    # 是否在截图的同时，提前在后台线程里跑 OCR（见 ParseCache.read_ahead）。
    # 查询应用、窗口、网址很慢时才有用，比如通过 WAA 虚拟机；本机查询很快，所以默认关。
    read_ahead: bool = False

    @property
    def replay(self) -> bool:
        return self.image is not None


# 名字还留在这里，是因为这个模块和测试都在用。类本身在 agents.session。
RunState = Session


def run(cfg: RunConfig, ctx_factory) -> RunState:
    """跑完整次运行。

    从宿主开始，按 `agents.status` 的转移表把运行交给分类器、规划、写手或接管。
    分类器一步步走；首步分差不够，或它因低置信、没有可用动作、卡住而停下时，先规划。
    它说完成，或步数用完，交给写手。写手可能让它继续，也可能给出最终回答。
    目标没完成又没路可走，再交给接管；接管跑完后写手再看最后一屏。结束时写 run.json。
    ctx_factory(typesafe, history) 负责构造执行动作用的 Context。
    """
    cfg.out.mkdir(parents=True, exist_ok=True)
    log = Log(cfg.out / "run.log")
    forget_clipboard()
    log(f"run folder: {cfg.out}")
    if cfg.act:
        log(f"driving the machine. abort: {desktop.abort_hint()}.")

    state = RunState(cfg=cfg, log=log)
    started = time.time()
    snapshot_session_cpu("at_run")
    try:
        with TypeSafeClient() as typesafe:
            state.ctx = metered(ctx_factory(typesafe, state.history), state.calls)
            # 状态只回答“下一步该谁”。为什么停写在 state.outcome，不写在状态里。
            status = Status.HOST
            while status is not Status.FINISH:
                # 最终回答不走 handle：接管已经结束，写手只看最后一屏，然后必须到 FINISH。
                if status is Status.FINAL_ANSWER:
                    nxt = AgentFactory.create("writer").conclude(state)
                else:
                    nxt = AgentFactory.create(status.value).handle(state)
                # 表里没有的边是程序错误。停在这里，避免运行交到没有角色接的状态。
                if not allows(status, nxt):
                    raise RuntimeError(f"illegal transition {status.value} -> {nxt.value}")
                status = nxt
    except (KeyboardInterrupt, Abort) as e:
        state.outcome = f"aborted ({e or 'Ctrl-C'})"
        log(f"\n{state.outcome} after {len(state.history)} actions")
    finally:
        started_up = startup_report()
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
            "startup": started_up["marks"],
            "warm_up": started_up["stages"],
            "session_cpu": started_up["session_cpu"],
            "perception": pipeline_record(),
            "image_refusals": state.image_refusals,
            "driver_error": state.driver_error,
            "driver_actions": state.driver_actions,
            "driver_calls": state.driver_calls,
            "driver_tool_calls": state.driver_tool_calls,
            "history": state.history,
            "config": {k: str(v) for k, v in asdict(cfg).items()},
        }
        (cfg.out / "run.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        log(f"{state.calls.line()}  handoffs {len(state.handoffs)}  questions {len(state.guidance.exchanges)}")
        log(f"startup: {_startup_line(started_up)}")
        log(f"run folder: {cfg.out}")
    return state


def _driver():
    """The takeover. Imported here, not at startup: it pulls in the agents SDK, which the run
    pays for only when the classifier has stopped and a takeover is actually going to happen."""
    from . import driver

    return driver


def _startup_line(report: dict) -> str:
    """One log line: the startup marks, then how long each warm-up stage took."""
    marks = "  ".join(f"{name} {seconds:.2f}s" for name, seconds in report["marks"].items())
    stages = "  ".join(f"{name} {seconds:.2f}s" for name, seconds in report["stages"].items())
    return f"{marks}  warm-up: {stages}".strip()


def driver_ready(cfg: RunConfig, ctx: Context) -> bool:
    """接管开着、是真操作、写手可用，而且写手走的是 OpenAI 接口。演练和重放不接管。"""
    return cfg.driver_steps > 0 and cfg.act and not cfg.replay and ctx.writer is not None and settings().writer_api == "openai"


def wants_driver(cfg: RunConfig, ctx: Context, state: RunState, log=None) -> bool:
    """要不要交给接管模型。以下条件全部满足才接管：

    - 接管功能开着（driver_steps > 0），而且是真操作（不是演练或重放）；
    - 写手可用，走 OpenAI 接口，并且判断目标还没完成，去向也不是结束；
    - 分类器停在 STOPPED 里的任一结局，包括把 --steps 走满。接管用自己的步数，不占用 --steps。

    其它条件都满足、只差接口是 Anthropic 时，记一行日志，说明驱动需要 OpenAI 接口。
    """
    would = (
        cfg.driver_steps > 0
        and cfg.act
        and not cfg.replay
        and ctx.writer is not None
        and state.answer is not None
        and not state.answer.achieved
        and state.route != "end"
        and state.outcome in STOPPED
    )
    if would and settings().writer_api != "openai":
        if log is not None:
            log("the driver needs CLICKER_WRITER_API=openai; stopping at the classifier")
        return False
    return would and driver_ready(cfg, ctx)


def answer_last(cfg: RunConfig, ctx: Context, state: RunState, log: Log) -> None:
    """接管结束后，让写手看最后的屏幕给出最终回答。到这里运行就结束了，不会再交回分类器。

    接管已经判定完成、写手却说没完成时，把接管的理由和当前画面再给写手看一次，以这次为准。
    """
    try:
        answer = review(cfg, ctx, state, _driver().STOPPED[state.outcome], can_ask=False)
    except WriterError as e:
        log(f"\nno answer: the writer failed ({e})")
        return
    if not answer.achieved and state.driver_done and state.view is not None:
        log("\nthe driver said done; asking the writer once more")
        screen, items = state.view
        try:
            answer = compose_answer(
                ctx.writer,
                cfg.goal,
                screen,
                items,
                state.history,
                _driver().STOPPED[state.outcome],
                earlier_screens(state, signature(screen, items)),
                state.guidance,
                stops_so_far(state),
                False,
                driver_said_done=state.driver_done,
            )
        except WriterError as e:
            log(f"\nno answer: the writer failed ({e})")
    state.answer = answer
    log(f"\nanswer ({verdict(answer)}):\n  {answer.text}")


def look(cfg: RunConfig, ctx: Context, state: RunState, log: Log) -> tuple[Screen, list[Item]] | None:
    """接管模型每一步用的截屏和识别。

    和分类器的每一步一样，也会检查“连续几次动作屏幕都没变”。卡住了就返回 None（见 screen_moved）。
    识别沿用这次运行的缓存，只重读变过的区域，图标说明也不重算。
    """
    screen = capture(cfg.image, cfg.app, cfg.url, ctx.browser)
    items = perceive(screen, settings().max_options, cfg.goal, cache=None if cfg.replay else state.parse_cache)
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
        state.view = (screen, perceive(screen, settings().max_options, cfg.goal, cache=None if cfg.replay else state.parse_cache))
    screen, items = state.view
    earlier = earlier_screens(state, signature(screen, items))
    earlier_stops = stops_so_far(state)
    answer = compose_answer(
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
    )
    # 空白预览不能把名单上的名字说成没有。否定了它们就丢掉截图再判一次，第二次为准。
    denied = denied_listed_names(f"{answer.text}\n{answer.focus}", answer.achieved, items)
    for name in listed_but_called_absent(answer, items):
        if name not in denied:
            denied.append(name)
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
    out: list[tuple[Signature, int]] = []
    lines_left = budget
    total = len(state.seen)
    for index in range(total - 1, -1, -1):
        seen = state.seen[index][0]
        if same_screen(seen, final) or any(same_screen(seen, kept) for kept, _ in out):
            continue
        lines_left -= len(seen[3])
        if lines_left < 0:
            break
        out.append((seen, total - index))
    return [
        {"app": app, "url": url, "text": [text for text, _ in lines], "actions_since": since}
        for (app, url, _, lines), since in ((sig[:4], since) for sig, since in reversed(out))
    ]
