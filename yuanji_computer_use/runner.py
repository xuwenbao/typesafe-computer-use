"""主循环：按状态表把一次运行交给宿主、分类器、写手或接管，并把记录写进运行目录。

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

from .actions import ActionPerformer, Context
from .agents.base import AgentFactory
from .agents.session import Session
from .agents.status import Status, allows
from .config import Config
from .llm.calls import Calls, MeteredClassifier, MeteredWriter
from .models import Abort
from .perception.pipeline import Capture
from .platforms.adapter import desktop
from .report import Log
from .timing import Timing


@dataclass
class RunConfig:
    """一次运行的参数。"""

    goal: str  # 任务目标，一句话
    out: Path  # 运行目录，截图和日志都存这里
    act: bool = False  # True 才真的操作机器；False 只演练，告诉你会做什么
    steps: int = field(default_factory=lambda: Config.settings().steps)  # 分类器最多走多少步
    min_confidence: float = field(default_factory=lambda: Config.settings().min_confidence)  # 置信度低于它就停下
    delay: float = field(default_factory=lambda: Config.settings().delay)  # 每个动作后等几秒再截图
    handoffs: int = field(default_factory=lambda: Config.settings().handoffs)  # 写手最多能让分类器“继续干”几次；0 表示一停就结束
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


class Runner:
    """One run: startup, the step loop, and the metered context."""

    @staticmethod
    def run(cfg: RunConfig, ctx_factory) -> Session:
        """跑完整次运行。

        从宿主开始，按 `agents.status` 的转移表把运行交给分类器、写手或接管。
        分类器一步步走；它停下时交给写手。写手可能让它继续，也可能给出最终回答。
        目标没完成又没路可走，再交给接管；接管跑完后写手再看最后一屏。结束时写 run.json。
        ctx_factory(typesafe, history) 负责构造执行动作用的 Context。
        """
        cfg.out.mkdir(parents=True, exist_ok=True)
        log = Log(cfg.out / "run.log")
        ActionPerformer.forget_clipboard()
        log(f"run folder: {cfg.out}")
        if cfg.act:
            log(f"driving the machine. abort: {desktop.abort_hint()}.")

        state = Session(cfg=cfg, log=log)
        started = time.time()
        Capture.snapshot_session_cpu("at_run")
        try:
            with TypeSafeClient() as typesafe:
                state.ctx = Runner.metered(ctx_factory(typesafe, state.history), state.calls)
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
            started_up = Capture.startup_report()
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
                "timing": Timing.summarize(state.timings),
                "startup": started_up["marks"],
                "warm_up": started_up["stages"],
                "session_cpu": started_up["session_cpu"],
                "perception": Capture.pipeline_record(),
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
            log(f"startup: {Runner._startup_line(started_up)}")
            log(f"run folder: {cfg.out}")
        return state

    @staticmethod
    def _startup_line(report: dict) -> str:
        """One log line: the startup marks, then how long each warm-up stage took."""
        marks = "  ".join(f"{name} {seconds:.2f}s" for name, seconds in report["marks"].items())
        stages = "  ".join(f"{name} {seconds:.2f}s" for name, seconds in report["stages"].items())
        return f"{marks}  warm-up: {stages}".strip()

    @staticmethod
    def metered(ctx: Context, calls: Calls) -> Context:
        """给分类器和写手包一层计数器，统计整次运行调用了它们多少次。"""
        return replace(
            ctx,
            typesafe=MeteredClassifier(ctx.typesafe, calls),
            writer=MeteredWriter(ctx.writer, calls) if ctx.writer is not None else None,
        )
