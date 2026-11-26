"""一步外面的四件套：计时、写文件、向会话窗口上报、收尾日志。

阶段本身不碰这些。换掉决策或执行时，文件和计时仍按原来的时机留下。
"""

from __future__ import annotations

import json
import time
from typing import Protocol

from ...parse.marks import Marker
from ...perception import pipeline as perception
from ...perception.pipeline import Perceiver
from ...platforms.windows_session import WindowsSession
from ...report import ScreenReport
from ...timing import Timing as Phases
from ..session import Session, StepContext
from .stages import DecideStrategy


class StepMiddleware(Protocol):
    def before(self, session: Session, sc: StepContext) -> None: ...

    def after(self, session: Session, sc: StepContext, log) -> None: ...

    def on_error(self, session: Session, sc: StepContext, error: BaseException) -> None: ...


class Timing:
    """记下这一步用了多久。中途抛出异常时也记，再把异常抛回去。"""

    def before(self, session: Session, sc: StepContext) -> None:
        sc.started = time.perf_counter()

    def after(self, session: Session, sc: StepContext, log) -> None:
        self.seal(session, sc)

    def on_error(self, session: Session, sc: StepContext, error: BaseException) -> None:
        self.seal(session, sc)

    def seal(self, session: Session, sc: StepContext) -> None:
        if sc.timing.get("total") is not None and session.timings and session.timings[-1] is sc.timing:
            return
        if sc.prefix is not None:
            sc.timing.setdefault("act", 0.0)
        sc.timing["total"] = round(time.perf_counter() - sc.started, 3)
        session.timings.append(sc.timing)


class StepFiles:
    """写出这一步的五份记录。请求在决策前，标注图在决策后，答案在动作之后。"""

    def before(self, session: Session, sc: StepContext) -> None:
        return None

    def after(self, session: Session, sc: StepContext, log) -> None:
        if sc.prefix is None or sc.decision is None:
            return
        sc.prefix.with_name(sc.prefix.name + "-answers.json").write_text(
            json.dumps(DecideStrategy.answers(sc.decision, sc.screen, sc.items, sc.timing, sc.tried, session), indent=2),
            encoding="utf-8",
        )

    def on_error(self, session: Session, sc: StepContext, error: BaseException) -> None:
        return None

    def open_request(self, cfg, ctx, session: Session, sc: StepContext) -> None:
        screen = sc.screen
        sc.picture = Marker.marks(screen.image, sc.items, Perceiver.ocr_region(screen))
        screen.image.save(sc.prefix.with_name(sc.prefix.name + "-raw.png"))
        sent = sc.prefix.with_name(sc.prefix.name + "-sent.png")
        sent.write_bytes(sc.picture.png)
        sc.prefix.with_name(sc.prefix.name + "-payload.txt").write_text(
            ScreenReport.render_payload(
                cfg.goal,
                screen,
                sc.items,
                session.history,
                ctx.browser,
                ctx.email,
                sc.tried,
                ctx.guidance,
                sc.picture,
                sent.name,
            ),
            encoding="utf-8",
        )

    def after_decide(self, session: Session, sc: StepContext) -> None:
        ScreenReport.annotate(sc.screen, sc.items, sc.decision.chosen, sc.prefix.with_suffix(".png"))


class SessionReport:
    """把这一步做成的事告诉会话窗口。没有动作，或窗口没开，就什么都不做。"""

    def before(self, session: Session, sc: StepContext) -> None:
        return None

    def after(self, session: Session, sc: StepContext, log) -> None:
        if sc.what:
            WindowsSession.report_action(sc.what)

    def on_error(self, session: Session, sc: StepContext, error: BaseException) -> None:
        return None


class StepLog:
    """这一步留下了哪些文件，以及各阶段的耗时。"""

    def before(self, session: Session, sc: StepContext) -> None:
        return None

    def after(self, session: Session, sc: StepContext, log) -> None:
        if sc.prefix is None:
            return
        name = sc.prefix.name
        if sc.decision is None:
            log(f"  files: {name}-raw.png, {name}-sent.png, {name}-payload.txt")
        else:
            log(f"  files: {name}-raw.png, {name}.png, {name}-sent.png, {name}-payload.txt, {name}-answers.json")
        log(Phases.format_timing(sc.timing))

    def on_error(self, session: Session, sc: StepContext, error: BaseException) -> None:
        return None


class StepPipeline:
    """按固定顺序跑四个阶段，中间件包在外面。"""

    def __init__(self, perceive, decide, act, memory, middleware=None):
        self.perceive = perceive
        self.decide = decide
        self.act = act
        self.memory = memory
        self.middleware = middleware if middleware is not None else [Timing(), StepFiles(), SessionReport(), StepLog()]

    def run(self, cfg, ctx, session: Session, sc: StepContext, log) -> bool:
        for mw in self.middleware:
            mw.before(session, sc)
        try:
            # 感知之后才有画面。换按法把决策停在「直接再按一次」，不再问分类器。
            self.perceive.run(cfg, ctx, session, sc, log)
            opened = sc.prefix is not None
            if opened:
                self._files().open_request(cfg, ctx, session, sc)
                if sc.keep_going and not sc.skip_decide:
                    self.decide.run(cfg, ctx, session, sc, log)
                if sc.decision is not None:
                    self._files().after_decide(session, sc)
                if sc.keep_going:
                    self.act.run(cfg, ctx, session, sc, log)
                if sc.what:
                    self.memory.run(session, sc, log)
        except Exception as exc:
            for mw in self.middleware:
                mw.on_error(session, sc, exc)
            raise
        # 连续没变化时，原来就不记这一步的耗时，也不写文件。截屏超时要记耗时。
        if sc.prefix is None and session.outcome != "capture timed out":
            return False
        self._timing().seal(session, sc)
        if sc.prefix is None:
            return False
        for mw in reversed(self.middleware):
            mw.after(session, sc, log)
        if session.view is None:
            perception.Capture.settle(sc.screen.image, cfg.delay)
        return sc.keep_going

    def _files(self) -> StepFiles:
        return next(mw for mw in self.middleware if isinstance(mw, StepFiles))

    def _timing(self) -> Timing:
        return next(mw for mw in self.middleware if isinstance(mw, Timing))
