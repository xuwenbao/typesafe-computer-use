"""接管：分类器彻底做不完时，由更强的模型一步一步接着操作。

读这个文件时，先看清它不做什么。模型不能报坐标、选择器或命令。它只能做三件事：点屏幕上已经标出来的一项、按下面名单里的一个键、往文本框里打字。
每一步选出来之后，都先在这里和当前屏幕核对。对不上就不碰机器，并把原因写进历史。下一步模型读到的就是自己刚才为什么被拒绝。
"""

from __future__ import annotations

import json
import string
from dataclasses import asdict

from .actions import WAIT_SECONDS, click_item, fill_field, focus_landed
from .calls import MeteredWriter
from .models import AxNode, Item, Missed, Screen, signature
from .parse.marks import marks
from .perception import ocr_region
from .platform_adapter import desktop
from .writer import DriveStep, WriterError, compose_drive_step, looks_credential

# 模型可以按的单个键。名单以外的按键直接拒绝。start 在 Windows 上打开开始菜单，在 Mac 上打开 Spotlight。
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
)
DENIED_SHORTCUTS = ("q", "w", "h", "m")  # 这四个快捷键会退出、关闭、隐藏或最小化窗口，所以不让模型按
QUIET_SHORTCUTS = ("c", "x")  # 复制和剪切只改剪贴板。屏幕可以不变，不能据此说这一步没效果
# 允许的 Cmd/Ctrl 组合：小写字母和数字，去掉上面四个。
SHORTCUTS = tuple(ch for ch in string.ascii_lowercase + string.digits if ch not in DENIED_SHORTCUTS)

# 点击和拖动的限制。模型只能点已经标出的项，拖动也不能一下离开屏幕。
MODIFIERS = ("", "add", "range")  # 普通点、追加到已有选择、从锚点延伸到这一项
# 模型会用键的名字。这里收成适配器真正按下的修饰：Control/Command 是追加，Shift 是延伸。
MODIFIER_ALIASES = {"control": "add", "ctrl": "add", "command": "add", "cmd": "add", "shift": "range"}
CLICKS = ("click", "double_click", "right_click")
MAX_TEXT = 2000  # 一次输入的字数上限
DRAG_LIMIT = 600  # 按像素拖动时，横向和纵向都不能超过这么远

# 写进历史、以及什么情况下接管自己停下来。
MAX_UNUSABLE = 2  # 连续两次回复都读不成一个动作就停。一次还允许再试
PREFIX = "driver: "  # 每一行都带这个前缀，后面能分清是分类器做的还是接管做的
UNUSABLE = "the model's reply was unusable"  # 写进历史的英文句子，下一步模型会读到
UNMARKED = ("wait", "type")  # 等待本来就不改屏幕，输入会自己读回文本框，这两种不加“屏幕没变”
DEAD = "refused: this did nothing on this screen before; try another way"  # 这个动作在当前屏幕上做过且没效果，拒绝再做

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
    步数是接管自己的 `driver_steps`，不从分类器剩下的 `--steps` 里扣。
    真正开始之后，`state.outcome` 会改成上面 STOPPED 里的一种，做过的动作都在 `state.history` 里，每行以 `driver: ` 开头。
    """
    from . import runner  # runner 会调用本模块，顶层互相导入会成环，所以用到时再导入

    # 目标：确认这次接管可以开始。完成：步数为 0 或没有写手时返回 False，后面的循环不会跑。
    budget = cfg.driver_steps
    if budget <= 0 or ctx.writer is None:
        return False

    # 目标：记下分类器为什么停下，并准备接管自己的计数。
    # 完成：日志写出最多几步；空转和重复从 0 计；结局先写成“步数用完”，中途完成或卡住再改。
    writer = ctx.writer.as_driver() if isinstance(ctx.writer, MeteredWriter) else ctx.writer
    stopped = runner.STOPPED[state.outcome]
    log(f"\nthe driver takes over, {budget} actions at most: {stopped}")
    state.idle = state.repeats = 0
    state.outcome = "driver step limit"
    unusable = 0

    for step in range(first, first + budget):
        desktop.check_abort()

        # 目标：拿到这一步要看的屏幕。完成：连续没变化就定为卡住并返回，不再问模型。
        seen = runner.look(cfg, ctx, state, log)
        if seen is None:
            state.outcome = "driver stalled"
            return True

        # 目标：把当前屏幕交给模型，并留下这一步的截图。
        # 完成：标出元素、存下发出去的图，并带上之前的屏幕、停下记录和还剩几步。
        _mark_tried(state)
        screen, items = seen
        picture = marks(screen.image, items, ocr_region(screen))
        prefix = cfg.out / f"step-{step:03d}"
        prefix.with_name(prefix.name + "-sent.png").write_bytes(picture.png)
        earlier = runner.earlier_screens(state, signature(screen, items))
        earlier_stops = runner.stops_so_far(state)

        try:
            # 目标：让模型选出下一个动作。完成：得到一个 DriveStep；读不成动作则进入下面的失败处理。
            move = compose_drive_step(
                writer,
                cfg.goal,
                screen,
                items,
                state.history,
                stopped,
                earlier,
                earlier_stops,
                state.guidance,
                first + budget - step,
                picture.image(),
                _tried_report(state),
            )
        except WriterError as e:
            # 目标：处理读不成动作的回复。完成：记进历史；连续两次把结局写成失败并停下，一次则跳过这一步再问。
            unusable += 1
            log(f"  the model's reply was unusable: {e}")
            state.history.append(PREFIX + UNUSABLE)
            state.markable = False
            record = {"step": step, "did": UNUSABLE, "error": str(e)}
            prefix.with_name(prefix.name + "-driver.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
            if unusable >= MAX_UNUSABLE:
                state.outcome = "driver failed"
                return True
            continue

        # 目标：执行模型选的动作，或在它宣布结束时停下来。
        # 完成：done / give_up 只改结局；其它动作写入带 driver: 前缀的历史，重复过多则卡住返回。
        unusable = 0
        log(f"\ndriver step {step}: app={screen.app!r} items={len(items)} action={move.action} {_target(move)}\n  {move.reason}")
        record = {"step": step, **asdict(move), "did": None}
        try:
            # 目标：模型宣布结束时不再操作机器。完成：达成写成 driver done，没达成或放弃写成 driver gave up。
            if move.action == "done":
                state.outcome = "driver done" if move.achieved else "driver gave up"
                return True
            if move.action == "give_up":
                state.outcome = "driver gave up"
                return True

            # 目标：真正做这一步，并让下一步知道它有没有效果。
            # 完成：历史多一行；复制和剪切不算空转；做过动作后当前截图作废，等页面稳定再截下一步。
            state.quiet = False
            what = _take(move, screen, items, state)
            record["did"] = what
            # 复制、剪切只改剪贴板，屏幕不变不能记成空转。
            clipboard = move.action == "shortcut" and move.key in QUIET_SHORTCUTS and what.startswith("pressed ")
            state.view = None
            state.history.append(PREFIX + what)
            state.markable = move.action not in UNMARKED and not what.startswith("refused:") and not clipboard
            state.quiet = clipboard
            log(f"  did: {what}")
            if _repeat(state, _signature(move, items, screen.offscreen), move.action == "wait", log):
                return True
        finally:
            prefix.with_name(prefix.name + "-driver.json").write_text(json.dumps(record, indent=2), encoding="utf-8")

        desktop.sleep_watching(cfg.delay)

    # 目标：步数用完时收尾。完成：保留一开始写下的 driver step limit，并返回 True，调用方会再让写手看最后一屏。
    log(f"  the driver used all {budget} of its actions")
    return True


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
    """执行这一步。如果它在当前屏幕上已经做过且没效果，直接拒绝，不再碰机器。"""
    if _signature(move, items, screen.offscreen) in _dead(state):
        return DEAD
    return act(move, screen, items)


def _repeat(state, sig: str, waiting: bool, log) -> bool:
    """用和分类器同一条规则数重复。返回 True 表示接管因此卡住并停下。

    重复指的是：当前屏幕上这个动作以前做过。换到另一张屏幕不会把计数清掉，所以两个页面来回跳，绕第二圈时就会停。
    等待不算重复，连续等待由“屏幕没变”那条规则去限制。"""
    from . import runner

    # 目标：等待不计入重复。完成：只记下当时的屏幕，计数不变，接管继续。
    if waiting:
        state.seen.append((state.last, None))
        return False

    # 目标：数这个动作在当前屏幕上连续重复了几次。完成：够次数就把结局写成卡住并停下，否则记入已尝试并继续。
    prior = [what.removesuffix(runner.UNCHANGED) for what in runner.tried_here(state)]
    state.repeats = state.repeats + 1 if sig in prior else 0
    state.seen.append((state.last, sig))
    if state.repeats >= runner.MAX_REPEATS:
        log(f"  {runner.MAX_REPEATS} actions in a row already taken on the same screen; stopping")
        state.outcome = "driver stalled"
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
        desktop.press(move.key)
        return f"pressed {move.key}"
    if move.action == "shortcut":
        # 目标：按一个允许的 Cmd/Ctrl 组合。完成：禁止退出类快捷键；复制和剪切在结果里标明屏幕可以不变。
        if move.key not in SHORTCUTS:
            return f"shortcut refused: {move.key!r} is not one of {', '.join(SHORTCUTS)}"
        desktop.press(move.key, command=True)
        line = f"pressed command+{move.key}"
        if move.key in QUIET_SHORTCUTS:
            return f"{line} (to the clipboard; the screen need not change)"
        return line
    if move.action in ("scroll_down", "scroll_up"):
        return _scroll(move, screen, items)
    if move.action == "focus_offscreen":
        return _focus_offscreen(move, screen)
    if move.action == "wait":
        desktop.sleep_watching(WAIT_SECONDS)
        return "waited"
    return f"refused: {move.action!r} is not an action"


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
    """打字。没点名项目时，打进当前聚焦的文本框，并读回来核对。

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

    # 目标：没点名项目时，打进当前聚焦的文本框。完成：没有文本框或像密码就拒绝；否则打入并读回来核对。
    field = screen.field
    if field is None or not field.is_text:
        return "type refused: no text field is focused"
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
    if desktop.ax_focus(node.ref) and focus_landed(node.label):
        return f"focused {node.label!r} (off-screen control)"
    return f"focus_offscreen refused: {node.label!r} did not take the focus"


def _click(move: DriveStep, screen: Screen, items: list[Item]) -> str:
    """单击、双击或右键当前屏幕上的一项。普通单击走分类器同一条点击路径；带修饰键、双击、右键在这里单独发。"""
    item = next((it for it in items if str(it.index) == move.item), None)
    # 目标：确认要点的项和修饰键都合法。完成：没有这项或修饰键不认识就拒绝。
    if item is None:
        return f"{move.action} refused: there is no item {move.item!r} on this screen"
    modifier = _modifier(move.modifier)
    if modifier not in MODIFIERS:
        return f"click refused: {move.modifier!r} is not a modifier"
    # 目标：普通单击走分类器同一条路径。完成：返回点击结果；双击、右键和带修饰的点击改走下面。
    if move.action == "click" and not move.modifier:
        return click_item(item, screen)

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
