"""接管这个角色。导入本模块不会加载模型 SDK；`handle` 真正开始接管时才导入 `takeover`。"""

from __future__ import annotations

from ...config import settings
from ...perception import pipeline as perception
from ..base import register
from ..outcomes import CLASSIFIER_STOPPED
from ..policy.stages import screen_moved
from ..status import Status


def ready(cfg, ctx) -> bool:
    """接管开着、是真操作、写手可用，而且写手走的是 OpenAI 接口。演练和重放不接管。"""
    return cfg.driver_steps > 0 and cfg.act and not cfg.replay and ctx.writer is not None and settings().writer_api == "openai"


def wants(session) -> bool:
    """要不要交给接管模型。以下条件全部满足才接管：

    - 接管功能开着（driver_steps > 0），而且是真操作（不是演练或重放）；
    - 写手可用，走 OpenAI 接口，并且判断目标还没完成，去向也不是结束；
    - 分类器停在 CLASSIFIER_STOPPED 里的任一结局，包括把 --steps 走满。接管用自己的步数，不占用 --steps。

    其它条件都满足、只差接口是 Anthropic 时，记一行日志，说明驱动需要 OpenAI 接口。
    """
    cfg, ctx, log = session.cfg, session.ctx, session.log
    would = (
        cfg.driver_steps > 0
        and cfg.act
        and not cfg.replay
        and ctx.writer is not None
        and session.answer is not None
        and not session.answer.achieved
        and session.route != "end"
        and session.outcome in CLASSIFIER_STOPPED
    )
    if would and settings().writer_api != "openai":
        if log is not None:
            log("the driver needs CLICKER_WRITER_API=openai; stopping at the classifier")
        return False
    return would and ready(cfg, ctx)


def look(session):
    """接管模型每一步用的截屏和识别。

    和分类器的每一步一样，也会检查“连续几次动作屏幕都没变”。卡住了就返回 None（见 screen_moved）。
    识别沿用这次运行的缓存，只重读变过的区域，图标说明也不重算。
    """
    cfg, ctx, log = session.cfg, session.ctx, session.log
    screen = perception.capture(cfg.image, cfg.app, cfg.url, ctx.browser)
    items = perception.perceive(screen, settings().max_options, cfg.goal, cache=None if cfg.replay else session.parse_cache)
    session.view = (screen, items)
    return (screen, items) if screen_moved(session, screen, items, log) else None


def _takeover():
    """The takeover. Imported here, not at startup: it pulls in the agents SDK, which the run
    pays for only when the classifier has stopped and a takeover is actually going to happen."""
    from . import takeover

    return takeover


@register("driver")
class DriverAgent:
    def handle(self, session) -> Status:
        cfg, ctx, log = session.cfg, session.ctx, session.log
        try:
            takeover = _takeover()
        except (ImportError, OSError) as exc:
            # 冻结包里可能缺 SDK 要读的数据文件。分类器已经答过了，不能因为导入失败把这次运行丢掉。
            session.driver_error = str(exc)
            log(f"takeover unavailable: {exc}")
            return Status.FINISH
        if takeover.take_over(cfg, ctx, session, log, session.step + 1):
            return Status.FINAL_ANSWER
        return Status.FINISH
