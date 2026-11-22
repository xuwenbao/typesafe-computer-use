"""接管：分类器彻底做不完时，由更强的模型一步一步接着操作。

读这个文件时，先看清它不做什么。模型不能报坐标、选择器或命令。它只能做三件事：点屏幕上已经标出来的一项、按下面名单里的一个键、往文本框里打字。
每一步选出来之后，都先在这里和当前屏幕核对。对不上就不碰机器，并把原因写进历史。下一步模型读到的就是自己刚才为什么被拒绝。
"""

from __future__ import annotations

import json
import string
import time
from dataclasses import asdict

from . import driver_agent
from .actions import (
    WAIT_SECONDS,
    _paste_note,
    _return_note,
    add_item,
    click_item,
    clipboard_line,
    fill_field,
    focus_landed,
    noted,
)
from .calls import DRIVER
from .config import settings
from .models import AxNode, Item, Missed, Screen, signature
from .parse.marks import marks
from .perception import ocr_region, settle
from .platform_adapter import desktop
from .writer import DriveStep, denied_listed_names, looks_credential

# 模型可以按的单个键。名单以外的按键直接拒绝。start 在 Windows 上打开开始菜单，在 Mac 上打开 Spotlight。
# run 在 Windows 上打开运行对话框（Win+R），在 Mac 上同样打开 Spotlight。
KEYS = (
    "return",
    "escape",
    "tab",
    "up",
    "down",
    "left",
    "right",
    "pageup",
    "pagedown",
    "home",
    "end",
    "delete",
    "space",
    "backspace",
    "f2",
    "f5",
    "start",
    "run",
)
# 只在列表里移动的键。连续按它们是正常翻页，不计入“同一个动作重复”的闸。
NAVIGATION_KEYS = ("up", "down", "left", "right", "pageup", "pagedown", "home", "end")
MAX_SAME_ACTION = 4  # 同一个动作连续这么多次就停下，不论这几张屏幕像不像
MAX_REVISITS = 3  # 这次接管里，同一屏幕上同一个动作累计这么多次就换路，中间夹了别的动作也算
DENIED_SHORTCUTS = ("q", "w", "h", "m")  # 这四个快捷键会退出、关闭、隐藏或最小化窗口，所以不让模型按
QUIET_SHORTCUTS = ("c", "x")  # 复制和剪切只改剪贴板。屏幕可以不变，不能据此说这一步没效果
# 允许的 Cmd/Ctrl 组合：小写字母和数字，去掉上面四个。
SHORTCUTS = tuple(ch for ch in string.ascii_lowercase + string.digits if ch not in DENIED_SHORTCUTS)

# 点击和拖动的限制。模型只能点已经标出的项，拖动也不能一下离开屏幕。
MODIFIERS = ("", "add", "range")  # 普通点、追加到已有选择、从锚点延伸到这一项
# 模型会用键的名字。这里收成适配器真正按下的修饰：Control/Command 是追加，Shift 是延伸。
MODIFIER_ALIASES = {"control": "add", "ctrl": "add", "command": "add", "cmd": "add", "shift": "range"}
CLICKS = ("click", "double_click", "right_click")
# 这些动作用编号点名屏幕上的一项。执行前 target 必须对得上那一项的文字，对不上就不碰机器。
NAMED = frozenset({*CLICKS, "type", "drag", "set_value", "scroll_down", "scroll_up"})
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff\u2060"), None)
MAX_TEXT = 2000  # 一次输入的字数上限
DRAG_LIMIT = 600  # 按像素拖动时，横向和纵向都不能超过这么远

# 写进历史、以及什么情况下接管自己停下来。
MAX_UNUSABLE = 2  # 连续两次回复都读不成一个动作就停。一次还允许再试
PREFIX = "driver: "  # 每一行都带这个前缀，后面能分清是分类器做的还是接管做的
UNUSABLE = "the model's reply was unusable"  # 写进历史的英文句子，下一步模型会读到
UNMARKED = ("wait", "type")  # 等待本来就不改屏幕，输入会自己读回文本框，这两种不加“屏幕没变”
DONE_BEFORE = "this did nothing on this screen before"  # 这个动作在当前屏幕上做过且没效果。照做，结果行带上这句提醒
BANNED = "refused: this is the way that just looped; try another way"  # 换路之后，打转的动作在那一屏上不再执行
MAX_REROUTES = 1  # 一次接管里止损最多逼它换几次路；再触发就停

# 接管结束时告诉写手的英文说明。写手用它向用户解释这次是怎么停的，所以保持英文。
TOOK_OVER = "the classifier stopped short of the goal for good, a stronger model took over the actions, and "
STOPPED = {
    "driver done": TOOK_OVER + "it judged the goal reached on this screen",
    "driver gave up": TOOK_OVER + "it found no action that would get closer to the goal",
    "driver stalled": TOOK_OVER + "its last actions changed nothing",
    "driver step limit": TOOK_OVER + "it used every action it was allowed",
    "driver failed": TOOK_OVER + "the model it asks could not be reached",
}


def take_over(cfg, ctx, state, log, first: int) -> bool:
    """从第 `first` 步开始接管，直到模型说完成、放弃、卡住，或把自己的步数用完。

    返回 False 表示根本没开始：接管关着（步数为 0），或者没有写手可问。
    步数是接管自己的 `driver_steps`，不从分类器剩下的 `--steps` 里扣。每一步只问一个动作，做完再看新截图。
    真正开始之后，`state.outcome` 会改成上面 STOPPED 里的一种，做过的动作都在 `state.history` 里，每行以 `driver: ` 开头。
    """
    from . import runner  # runner 会调用本模块，顶层互相导入会成环，所以用到时再导入

    # 目标：确认这次接管可以开始。完成：步数为 0 或没有写手时返回 False，后面的循环不会跑。
    budget = cfg.driver_steps
    if budget <= 0 or ctx.writer is None:
        return False

    # 目标：记下分类器为什么停下，并准备接管自己的计数。
    # 完成：日志写出最多几步；空转、重复和换路从 0 计；结局先写成“步数用完”，中途完成或卡住再改。
    stopped = runner.STOPPED[state.outcome]
    model = driver_agent.model_for()
    if model is None:
        log("the driver needs CLICKER_WRITER_API=openai; stopping at the classifier")
        return False
    log(f"\nthe driver takes over, {budget} actions at most: {stopped}")
    state.idle = state.repeats = state.reroutes = 0
    state.banned = set()
    state.banned_on = state.change_course = None
    state.outcome = "driver step limit"
    unusable = 0
    cursor = first
    calls = 0
    reasoning = [settings().answer_reasoning]
    state.driver_talk = []
    same_streak = 0
    same_sig: str | None = None
    last_sig = ""
    started = len(state.seen)  # 累计重复只数这次接管做的动作，分类器做过的不算

    try:
        while cursor < first + budget:
            desktop.check_abort()

            # 目标：拿到这一步要看的屏幕。完成：连续没变化时第一次逼模型换路，第二次定为卡住并返回。
            seen = runner.look(cfg, ctx, state, log)
            if seen is None:
                if _loop(state, f"the last {runner.MAX_IDLE} actions changed nothing on screen", last_sig, log):
                    return True
                same_streak, same_sig = 0, None
                seen = state.view

            # 目标：把当前屏幕交给模型，并留下这一步的截图。
            # 完成：标出元素、存下发出去的图，并带上之前的屏幕、停下记录、还剩几步和换路说明。
            _mark_tried(state)
            screen, items = seen
            picture = marks(screen.image, items, ocr_region(screen))
            prefix = cfg.out / f"step-{cursor:03d}"
            prefix.with_name(prefix.name + "-sent.png").write_bytes(picture.png)
            earlier = runner.earlier_screens(state, signature(screen, items))
            earlier_stops = runner.stops_so_far(state)
            change = state.change_course
            state.change_course = None

            if not state.driver_talk:
                state.driver_talk.append(
                    driver_agent.opening_message(cfg.goal, state.history, stopped, earlier, earlier_stops, state.guidance)
                )
            told = _tell(
                model,
                state,
                screen,
                items,
                cfg,
                stopped,
                earlier,
                earlier_stops,
                first + budget - cursor,
                picture.image(),
                reasoning,
                log,
                change=change,
            )
            calls += told.calls or 1
            _tally(state, told)
            desktop.check_abort()
            if told.failed:
                log(f"  the driver failed: {told.error}")
                state.outcome = "driver failed"
                _write_call(
                    prefix, cursor, calls, {"step": cursor, "did": "driver failed", "error": told.error}, tools=told.tools
                )
                return True
            if told.unusable or told.move is None:
                # 目标：处理读不成动作的回复。完成：记进历史；连续两次把结局写成失败并停下，一次则再问。
                unusable += 1
                log(f"  the model's reply was unusable: {told.error or UNUSABLE}")
                _record(state, PREFIX + UNUSABLE)
                state.markable = False
                state.driver_talk.append({"role": "user", "content": [{"type": "input_text", "text": UNUSABLE}]})
                _write_call(
                    prefix,
                    cursor,
                    calls,
                    {"step": cursor, "did": UNUSABLE, "error": told.error or UNUSABLE},
                    tools=told.tools,
                )
                if unusable >= MAX_UNUSABLE:
                    state.outcome = "driver failed"
                    return True
                continue

            # 空白预览不能把名单上的名字说成没有。否定了它们就丢掉截图再判一次，第二次为准，第一个回复不执行。
            move = told.move
            tools = told.tools
            reasked = None
            reached = move.action == "done" and move.achieved
            denied = [] if reached else denied_listed_names(move.reason, False, items)
            if denied:
                told = _tell(
                    model,
                    state,
                    screen,
                    items,
                    cfg,
                    stopped,
                    earlier,
                    earlier_stops,
                    first + budget - cursor,
                    None,
                    reasoning,
                    log,
                    denied=denied,
                    change=change,
                    fresh=True,
                )
                calls += told.calls or 1
                _tally(state, told)
                desktop.check_abort()
                if told.failed:
                    log(f"  the driver failed: {told.error}")
                    state.outcome = "driver failed"
                    _write_call(prefix, cursor, calls, {"step": cursor, "did": "driver failed", "error": told.error})
                    return True
                if told.unusable or told.move is None:
                    unusable += 1
                    log(f"  the model's reply was unusable: {told.error or UNUSABLE}")
                    _record(state, PREFIX + UNUSABLE)
                    state.markable = False
                    if unusable >= MAX_UNUSABLE:
                        state.outcome = "driver failed"
                        return True
                    continue
                move = told.move
                tools = told.tools
                reasked = denied

            # 目标：执行模型选的动作，或在它宣布结束时停下来。
            # 完成：done / give_up 只改结局；其它动作占一行历史和一个预算，打转时先换路、再卡住。
            unusable = 0
            log(
                f"\ndriver step {cursor}: app={screen.app!r} items={len(items)} action={move.action} {_target(move)}\n  {move.reason}"
            )
            entry = {"step": cursor, **asdict(move), "did": None}
            if move.action in ("done", "give_up"):
                reached = move.action == "done" and move.achieved
                state.outcome = "driver done" if reached else "driver gave up"
                if reached:
                    state.driver_done = move.reason
                entry["did"] = state.outcome
                _write_call(prefix, cursor, calls, entry, reasked, tools)
                return True

            state.quiet = False
            what = _take(move, screen, items, state)
            entry["did"] = what
            _write_call(prefix, cursor, calls, entry, reasked, tools)
            # 复制、剪切只改剪贴板，屏幕不变不能记成空转。
            clipboard = "to the clipboard" in what
            state.view = None
            _record(state, PREFIX + what)
            state.markable = move.action not in UNMARKED and move.action != "set_value" and not _refused(what) and not clipboard
            state.quiet = clipboard
            log(f"  did: {what}")
            cursor += 1

            sig = _signature(move, items, screen.offscreen)
            last_sig = sig
            if _repeat(state, sig, move.action == "wait", log):
                if _loop(state, f"{runner.MAX_REPEATS} actions in a row were already taken on this screen", sig, log):
                    return True
                same_streak, same_sig = 0, None
            elif move.action != "wait" and _revisited(state, started, sig):
                if _loop(state, f"this action was already taken {MAX_REVISITS} times on this screen", sig, log):
                    return True
                same_streak, same_sig = 0, None
            else:
                same_streak, same_sig = _advance_same_action(same_streak, same_sig, move, items, screen.offscreen)
                if same_streak >= MAX_SAME_ACTION:
                    if _loop(state, f"the same action was taken {MAX_SAME_ACTION} times in a row", sig, log):
                        return True
                    same_streak, same_sig = 0, None
            settle(screen.image, cfg.delay)

        # 目标：步数用完时收尾。完成：保留一开始写下的 driver step limit，并返回 True。
        log(f"  the driver used all {budget} of its actions")
        return True
    finally:
        state.driver_actions = cursor - first
        state.driver_calls = calls


def _refused(what: str) -> bool:
    """这一行说的是没做成的动作：机器保持原样。"""
    return what.startswith("refused") or "refused:" in what


def _loop(state, why: str, sig: str, log) -> bool:
    """止损触发了。第一次逼模型换路并返回 False；这次接管里已经换过路，就定为卡住并返回 True。

    换路时把打转的动作记进 banned，此后只要回到打转的那一屏就拒绝它；空转、重复的计数清零，下一次提问带上换路说明。"""
    if state.reroutes >= MAX_REROUTES:
        log(f"  {why}; it already changed course, so it stops")
        state.outcome = "driver stalled"
        return True
    state.reroutes += 1
    state.banned = {sig} if sig else set()
    state.banned_on = state.last
    state.change_course = {
        "why": why,
        "do_not_repeat": sorted(state.banned),
        "tried_on_this_screen": _tried_report(state),
    }
    state.idle = state.repeats = 0
    state.outcome = "driver step limit"
    log(f"  {why}; changing course")
    _record(state, PREFIX + f"changed course: {why}")
    state.markable = False
    return False


def _banned_here(state, sig: str) -> bool:
    """这个动作是换路时打转的那一个，而现在又回到了打转的那一屏。两个页面来回跳时，回到原处也认得出来。"""
    from . import runner

    if sig not in state.banned or state.banned_on is None or state.last is None:
        return False
    return runner.same_screen(state.last, state.banned_on)


def _record(state, line: str) -> None:
    """记进历史，并在子会话里把这一行送到画中画蒙板上。"""
    state.history.append(line)
    from .windows_session import report_action

    report_action(line)


def _tell(
    model,
    state,
    screen,
    items,
    cfg,
    stopped,
    earlier,
    earlier_stops,
    steps_left,
    image,
    reasoning,
    log,
    denied=None,
    change=None,
    fresh: bool = False,
):
    """问这一张屏幕。`fresh` 时只发这一条、不带之前的对话，给丢掉截图再判一次用。"""
    packet = driver_agent.screen_packet(
        cfg.goal,
        screen,
        items,
        state.history,
        stopped,
        earlier,
        earlier_stops,
        state.guidance,
        steps_left,
        _tried_report(state),
        denied=denied,
        change_course=change,
    )
    message = driver_agent.screen_message(packet, image)
    messages = [message] if fresh else [*state.driver_talk, message]
    if not fresh:
        state.driver_talk.append(message)
    started = time.perf_counter()
    segment = driver_agent.run_segment(model, messages, driver_agent.Turn(screen, items), reasoning[0])
    if segment.drop_reasoning and reasoning[0]:
        log("  the endpoint rejected reasoning; asking again without it")
        reasoning[0] = None
        segment = driver_agent.run_segment(model, messages, driver_agent.Turn(screen, items), None)
    segment.elapsed = time.perf_counter() - started
    if segment.history is not None:
        state.driver_talk = segment.history
    return segment


def _tally(state, segment) -> None:
    """把这一段的请求、token 和工具调用记到运行状态上。"""
    model = settings().resolved_driver_model
    for usage in segment.usages:
        cached = int(getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0) or 0)
        incoming = int(getattr(usage, "input_tokens", 0) or 0)
        state.calls.used(model, max(incoming - cached, 0), cached, int(getattr(usage, "output_tokens", 0) or 0))
    state.calls.count[DRIVER] += segment.calls
    state.calls.seconds[DRIVER] += segment.elapsed
    for tool in segment.tools:
        name = tool["name"]
        state.driver_tool_calls[name] = state.driver_tool_calls.get(name, 0) + 1


def _write_call(prefix, step: int, call: int, action: dict, reasked: list[str] | None = None, tools: list | None = None) -> None:
    """一次模型调用一份记录，里面只有一个动作。再判一次时记下被否定的名字，工具按调用顺序列出。"""
    body = {"step": step, "call": call, "actions": [action]}
    if tools:
        body["tools"] = tools
    if reasked:
        body["reasked"] = reasked
    prefix.with_name(prefix.name + "-driver.json").write_text(json.dumps(body, indent=2), encoding="utf-8")


def load_driver_record(path) -> dict:
    """读一次接管调用。没有调用序号的旧文件当作一次调用、一个动作。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    if "call" in data and isinstance(data.get("actions"), list):
        return data
    return {"step": data.get("step"), "call": 1, "actions": [data]}


def _mark_tried(state) -> None:
    """上一动做完后，如果下一张截图显示屏幕没变，就把这个结论记到“已尝试”里。

    历史行上的“屏幕没变”是截图比较时加上的。这里把它同步到动作签名上，下一步才会拒绝再做同一个动作。"""
    from . import runner

    if not state.history or not state.seen or not state.history[-1].endswith(runner.UNCHANGED):
        return
    seen, what = state.seen[-1]
    if what and not what.endswith(runner.UNCHANGED):
        state.seen[-1] = (seen, what + runner.UNCHANGED)


def _tried_report(state) -> list[dict]:
    """当前这张屏幕上已经做过的动作，以及每一次有没有改变屏幕。发给模型，让它换一条路。"""
    from . import runner

    report = []
    for what in runner.tried_here(state):
        dead = what.endswith(runner.UNCHANGED)
        report.append({"action": what.removesuffix(runner.UNCHANGED), "screen_changed": not dead})
    return report


def _dead(state) -> set[str]:
    """当前屏幕上做过、而且没改变屏幕的动作。这些动作不会再执行一次。"""
    from . import runner

    return {what.removesuffix(runner.UNCHANGED) for what in runner.tried_here(state) if what.endswith(runner.UNCHANGED)}


def _modifier(name: str) -> str:
    """模型说的修饰键，换成适配器要按的那个。不认识的名字原样返回，后面会被拒绝。"""
    return MODIFIER_ALIASES.get(name, name)


def _signature(move: DriveStep, items: list[Item], offscreen: list[AxNode] | None = None) -> str:
    """把这一步收成一句不看措辞的签名。编号换成屏幕上的文字，这样“又做了同一个动作”才能对上。"""
    if move.action in CLICKS:
        item = next((it for it in items if str(it.index) == move.item), None)
        name = item.text if item is not None else f"#{move.item}"
        modifier = _modifier(move.modifier)
        extra = f" {modifier}" if modifier else ""
        return f"{move.action} {name!r}{extra}"

    if move.action == "focus_offscreen":
        nodes = offscreen or []
        node = nodes[int(move.item)] if move.item.isdigit() and int(move.item) < len(nodes) else None
        label = node.label if node is not None else f"#{move.item}"
        return f"focus_offscreen:{label}"

    if move.action in ("key", "shortcut"):
        return f"{move.action} {move.key}"

    if move.action in ("scroll_down", "scroll_up") and move.item:
        item = next((it for it in items if str(it.index) == move.item), None)
        name = item.text if item is not None else f"#{move.item}"
        return f"{move.action} {name!r}"

    if move.action == "type":
        if move.item:
            item = next((it for it in items if str(it.index) == move.item), None)
            name = item.text if item is not None else f"#{move.item}"
            return f"type {move.text!r} into {name!r}"
        return f"type {move.text!r}"

    if move.action == "drag":
        item = next((it for it in items if str(it.index) == move.item), None)
        start = item.text if item is not None else f"#{move.item}"
        return f"drag:{start}->{_drag_name(move, items)}"
    return move.action


def _take(move: DriveStep, screen: Screen, items: list[Item], state) -> str:
    """执行这一步。换路后在打转那一屏上又做打转的动作，就拒绝；在这一屏做过且没效果的照做，结果行带上提醒。"""
    sig = _signature(move, items, screen.offscreen)
    if _banned_here(state, sig):
        return BANNED
    what = act(move, screen, items)
    if sig in _dead(state) and not _refused(what):
        return noted(what, DONE_BEFORE)
    return what


def _advance_same_action(
    streak: int, previous: str | None, move: DriveStep, items: list[Item], offscreen: list[AxNode] | None
) -> tuple[int, str | None]:
    """连续同一个动作做了几次。等待和只翻页的方向键不计入，也不把计数清掉。

    这条不看两张截图像不像。结果页在加载时每一帧的文字都会变，按屏幕比较的闸因此数不到头。
    """
    if move.action == "wait" or (move.action == "key" and move.key in NAVIGATION_KEYS):
        return streak, previous
    sig = _signature(move, items, offscreen)
    return (streak + 1, sig) if sig == previous else (1, sig)


def _revisited(state, started: int, sig: str) -> bool:
    """这次接管里，当前这一屏上这个动作是不是已经做满了 MAX_REVISITS 次。

    连续重复由 `_repeat` 管。这里数的是中间夹了别的动作的情况：两个标签来回切时，每一步的屏幕都变了，
    连续计数一直是 0，但同一个动作在同一屏上已经出现了好几次。"""
    from . import runner

    if not sig or state.last is None:
        return False
    count = 0
    for seen, what in state.seen[started:]:
        if what and what.removesuffix(runner.UNCHANGED) == sig and runner.same_screen(seen, state.last):
            count += 1
    return count >= MAX_REVISITS


def _repeat(state, sig: str, waiting: bool, log) -> bool:
    """用和分类器同一条规则数重复。返回 True 表示打转了，由 `_loop` 决定换路还是停下。

    重复指的是：当前屏幕上这个动作以前做过。换到另一张屏幕不会把计数清掉，所以两个页面来回跳，绕第二圈时就会停。
    等待不算重复，连续等待由“屏幕没变”那条规则去限制。"""
    from . import runner

    # 目标：等待不计入重复。完成：只记下当时的屏幕，计数不变，接管继续。
    if waiting:
        state.seen.append((state.last, None))
        return False

    # 目标：数这个动作在当前屏幕上连续重复了几次。完成：够次数返回 True，否则记入已尝试并继续。
    prior = [what.removesuffix(runner.UNCHANGED) for what in runner.tried_here(state)]
    state.repeats = state.repeats + 1 if sig in prior else 0
    state.seen.append((state.last, sig))
    if state.repeats >= runner.MAX_REPEATS:
        log(f"  {runner.MAX_REPEATS} actions in a row already taken on the same screen")
        return True
    return False


def _target(move: DriveStep) -> str:
    """日志里跟在动作名后面的那一段：点的是哪一项、按的是哪个键、拖到哪里。"""
    if move.action in CLICKS:
        modifier = _modifier(move.modifier)
        return f"item={move.item}" + (f" modifier={modifier}" if modifier else "")
    if move.action == "focus_offscreen":
        return f"item={move.item}"
    if move.action in ("key", "shortcut"):
        return f"key={move.key}"
    if move.action in ("scroll_down", "scroll_up") and move.item:
        return f"item={move.item}"
    if move.action == "type":
        aimed = f"item={move.item} " if move.item else ""
        return f"{aimed}text={move.text!r}"
    if move.action == "drag":
        if move.to:
            return f"item={move.item} to={move.to}"
        return f"item={move.item} dx={move.dx} dy={move.dy}"
    return ""


def act(move: DriveStep, screen: Screen, items: list[Item]) -> str:
    """执行模型选的一个动作，并返回一句写进历史的话。

    先看当前屏幕允不允许。不允许就一句拒绝，机器保持原样。这句话会进历史，模型下一步能读到自己错在哪。
    """
    desktop.check_abort()
    refused = _target_refusal(move, items)
    if refused is not None:
        return refused

    # 目标：按动作种类分发。完成：点、输入、拖动、按键、滚动、聚焦或等待；名单外的动作一句拒绝，不碰机器。
    if move.action in CLICKS:
        return _click(move, screen, items)
    if move.action == "type":
        return _type(move, screen, items)
    if move.action == "drag":
        return _drag(move, screen, items)
    if move.action == "key":
        # 目标：只按名单里的单个键。完成：名单内按下并记下键名，名单外拒绝。
        if move.key not in KEYS:
            return f"key refused: {move.key!r} is not one of {', '.join(KEYS)}"
        note = _return_note(screen) if move.key == "return" else None
        desktop.press(move.key)
        return noted(f"pressed {move.key}", note)
    if move.action == "shortcut":
        # 目标：按一个允许的 Cmd/Ctrl 组合。完成：禁止退出类快捷键；复制和剪切在结果里标明屏幕可以不变。
        if move.key not in SHORTCUTS:
            return f"shortcut refused: {move.key!r} is not one of {', '.join(SHORTCUTS)}"
        if move.key in QUIET_SHORTCUTS:
            return clipboard_line(move.key, items)
        note = _paste_note(items) if move.key == "v" else None
        desktop.press(move.key, command=True)
        return noted(f"pressed command+{move.key}", note)
    if move.action in ("scroll_down", "scroll_up"):
        return _scroll(move, screen, items)
    if move.action == "set_value":
        return _set_value(move, screen, items)
    if move.action == "focus_offscreen":
        return _focus_offscreen(move, screen)
    if move.action == "wait":
        desktop.sleep_watching(WAIT_SECONDS)
        return "waited"
    return f"refused: {move.action!r} is not an action"


def _plain(text: str) -> str:
    """条目文字和 target 比之前先去掉零宽字符、首尾空白，并忽略大小写。"""
    return text.translate(_INVISIBLE).strip().casefold()


def _target_refusal(move: DriveStep, items: list[Item]) -> str | None:
    """带编号的动作，target 必须就是那一项上的字。空的、对不上的，都拒绝。

    找不到这个编号时返回 None，交给原来的分支去说“没有这一项”。一边包含另一边就算相符，
    这样“文档”对得上“文档 (已固定)”，去掉零宽字符后的“剪切”也对得上。
    """
    if move.action not in NAMED or not move.item:
        return None
    item = next((it for it in items if str(it.index) == move.item), None)
    if item is None:
        return None
    wanted = move.target.strip()
    if not wanted:
        return f"{move.action} refused: name item {move.item} in target"
    shown = _plain(item.text)
    asked = _plain(wanted)
    if shown and asked and (asked in shown or shown in asked):
        return None
    return f"{move.action} refused: item {move.item} is {item.text!r}, not {wanted!r}"


def _scroll(move: DriveStep, screen: Screen, items: list[Item]) -> str:
    """滚动。模型点了一项，就滚那一项所在的窗格；没点，就滚指针下的窗口。"""
    # 目标：决定滚哪一块、往哪边。完成：向下是负行数；点了不存在的项就拒绝，否则滚动并写明滚过哪一项。
    lines = -10 if move.action == "scroll_down" else 10
    point = None
    name = ""
    if move.item:
        item = next((it for it in items if str(it.index) == move.item), None)
        if item is None:
            return f"{move.action} refused: there is no item {move.item!r} on this screen"
        point = screen.to_points(item)
        name = f" over {item.text!r}"
    desktop.scroll(lines, at=point)
    return f"scrolled {'down' if lines < 0 else 'up'}{name}"


def _type(move: DriveStep, screen: Screen, items: list[Item]) -> str:
    """打字。没点名项目时，打进当前聚焦的文本框，并读回来核对；没看到文本框也照样送按键，结果行带提醒。

    数字旋钮、数字框、时间的某一格不是文本框。模型点了那一项，就先点它再送按键。这条路不读回，下一张截图才会显示有没有打进去。
    看起来像密码或密钥的文字，两种路都不打。
    """
    text = move.text
    # 目标：拦住空文本、过长文本。完成：不合规时直接拒绝，两种打字路径都不会执行。
    if not text.strip() or len(text) > MAX_TEXT:
        return f"type refused: the text must be 1 to {MAX_TEXT} characters"

    # 目标：把字打进模型点名的那一项。完成：先点中、清空再送按键；像密码的内容拒绝；不读回，下一张截图才知道有没有打进去。
    if move.item:
        item = next((it for it in items if str(it.index) == move.item), None)
        if item is None:
            return f"type refused: there is no item {move.item!r} on this screen"
        if looks_credential(item.text) or looks_credential(text):
            return f"type refused: {item.text!r} or the text looks like a credential"
        try:
            desktop.click_at(screen.to_points(item))
        except Missed as e:
            return f"type refused: {item.text!r} was not clicked, {e}"
        desktop.clear_field()
        desktop.type_text(text)
        return f"typed {text!r} into {item.text!r} (keystrokes, not read back)"

    # 目标：没点名项目时，打进当前聚焦的文本框。完成：像密码就拒绝；没看到文本框也照样送按键，结果行带提醒；有文本框就打入并读回来核对。
    field = screen.field
    if field is None or not field.is_text:
        if looks_credential(text):
            return "type refused: the text looks like a credential"
        desktop.type_text(text)
        return noted(f"typed {text!r} (keystrokes, not read back)", "no text field was seen focused")
    if looks_credential(field.label) or looks_credential(field.placeholder) or looks_credential(text):
        return f"type refused: {field.label!r} or the text looks like a credential"
    return f"typed {text!r} into {field.label!r} {fill_field(field, text)}"


def _whole_pixels(raw: str) -> int | None:
    """模型给的像素偏移。不是整数就返回 None，拖动会被拒绝。"""
    try:
        return int(raw)
    except ValueError:
        return None


def _on_screen(screen: Screen, point: tuple[float, float]) -> bool:
    """这个点还在当前截图里面。拖出截图的动作不做。"""
    x, y = point
    return 0 <= x <= screen.image.width and 0 <= y <= screen.image.height


def _drag_name(move: DriveStep, items: list[Item]) -> str:
    """拖动终点在签名里怎么写：落到某一项就用它的文字，按像素拖就写偏移。"""
    if move.to:
        item = next((it for it in items if str(it.index) == move.to), None)
        return item.text if item is not None else f"#{move.to}"
    return f"{move.dx},{move.dy}"


def _drag(move: DriveStep, screen: Screen, items: list[Item]) -> str:
    """从一个已有项目拖到另一个项目，或者按像素偏移拖一段。起点、终点都必须落在当前截图里，偏移也不能超过 DRAG_LIMIT。"""
    start = next((it for it in items if str(it.index) == move.item), None)
    # 目标：确认拖动起点在当前屏幕上。完成：没有这项或它不在截图里就拒绝。
    if start is None:
        return f"drag refused: there is no item {move.item!r} on this screen"
    if not _on_screen(screen, start.center):
        return f"drag refused: {start.text!r} is not on the screen"

    # 目标：算出落点。完成：拖到另一项，或按不超过 DRAG_LIMIT 的像素偏移；落点离开截图、没给终点，都拒绝。
    if move.to:
        end = next((it for it in items if str(it.index) == move.to), None)
        if end is None:
            return f"drag refused: there is no item {move.to!r} on this screen"
        if not _on_screen(screen, end.center):
            return f"drag refused: {end.text!r} is not on the screen"
        dest = screen.to_points(end)
        where = end.text
    elif move.dx.strip() or move.dy.strip():
        dx = _whole_pixels(move.dx) if move.dx.strip() else 0
        dy = _whole_pixels(move.dy) if move.dy.strip() else 0
        if dx is None or dy is None:
            return "drag refused: dx and dy are whole pixels"
        if abs(dx) > DRAG_LIMIT or abs(dy) > DRAG_LIMIT:
            return f"drag refused: a drag stays within {DRAG_LIMIT} pixels"
        cx, cy = start.center
        landed_at = (cx + dx, cy + dy)
        if not _on_screen(screen, landed_at):
            return "drag refused: the drag leaves the screen"
        dest = (landed_at[0] / screen.scale, landed_at[1] / screen.scale)
        where = f"{dx},{dy}"
    else:
        return "drag refused: name the item it ends on, or how far to drag"

    # 目标：从起点拖到落点。完成：拖成则写明拖到哪里；点不中则拒绝，机器保持原样。
    try:
        desktop.drag(screen.to_points(start), dest)
    except Missed as e:
        return f"drag refused: {start.text!r} was not dragged, {e}"
    return f"dragged {start.text!r} to {where!r}"


def _focus_offscreen(move: DriveStep, screen: Screen) -> str:
    """聚焦一个应用报出来、但当前画面里看不见的控件。只有下一次读取确认焦点真的落在它上面，才算成功。"""
    # 目标：让看不见的控件获得焦点。完成：编号不存在就拒绝；系统报了焦点且下一次读取能对上标签，才算成功。
    nodes = screen.offscreen
    node = nodes[int(move.item)] if move.item.isdigit() and int(move.item) < len(nodes) else None
    if node is None:
        return f"focus_offscreen refused: there is no off-screen control {move.item!r}"
    if node.enabled is False:
        return f"focus_offscreen refused: {node.label!r} is disabled"
    desktop.ax_operate(node.ref, "scroll_into_view")
    if desktop.ax_focus(node.ref) and focus_landed(node.label):
        return f"focused {node.label!r} (off-screen control)"
    return f"focus_offscreen refused: {node.label!r} did not take the focus"


def _set_value(move: DriveStep, screen: Screen, items: list[Item]) -> str:
    """把一项设成给定的值。滑块走范围并读回；文本框走现有设值。像密码、只读、不支持都拒绝。"""
    item = next((it for it in items if str(it.index) == move.item), None)
    if item is None:
        return f"set_value refused: there is no item {move.item!r} on this screen"
    if item.enabled is False:
        return f"set_value refused: {item.text!r} is disabled"
    if item.writable is False:
        return f"set_value refused: {item.text!r} is read-only"
    if looks_credential(move.text):
        return "set_value refused: that text looks like a password"
    ref = screen.ax_refs.get(item.index)
    if ref is None or not item.controls:
        return f"set_value refused: {item.text!r} cannot take a value"
    if "range_value" in item.controls:
        if item.low is not None and item.high is not None:
            try:
                number = float(move.text)
            except ValueError:
                return "set_value refused: the range wants a number"
            if number < float(item.low) or number > float(item.high):
                return f"set_value refused: {move.text!r} is outside {item.low} to {item.high}"
        if not desktop.ax_operate(ref, "set_range", move.text):
            return f"set_value refused: {item.text!r} did not take {move.text!r}"
        return f"set {move.text!r} on {item.text!r} through RangeValue"
    if "value" in item.controls:
        if not desktop.ax_set_value(ref, move.text):
            return f"set_value refused: {item.text!r} did not take {move.text!r}"
        read = desktop.ax_value(ref)
        if read is not None and read != move.text:
            return f"set_value refused: {item.text!r} read back {read!r}, not {move.text!r}"
        return f"set {move.text!r} on {item.text!r} through Value"
    return f"set_value refused: {item.text!r} cannot take a value"


def _click(move: DriveStep, screen: Screen, items: list[Item]) -> str:
    """单击、双击或右键当前屏幕上的一项。普通单击走分类器同一条点击路径；带修饰键、双击、右键在这里单独发。"""
    item = next((it for it in items if str(it.index) == move.item), None)
    # 目标：确认要点的项和修饰键都合法。完成：没有这项或修饰键不认识就拒绝。
    if item is None:
        return f"{move.action} refused: there is no item {move.item!r} on this screen"
    modifier = _modifier(move.modifier)
    if modifier not in MODIFIERS:
        return f"click refused: {move.modifier!r} is not a modifier"
    # 目标：普通单击走分类器同一条路径。追加选择走 AddToSelection，不靠按住 Control 再点一个很矮的文件名。
    # 完成：返回点击结果；双击、右键和带 Shift 的点击改走下面。
    if move.action == "click" and not move.modifier:
        return click_item(item, screen, items)
    if move.action == "click" and modifier == "add":
        return add_item(item, screen)

    # 目标：发出双击、右键或带修饰的点击。完成：点中则写明点法和项目；点不中则拒绝。
    button = "right" if move.action == "right_click" else "left"
    count = 2 if move.action == "double_click" else 1
    try:
        desktop.click_at(screen.to_points(item), button=button, count=count, modifier=modifier or None)
    except Missed as e:
        return f"{move.action} refused: {item.text!r} was not clicked, {e}"
    if move.action == "double_click":
        return f"double-clicked {item.text!r}"
    if move.action == "right_click":
        return f"right-clicked {item.text!r}"
    return f"clicked {item.text!r} with {modifier}"
