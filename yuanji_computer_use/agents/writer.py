"""写手这个角色。模块路径是 `agents.writer`，和包里生成回答的 `writer` 不是同一个文件。

交回、要不要接管、以及接管结束后的最终回答都从这里走。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, replace

from ..config import settings
from ..llm.writer import Answer, Writer, WriterError
from ..models import Signature, same_screen, signature
from ..perception import pipeline as perception
from ..platforms.adapter import desktop
from .base import BaseAgent, register
from .outcomes import CLASSIFIER_STOPPED
from .session import Handoff, Session
from .status import Status

# ---- 判断“卡住了”的阈值 ----
# 判断一个动作有没有效果，看的是动作之后的截图有没有变化，而不是看历史记录里写了什么。
MAX_STALLS = 3  # 没到过新页面的情况下第 3 次卡住，运行直接结束（前两次写手给的提示都没用，再试也是白费）

# ---- 写手回答相关 ----
EARLIER_LINES = 600  # 除了最后一屏，写手最多还能看之前屏幕上的多少行文字，用来找答案
MAX_QUESTIONS = 3  # 一次运行里写手最多向用户提问几次；用户回了空内容会提前结束提问


class RunMemory:
    """一次运行里，写手和接管都要回看的屏幕与停下记录。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def stops_so_far(self) -> list[dict]:
        """之前每次“分类器停下 → 写手让它继续”的记录：第几个动作后停的、为什么停、写手给了什么提示。"""
        return [
            {"after_action": handoff.actions, "why": CLASSIFIER_STOPPED[handoff.outcome], "focus_given": handoff.focus}
            for handoff in self.session.handoffs
        ]

    def earlier_screens(self, final: Signature, budget: int = EARLIER_LINES) -> list[dict]:
        """最后一屏之前经过的其他屏幕上的文字，按时间从早到晚排列，一并发给写手。

        原因：答案可能出现在中途的页面上（比如价格在商品列表页，结账页上没有）。
        规则：重复的屏幕只发一次；从最新的往前取，总行数超过 budget 就不再往前取。
        """
        out: list[tuple[Signature, int]] = []
        lines_left = budget
        total = len(self.session.seen)
        for index in range(total - 1, -1, -1):
            seen = self.session.seen[index][0]
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


@register("writer")
class WriterAgent(BaseAgent):
    def handle(self, session) -> Status:
        """分类器停下后的一次交回。继续就回到分类器；要接管就交给接管；否则结束。"""
        from .driver import DriverAgent

        if self.hand_off(session):
            session.ctx = replace(session.ctx, guidance=session.guidance)
            return Status.POLICY
        if DriverAgent.wants(session):
            return Status.DRIVER
        return Status.FINISH

    def hand_off(self, session) -> bool:
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
        from .driver import DriverAgent

        self.bind(session)
        cfg, ctx, state, log = self.cfg, self.ctx, self.session, self.log
        step = session.step
        self.stuck()
        stopped = CLASSIFIER_STOPPED.get(state.outcome)
        if stopped is None:
            return False
        if ctx.writer is None:
            log(
                "\nno answer: the writer is disabled (set OPENAI_API_KEY, or ANTHROPIC_API_KEY when CLICKER_WRITER_API=anthropic)"
            )
            return False
        if state.handoffs and state.handoffs[-1].actions == len(state.history) and state.answer is not None:
            log(
                f"\nanswer ({self.verdict(state.answer)}; the focus led to no action, so the last answer stands):\n  {state.answer.text}"
            )
            return False

        may_resume = step < cfg.steps and len(state.handoffs) < cfg.handoffs and state.outcome != "stuck"
        reviews: list[dict] = []
        while True:
            can_ask = may_resume and ctx.ask is not None and len(state.guidance.exchanges) < MAX_QUESTIONS
            started = time.perf_counter()
            try:
                answer = self.review(session, stopped, can_ask)
            except WriterError as e:
                log(f"\nno answer: the writer failed ({e})")
                return False
            state.answer = answer
            seconds = time.perf_counter() - started
            record = {
                "outcome": state.outcome,
                "seconds": round(seconds, 3),
                **asdict(answer),
                "reply": None,
                "handed_back": False,
            }
            reviews.append(record)
            try:
                resuming = may_resume and not answer.achieved
                if resuming and can_ask and answer.question:
                    log(
                        f"\nreview ({self.verdict(answer)}, {seconds:.1f}s):\n  {answer.text}\n  the writer asks: {answer.question}"
                    )
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
                    and DriverAgent.ready(cfg, ctx)
                ):
                    state.route = "driver"
                    log(
                        f"\nreview ({self.verdict(answer)}, {seconds:.1f}s):\n  {answer.text}\n"
                        "  the classifier already stopped on this page after a focus; the driver takes over"
                    )
                    return False
                if resuming and answer.route == "classifier" and answer.focus:
                    log(
                        f"\nreview ({self.verdict(answer)}, {seconds:.1f}s):\n  {answer.text}\n  back to the classifier, focus: {answer.focus}"
                    )
                    record["handed_back"] = True
                    state.handoffs.append(Handoff(step, state.outcome, answer.focus, len(state.history), page))
                    state.guidance = state.guidance.focused(answer.focus)
                    state.idle = state.repeats = 0  # 有了新提示，卡住计数清零重来
                    return True
                log(f"\nanswer ({self.verdict(answer)}, route {answer.route}, {seconds:.1f}s):\n  {answer.text}")
                return False
            finally:
                (cfg.out / f"step-{step:03d}-review.json").write_text(json.dumps(reviews, indent=2), encoding="utf-8")

    def review(self, session, stopped: str, can_ask: bool) -> Answer:
        """让写手看当前屏幕，给出回答。

        如果上一步截图后没做过动作，直接用那张截图；否则重新截一张，并存成 answer-raw.png，方便事后核对回答。
        """
        cfg, ctx, state = session.cfg, session.ctx, session
        if state.view is None:
            desktop.check_abort()
            screen = perception.capture(cfg.image, cfg.app, cfg.url, ctx.browser)
            screen.image.save(cfg.out / "answer-raw.png")
            state.view = (
                screen,
                perception.perceive(screen, settings().max_options, cfg.goal, cache=None if cfg.replay else state.parse_cache),
            )
        screen, items = state.view
        memory = RunMemory(state)
        earlier = memory.earlier_screens(signature(screen, items))
        earlier_stops = memory.stops_so_far()
        answer = Writer(ctx.writer).compose_answer(
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
        denied = Writer.denied_listed_names(f"{answer.text}\n{answer.focus}", answer.achieved, items)
        for name in Writer.listed_but_called_absent(answer, items):
            if name not in denied:
                denied.append(name)
        if not denied:
            return answer
        return Writer(ctx.writer).compose_answer(
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

    def conclude(self, session) -> Status:
        """接管已经跑完。写手看最后一屏作答，然后结束，不再交回分类器。

        接管已经判定完成、写手却说没完成时，把接管的理由和当前画面再给写手看一次，以这次为准。
        """
        from .driver import DriverAgent

        self.bind(session)
        cfg, ctx, state, log = self.cfg, self.ctx, self.session, self.log
        takeover = DriverAgent._takeover()
        try:
            answer = self.review(session, takeover.STOPPED[state.outcome], False)
        except WriterError as e:
            log(f"\nno answer: the writer failed ({e})")
            return Status.FINISH
        if not answer.achieved and state.driver_done and state.view is not None:
            log("\nthe driver said done; asking the writer once more")
            screen, items = state.view
            memory = RunMemory(state)
            try:
                answer = Writer(ctx.writer).compose_answer(
                    cfg.goal,
                    screen,
                    items,
                    state.history,
                    takeover.STOPPED[state.outcome],
                    memory.earlier_screens(signature(screen, items)),
                    state.guidance,
                    memory.stops_so_far(),
                    False,
                    driver_said_done=state.driver_done,
                )
            except WriterError as e:
                log(f"\nno answer: the writer failed ({e})")
        state.answer = answer
        log(f"\nanswer ({self.verdict(answer)}):\n  {answer.text}")
        return Status.FINISH

    def stuck(self) -> None:
        """记录一次卡住；没到过新页面就第 MAX_STALLS（3）次卡住时，把结局改成 stuck，运行结束。

        为什么是 3 次：卡住后写手给的提示通常能让分类器继续。但如果在同样的页面上又卡住，说明提示没用，
        再试只会多花一次写手调用和几个无效动作。在 Chrome 任务上实测：第 3 次卡住的运行最后全部失败，
        成功的运行从没卡住超过 2 次。
        到达新页面会把计数清零，所以长任务偶尔卡一下不会被误判。
        """
        state, log = self.session, self.log
        if state.outcome != "stalled":
            return
        state.stalls += 1
        if state.stalls >= MAX_STALLS:
            log(f"  stuck: {state.stalls} stalls without reaching a new page; the run ends here")
            state.outcome = "stuck"

    @staticmethod
    def verdict(answer: Answer) -> str:
        return "goal achieved" if answer.achieved else "goal not achieved"
