"""一次运行的会话，以及一步之内传递的上下文。

`Session` 就是原来的 `RunState`：字段名和默认值都不变，接管模块仍按这些名字读写。
`cfg`、`ctx`、`log`、`step` 是主循环放进来的，不写进 `run.json`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..calls import Calls
from ..models import Guidance, Item, Screen, Signature
from ..perception import ParseCache
from ..writer import Answer


@dataclass(frozen=True)
class Handoff:
    """一次“交回”：分类器停下后，写手给了提示，让分类器继续干。"""

    step: int  # 在第几步停下的
    outcome: str  # 分类器停下的原因
    focus: str  # 写手给的提示（下一步该关注什么）
    actions: int  # 当时一共做了多少个动作；之后若没再增加，说明这个提示没起作用
    page: tuple[str, str | None] = ("", None)  # 交回时这一屏的（应用名，网址）


@dataclass
class Session:
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
    driver_error: str = ""  # 接管模块加载失败的原因；空表示没去加载，或加载成功
    driver_actions: int = 0  # 接管实际执行的动作数，含被拒绝的
    driver_calls: int = 0  # 接管问模型的次数
    driver_done: str = ""  # 接管判定完成时给出的理由；空表示它没有这么判定
    driver_talk: list = field(default_factory=list)  # 接管和模型的对话；屏幕消息和工具结果都在这里
    driver_tool_calls: dict = field(default_factory=dict)  # 这次接管里每个工具被调用的次数
    reroutes: int = 0  # 这次接管里止损已经逼它换过几次路；再触发一次就停
    banned: set[str] = field(default_factory=set)  # 换路时打转的动作签名；在打转的那一屏上不许再做
    banned_on: Signature | None = None  # 打转的那一屏；只在这一屏上拒绝 banned 里的动作
    change_course: dict | None = None  # 下一次问接管模型时带上的换路说明；问过一次就清掉
    cfg: object = None  # 这次运行的 RunConfig。主循环放进来，不写进 run.json
    ctx: object = None  # 执行动作用的 Context
    log: object = None  # 运行日志
    step: int = 0  # 刚跑过的分类器步号。0 表示还没跑
    alt: dict | None = None  # 下一步对同一条目换按法：index、method、text。不问分类器
    swapped: set = field(default_factory=set)  # 这一屏已经换过按法的条目编号
    press_tried: list = field(default_factory=list)  # 换过仍无效时，两种按法都算已尝试
    last_press: dict | None = None  # 刚点过的条目。下一屏没变、而且走的是辅助功能时，换成鼠标点击


@dataclass
class StepContext:
    """一步之内在各阶段之间传递的数据。一步结束就丢掉。"""

    step: int
    timing: dict[str, float] = field(default_factory=dict)
    screen: Screen | None = None
    items: list[Item] = field(default_factory=list)
    tried: list[str] = field(default_factory=list)
    picture: object = None
    decision: object = None
    what: str = ""
    keep_going: bool = True
    prefix: Path | None = None
    started: float = 0.0
    method: str = "auto"  # click_item 的按法。换按法时是 pointer 或 accessibility
    skip_decide: bool = False  # 换按法不再问分类器
