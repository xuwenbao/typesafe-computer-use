"""The driver: once the classifier has stopped short of the goal for good, a stronger model takes the
run's actions, one at a time, with the whole story of the run in front of it.

The classifier picks every step of a run it can finish. When it stops and the writer can no longer
send it back (the run is stuck, the writer has no focus left to give, or a focus led to no action),
the run would end short of the goal. With the driver on, the model reads what the classifier did,
where it stopped and why, and the screen now, and chooses the next action. The model never names a
coordinate, a selector, or a command: it names an item on the screen, a key from a short list, or
text for the focused field, and this module checks each against the screen before anything happens.
"""

from __future__ import annotations

import json
import string
from dataclasses import asdict

from .actions import WAIT_SECONDS, click_item, fill_field
from .calls import MeteredWriter
from .models import Item, Missed, Screen, signature
from .parse.marks import marks
from .perception import ocr_region
from .platform_adapter import desktop
from .writer import DriveStep, WriterError, compose_drive_step, looks_credential

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
)
DENIED_SHORTCUTS = ("q", "w", "h", "m")  # quit, close, hide, minimize
SHORTCUTS = tuple(ch for ch in string.ascii_lowercase + string.digits if ch not in DENIED_SHORTCUTS)
MODIFIERS = ("", "add", "range")  # plain, add to the selection, extend the selection
CLICKS = ("click", "double_click", "right_click")
MAX_TEXT = 2000
MAX_UNUSABLE = 2  # replies in a row that are not an action; the second ends the takeover
PREFIX = "driver: "  # on every history line the driver adds, so the story says who did what
UNUSABLE = "the model's reply was unusable"
UNMARKED = ("wait", "type")  # as runner.UNMARKED: a wait leaves the screen alone, typing reads its field back
DEAD = "refused: this did nothing on this screen before; try another way"

# What the final answer is told about how the run ended, by the driver's outcome.
TOOK_OVER = "the classifier stopped short of the goal for good, a stronger model took over the actions, and "
STOPPED = {
    "driver done": TOOK_OVER + "it judged the goal reached on this screen",
    "driver gave up": TOOK_OVER + "it found no action that would get closer to the goal",
    "driver stalled": TOOK_OVER + "its last actions changed nothing",
    "driver step limit": TOOK_OVER + "it used every action it was allowed",
    "driver failed": TOOK_OVER + "the model it asks could not be reached",
}


def take_over(cfg, ctx, state, log, first: int) -> bool:
    """Drive from step `first` until the driver is done, gives up, stalls, or runs out of actions.

    False when it never started: the run left no room, or there is no writer to ask. Otherwise
    `state.outcome` is one of STOPPED, and `state.history` holds every action it took.
    """
    from . import runner  # runner calls this module, so it cannot be imported at the top

    budget = min(cfg.driver_steps, cfg.steps - first + 1)
    if budget <= 0 or ctx.writer is None:
        return False
    writer = ctx.writer.as_driver() if isinstance(ctx.writer, MeteredWriter) else ctx.writer
    stopped = runner.STOPPED[state.outcome]
    log(f"\nthe driver takes over, {budget} actions at most: {stopped}")
    state.idle = state.repeats = 0
    state.outcome = "driver step limit"
    unusable = 0
    for step in range(first, first + budget):
        desktop.check_abort()
        seen = runner.look(cfg, ctx, state, log)
        if seen is None:
            state.outcome = "driver stalled"
            return True
        _mark_tried(state)
        screen, items = seen
        picture = marks(screen.image, items, ocr_region(screen))
        prefix = cfg.out / f"step-{step:03d}"
        prefix.with_name(prefix.name + "-sent.png").write_bytes(picture.png)
        earlier = runner.earlier_screens(state, signature(screen, items))
        earlier_stops = runner.stops_so_far(state)
        try:
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
        unusable = 0
        log(f"\ndriver step {step}: app={screen.app!r} items={len(items)} action={move.action} {_target(move)}\n  {move.reason}")
        record = {"step": step, **asdict(move), "did": None}
        try:
            if move.action == "done":
                state.outcome = "driver done" if move.achieved else "driver gave up"
                return True
            if move.action == "give_up":
                state.outcome = "driver gave up"
                return True
            what = _take(move, screen, items, state)
            record["did"] = what
            state.view = None
            state.history.append(PREFIX + what)
            state.markable = move.action not in UNMARKED and not what.startswith("refused:")
            log(f"  did: {what}")
            if _repeat(state, _signature(move, items), move.action == "wait", log):
                return True
        finally:
            prefix.with_name(prefix.name + "-driver.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        desktop.sleep_watching(cfg.delay)
    log(f"  the driver used all {budget} of its actions")
    return True


def _mark_tried(state) -> None:
    """Remember that the last action changed nothing, once the capture that followed it says so.

    The history line gains the mark in `screen_moved`. The same mark on the signature is what the
    next step refuses to repeat."""
    from . import runner

    if not state.history or not state.seen or not state.history[-1].endswith(runner.UNCHANGED):
        return
    seen, what = state.seen[-1]
    if what and not what.endswith(runner.UNCHANGED):
        state.seen[-1] = (seen, what + runner.UNCHANGED)


def _tried_report(state) -> list[dict]:
    """What was already done on the screen now showing, and whether each action changed it."""
    from . import runner

    report = []
    for what in runner.tried_here(state):
        dead = what.endswith(runner.UNCHANGED)
        report.append({"action": what.removesuffix(runner.UNCHANGED), "screen_changed": not dead})
    return report


def _dead(state) -> set[str]:
    from . import runner

    return {what.removesuffix(runner.UNCHANGED) for what in runner.tried_here(state) if what.endswith(runner.UNCHANGED)}


def _signature(move: DriveStep, items: list[Item]) -> str:
    """The action independent of how the attempt was worded, so a repeat is the same action."""
    if move.action in CLICKS:
        item = next((it for it in items if str(it.index) == move.item), None)
        name = item.text if item is not None else f"#{move.item}"
        extra = f" {move.modifier}" if move.modifier else ""
        return f"{move.action} {name!r}{extra}"
    if move.action in ("key", "shortcut"):
        return f"{move.action} {move.key}"
    if move.action == "type":
        return f"type {move.text!r}"
    return move.action


def _take(move: DriveStep, screen: Screen, items: list[Item], state) -> str:
    if _signature(move, items) in _dead(state):
        return DEAD
    return act(move, screen, items)


def _repeat(state, sig: str, waiting: bool, log) -> bool:
    """The classifier's repeat rule, counted on the driver's own signatures. True when it stalls.

    A repeat is an action already taken on this screen. Other screens do not wipe the count, so a
    loop through two screens stalls on the second time around (see `runner.repeating`)."""
    from . import runner

    if waiting:
        state.seen.append((state.last, None))
        return False
    prior = [what.removesuffix(runner.UNCHANGED) for what in runner.tried_here(state)]
    state.repeats = state.repeats + 1 if sig in prior else 0
    state.seen.append((state.last, sig))
    if state.repeats >= runner.MAX_REPEATS:
        log(f"  {runner.MAX_REPEATS} actions in a row already taken on the same screen; stopping")
        state.outcome = "driver stalled"
        return True
    return False


def _target(move: DriveStep) -> str:
    if move.action in CLICKS:
        return f"item={move.item}" + (f" modifier={move.modifier}" if move.modifier else "")
    if move.action in ("key", "shortcut"):
        return f"key={move.key}"
    if move.action == "type":
        return f"text={move.text!r}"
    return ""


def act(move: DriveStep, screen: Screen, items: list[Item]) -> str:
    """Take one action the model chose, once the screen shows it can be taken, and say what happened.

    An action the screen does not allow is refused without touching the machine, and the line says
    why, so the model reads its own mistake in the next step's story.
    """
    desktop.check_abort()
    if move.action in CLICKS:
        return _click(move, screen, items)
    if move.action == "type":
        field = screen.field
        if field is None or not field.is_text:
            return "type refused: no text field is focused"
        text = move.text
        if not text.strip() or len(text) > MAX_TEXT:
            return f"type refused: the text must be 1 to {MAX_TEXT} characters"
        if looks_credential(field.label) or looks_credential(field.placeholder) or looks_credential(text):
            return f"type refused: {field.label!r} or the text looks like a credential"
        return f"typed {text!r} into {field.label!r} {fill_field(field, text)}"
    if move.action == "key":
        if move.key not in KEYS:
            return f"key refused: {move.key!r} is not one of {', '.join(KEYS)}"
        desktop.press(move.key)
        return f"pressed {move.key}"
    if move.action == "shortcut":
        if move.key not in SHORTCUTS:
            return f"shortcut refused: {move.key!r} is not one of {', '.join(SHORTCUTS)}"
        desktop.press(move.key, command=True)
        return f"pressed command+{move.key}"
    if move.action == "scroll_down":
        desktop.scroll(-10)
        return "scrolled down"
    if move.action == "scroll_up":
        desktop.scroll(10)
        return "scrolled up"
    if move.action == "wait":
        desktop.sleep_watching(WAIT_SECONDS)
        return "waited"
    return f"refused: {move.action!r} is not an action"


def _click(move: DriveStep, screen: Screen, items: list[Item]) -> str:
    item = next((it for it in items if str(it.index) == move.item), None)
    if item is None:
        return f"{move.action} refused: there is no item {move.item!r} on this screen"
    if move.modifier not in MODIFIERS:
        return f"click refused: {move.modifier!r} is not a modifier"
    if move.action == "click" and not move.modifier:
        return click_item(item, screen)
    button = "right" if move.action == "right_click" else "left"
    count = 2 if move.action == "double_click" else 1
    try:
        desktop.click_at(screen.to_points(item), button=button, count=count, modifier=move.modifier or None)
    except Missed as e:
        return f"{move.action} refused: {item.text!r} was not clicked, {e}"
    if move.action == "double_click":
        return f"double-clicked {item.text!r}"
    if move.action == "right_click":
        return f"right-clicked {item.text!r}"
    return f"clicked {item.text!r} with {move.modifier}"
