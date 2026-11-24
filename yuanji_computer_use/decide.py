"""The TypeSafe side: state, criteria, and the one multi-Choice request."""

from __future__ import annotations

import base64
import logging
from collections import Counter
from dataclasses import dataclass

from typesafe_sdk import Choice, ChoiceAnswer, Noul, TypeSafeAPIError, TypeSafeClient

from .config import SITES
from .dates import date_hints, now_context
from .models import AxNode, Field, Guidance, Item, Screen
from .parse.marks import Marked

log = logging.getLogger(__name__)

STOP_KINDS = ("done", "none")
SCREEN_IMAGE = "screen_image"  # the state key that refers to the picture in the request's assets
IMAGES_HEADER = "TypeSafe-Danger-Zone-Images"
# What in a 400's message says the picture, or images at all, were the trouble. A 413 always is:
# the picture is most of the request.
IMAGE_WORDS = ("image", "asset", "danger-zone", "danger_zone", SCREEN_IMAGE, "png", "base64")
OFFSCREEN_PREFIX = "offscreen:"
PRESS_OFFSCREEN = (
    "Activate a labelled control that the app exposes but that is not currently visible on screen "
    "(chosen in the offscreen question). Use when the needed control is known to exist but is "
    "scrolled out of view or not yet shown."
)

# Said only while a focus is set, so a run the writer never steered asks the question it always asked.
FOCUS_RULE = (
    " The current focus is the next step on the way to the goal, set by a reviewer that read the "
    "screen when you last stopped: work toward it. 'done' still means the goal itself, not the focus."
)
# Said only while a plan has a current subgoal, so a run with no plan asks the question it always asked.
SUBGOAL_RULE = (
    " The current_subgoal in the state is the next step toward the goal: take the action it names, "
    "on the control it names. 'done' still means the user's goal, not this subgoal."
)


def fixed_actions(browser: str, email: str | None, typing: bool = True) -> dict[str, str]:
    """Deterministic actions offered alongside click_item. Keep them mutually exclusive.

    Typing is offered only while a text field is focused. Without one the action is refused, and
    offering it spends the step on that refusal.
    """
    actions = {
        "use_browser": (
            f"Work in {browser}: bring it to the front, and open a website there if one is needed. The "
            "site question says which website, or says that the page already open there is the one to "
            "continue with. This is the only way to reach a website: never click the address bar, a URL, "
            "or a search box to get there. Works from any app, including this one."
        ),
        "press_enter": ("Press Return to submit the focused form or field. When a menu item is focused, Return takes that item."),
        "press_escape": "Press Escape to dismiss a dialog, menu, or popup.",
        "go_back": (
            "Go back to the previous page or screen, as the browser's Back button does. Use when the last "
            "click led somewhere that does not help and the page before it did."
        ),
        "scroll_down": "Scroll down to reveal more of the page.",
        "scroll_up": "Scroll up.",
        "wait": "Nothing to do yet; the screen is still loading or changing.",
        "done": "The goal is already achieved.",
        "none": "Nothing on screen or in this list helps with the goal.",
    }
    if typing:
        actions["type_text"] = (
            "Type free text into the focused text field. A writing model composes the text from the "
            "goal and the field's label. Only valid when a text field is focused and needs content."
        )
        if email:
            actions["type_email"] = (
                "Type the user's email address into the focused text field. Use this, not type_text, "
                "whenever the field wants an email or username."
            )
    return actions


def kind_criteria(
    browser: str, email: str | None, offscreen: bool = False, typing: bool = True, desktop: bool = False
) -> dict[str, str]:
    clicks = {
        "click_item": (
            "Click one of the on-screen text items (chosen in the item question). When more than one "
            "file is already selected, do not click a different row: cut or copy the selection, or "
            "click one of the selected rows to keep only that one."
        )
    }
    if desktop:
        clicks["open_item"] = (
            "Open one of the on-screen items (chosen in the item question) by double-clicking it: a file, "
            "a folder, or a list entry that a single click only selects."
        )
        clicks["add_item"] = (
            "Click one on-screen item while holding the selection modifier (Control, or Command on a Mac), "
            "so it is added to the current selection. A plain click replaces the selection. Add only a "
            "file the goal names. When the goal asks for one kind of file, such as .docx, do not add "
            "other files in the same folder. The item is chosen in the item question."
        )
        clicks["shortcut"] = (
            "Press Control (Command on a Mac) together with the key chosen in the key question: cut, copy, "
            "paste, or select all. Use it once a file or a list entry is selected."
        )
    if offscreen:
        clicks["press_offscreen"] = PRESS_OFFSCREEN
    return {**clicks, **fixed_actions(browser, email, typing)}


ROW_MATES = 3  # how many neighbours name a duplicated item's row in a criterion; the history line takes them all


def row_mates(items: list[Item], limit: int | None = ROW_MATES) -> dict[int, list[str]]:
    """Item index -> the texts sharing its row, left to right, for every item whose text another item repeats.

    Three rows of events each end in a 'Buy'. The label says nothing about which; the row does,
    and the row is a fact the layout holds, so the code reads it and hands it over. `limit`
    keeps a criterion short; None takes the whole row, for a line that has to identify it.
    """
    counts = Counter(it.text for it in items)
    out: dict[int, list[str]] = {}
    for it in items:
        if counts[it.text] < 2:
            continue
        cy, half = it.center[1], max(1.0, it.y2 - it.y1) / 2
        mates = sorted((o for o in items if o is not it and abs(o.center[1] - cy) < half), key=lambda o: o.x1)
        if mates:
            out[it.index] = [o.text for o in mates[:limit]]
    return out


def item_criteria(screen: Screen, items: list[Item], only: list[Item] | None = None) -> dict[str, str]:
    """Each item as one line. A role prefix marks the ones the app itself declared, and "icon"
    an icon named by what an image model saw in it rather than by text on the screen. A
    duplicated label carries its row, and one under a popup says which.

    `only` lists a subset. The numbers stay the indexes they have in `items`.
    """
    shown = items if only is None else only
    hints = date_hints(items, screen)
    mates = row_mates(items)
    return {
        str(it.index): (
            f"{it.role + ' ' if it.role and (it.from_ax or it.source == 'icon') else ''}{it.text!r} "
            f"({screen.region(it)}"
            f"{'; ' + hints[it.index] if it.index in hints else ''}"
            f"{'; in the row of ' + ', '.join(repr(t) for t in mates[it.index]) if it.index in mates else ''}"
            f"{'; selected' if it.selected else ''}"
            f"{'; under ' + screen.covered[it.index].title if it.index in screen.covered else ''})"
        )
        for it in shown
    }


def offscreen_criteria(nodes: list[AxNode], banned: set[str] | None = None) -> dict[str, str]:
    """Each off-screen control as one line, keyed by its position in `screen.offscreen`.

    `banned` drops labels refused on this screen. The keys that remain are still the indexes in
    `nodes`, so a later press finds the same control.
    """
    skip = banned or set()
    return {str(i): f"{node.role_word} {node.label!r} (not visible)" for i, node in enumerate(nodes) if node.label not in skip}


def offscreen_records(nodes: list[AxNode], banned: set[str] | None = None) -> list[dict]:
    """The same controls as state, with the key the offscreen question answers with.

    `banned` drops refused labels and keeps the original indexes, the same way the question does.
    """
    skip = banned or set()
    return [{"k": i, "role": node.role_word, "label": node.label} for i, node in enumerate(nodes) if node.label not in skip]


# The file-list chords. Quit, close, hide, and minimize are not here; the driver keeps its own wider list.
SHORTCUT_KEYS = {
    "x": "Cut the selection.",
    "c": "Copy the selection.",
    "v": "Paste.",
    "a": "Select all.",
}


def key_criteria() -> dict[str, str]:
    """Which key a desktop shortcut presses with Control, or with Command on a Mac."""
    return dict(SHORTCUT_KEYS)


def site_criteria() -> dict[str, str]:
    """Which website use_browser opens. The catalog, plus one key for anything else and one for nothing."""
    return {
        **SITES,
        "other": "A website is needed to progress the goal, but it is not one of the sites named in this list.",
        "none": "No website needs to be opened: the page already open in the browser is the one to continue with.",
    }


def base_state(
    goal: str,
    screen: Screen,
    items: list[Item],
    history: list[str],
    tried: list[str] | None = None,
    guidance: Guidance | None = None,
    subgoal: dict | None = None,
    banned_offscreen: set[str] | None = None,
) -> dict:
    """The facts the classifier reads. `tried` lists the actions already taken on this same screen
    earlier in the run, each of which led back here: a fact the code knows and the model cannot.
    `guidance` is what the writer and the user added to the goal when the classifier last stopped.
    `subgoal` is the plan's current step. With no plan it is omitted, and the packet is unchanged.
    `banned_offscreen` drops labels refused on this screen from `offscreen_controls`."""
    hints = date_hints(items, screen)
    mates = row_mates(items)
    offscreen = offscreen_records(screen.offscreen, banned_offscreen)
    return {
        "goal": goal,
        **(guidance.state() if guidance else {}),
        **({"current_subgoal": subgoal} if subgoal else {}),
        "now": now_context(),
        "frontmost_app": screen.app,
        "browser_active_tab_url": screen.url,
        "focused_field": screen.field.summary() if screen.field else None,
        "previous_actions": history[-8:],
        "already_tried_on_this_screen": list(tried or []),
        "screen_items_in_reading_order": [
            {
                "i": it.index,
                "text": it.text,
                "where": screen.region(it),
                **({"role": it.role} if it.role else {}),
                **({"when": hints[it.index]} if it.index in hints else {}),
                **({"beside": mates[it.index]} if it.index in mates else {}),
                **({"under": screen.covered[it.index].title} if it.index in screen.covered else {}),
            }
            for it in items
        ],
        **({"offscreen_controls": offscreen} if offscreen else {}),
    }


@dataclass(frozen=True)
class Decision:
    kind: ChoiceAnswer
    item: ChoiceAnswer | None
    site: ChoiceAnswer
    offscreen: ChoiceAnswer | None = None
    key: ChoiceAnswer | None = None
    image_refused: str | None = None  # why System One refused the picture, when the step was asked again without it

    @property
    def clicking(self) -> bool:
        return self.kind.choice in ("click_item", "open_item", "add_item") and self.item is not None

    @property
    def opening(self) -> bool:
        return self.kind.choice == "open_item" and self.item is not None

    @property
    def adding(self) -> bool:
        return self.kind.choice == "add_item" and self.item is not None

    @property
    def pressing_offscreen(self) -> bool:
        return self.kind.choice == "press_offscreen" and self.offscreen is not None

    @property
    def shortcutting(self) -> bool:
        return self.kind.choice == "shortcut" and self.key is not None

    @property
    def chosen(self) -> str:
        if self.clicking:
            return self.item.choice
        if self.pressing_offscreen:
            return f"{OFFSCREEN_PREFIX}{self.offscreen.choice}"
        return self.kind.choice

    @property
    def confidence(self) -> float:
        # Only the answers that name a target lower the confidence: a click or a press lands
        # somewhere, and the wrong somewhere is not undone. use_browser reads the site answer too,
        # but every outcome of it is a page the next step can leave, so a split there must not
        # stop the run.
        if self.clicking:
            return min(self.kind.confidence, self.item.confidence)
        if self.pressing_offscreen:
            return min(self.kind.confidence, self.offscreen.confidence)
        if self.shortcutting:
            return min(self.kind.confidence, self.key.confidence)
        return self.kind.confidence

    @property
    def stops(self) -> bool:
        return self.kind.choice in STOP_KINDS


def decide(
    client: TypeSafeClient,
    goal: str,
    screen: Screen,
    items: list[Item],
    history: list[str],
    browser: str,
    email: str | None,
    tried: list[str] | None = None,
    guidance: Guidance | None = None,
    picture: Marked | None = None,
    candidates: list[Item] | None = None,
    subgoal: dict | None = None,
    banned_offscreen: set[str] | None = None,
) -> Decision:
    """One step's decision. With a `picture`, the state refers to it as `screen_image` and the
    request carries it as an asset; when System One refuses the picture, the step is asked once
    more without it, and the decision says why (see `refuses_the_picture`)."""
    on_desktop = screen.url is None and screen.app != browser
    offered = offscreen_criteria(screen.offscreen, banned_offscreen)
    questions = {
        "kind": Choice(
            instructions=(
                "You are driving this computer one action at a time. Which kind of action "
                "makes the most progress toward the goal right now? Do not repeat an action "
                "that was just taken unless the screen changed, and never one listed as already "
                "tried on this screen: each of those led straight back here."
                + (FOCUS_RULE if guidance and guidance.focus else "")
                + (SUBGOAL_RULE if subgoal else "")
            ),
            criteria=kind_criteria(
                browser,
                email,
                bool(offered),
                bool(screen.field and screen.field.is_text),
                desktop=on_desktop,
            ),
        ),
        "site": Choice(
            instructions=(
                "If the browser is used this step, which website should it show? Name a site from the "
                "list when the goal calls for that one, 'other' when the goal calls for a site the list "
                "does not name, and 'none' to stay on the page that is already open in the browser."
            ),
            criteria=site_criteria(),
        ),
    }
    shown = items if candidates is None else candidates
    if shown:
        questions["item"] = item_question(screen, items, pictured=picture is not None, only=shown)
    if offered:
        questions["offscreen"] = Choice(
            instructions=(
                "If activating a control that is not on screen is the right move, which control? "
                "These are real controls of the app, reachable without the mouse, but nothing on "
                "the capture points at them."
            ),
            criteria=offered,
        )
    if on_desktop:
        questions["key"] = Choice(
            instructions="If this step presses a command shortcut, which key?",
            criteria=key_criteria(),
        )
    state = base_state(goal, screen, items, history, tried, guidance, subgoal, banned_offscreen)
    refused = None
    if picture is None:
        answers = client.system_one(state=state, questions=questions).answers
    else:
        ref = picture.ref
        try:
            answers = client.system_one(
                state={**state, SCREEN_IMAGE: ref},
                questions=questions,
                extra_headers={IMAGES_HEADER: "true"},
                extra_body={"assets": {ref: image_asset(picture)}},
            ).answers
        except TypeSafeAPIError as e:
            if not refuses_the_picture(e):
                raise
            refused = str(e)
            log.warning("System One refused the screen picture, asking again without it: %s", refused)
            if shown:
                questions["item"] = item_question(screen, items, pictured=False, only=shown)
            answers = client.system_one(state=state, questions=questions).answers
    return Decision(
        kind=answers["kind"],
        item=answers.get("item"),
        site=answers["site"],
        offscreen=answers.get("offscreen"),
        key=answers.get("key"),
        image_refused=refused,
    )


def item_question(screen: Screen, items: list[Item], pictured: bool, only: list[Item] | None = None) -> Choice:
    return Choice(
        instructions=(
            ("Each item's number is drawn beside its box in the screen_image. " if pictured else "")
            + "If clicking an on-screen item is the right move, which item? Items marked with a "
            "role come from the app's accessibility tree and are real controls; items marked "
            "'icon' are icons without text, named by a short English description of how they "
            "look; plain items are text read from the screen. A criterion that says selected is in "
            "the app's selection; a plain click replaces that selection. Never pick an item that an action "
            "listed as already tried on this screen clicked or pressed: each of those led "
            "straight back here."
        ),
        criteria=item_criteria(screen, items, only),
    )


def merged_margin(answer, items: list[Item]) -> float:
    """第一名减第二名，先把同一个控件被拆开的概率加回去。

    框的交并比至少 0.5，或者标签相同且垂直中心相差不到半个行高，算同一个控件。
    """
    probabilities = getattr(answer, "probabilities", None) or {}
    groups = _same_targets(items)
    totals = sorted((sum(float(probabilities.get(str(index), 0.0)) for index in group) for group in groups), reverse=True)
    if not totals:
        return 0.0
    second = totals[1] if len(totals) > 1 else 0.0
    return totals[0] - second


def _same_targets(items: list[Item]) -> list[list[int]]:
    """把点的是同一个控件的条目并成一组。分差按组算，不按被拆开的编号算。"""
    parent = {it.index: it.index for it in items}

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for i, left in enumerate(items):
        for right in items[i + 1 :]:
            if _one_target(left, right):
                parent[find(right.index)] = find(left.index)
    groups: dict[int, list[int]] = {}
    for it in items:
        groups.setdefault(find(it.index), []).append(it.index)
    return list(groups.values())


def _one_target(left: Item, right: Item) -> bool:
    """框的交并比至少 0.5，或同一行上的同名文字，算同一个控件。行标题和真开关常是这一种。"""
    if _iou(left, right) >= 0.5:
        return True
    label = "".join(left.text.split()).casefold()
    if not label or label != "".join(right.text.split()).casefold():
        return False
    half = max(left.y2 - left.y1, right.y2 - right.y1) / 2
    return abs(left.center[1] - right.center[1]) < half


def _iou(left: Item, right: Item) -> float:
    x1, y1 = max(left.x1, right.x1), max(left.y1, right.y1)
    x2, y2 = min(left.x2, right.x2), min(left.y2, right.y2)
    overlap = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    if overlap <= 0:
        return 0.0
    area = (left.x2 - left.x1) * (left.y2 - left.y1) + (right.x2 - right.x1) * (right.y2 - right.y1) - overlap
    return overlap / area if area else 0.0


def image_asset(picture: Marked) -> dict:
    return {"type": "image", "format": "png", "data_base64": base64.b64encode(picture.png).decode("ascii")}


def refuses_the_picture(error: TypeSafeAPIError) -> bool:
    """Whether System One turned the request down for its picture: a 413, or a 400 whose message
    names images. Anything else, an expired key or a malformed question, is no reason to drop it."""
    if error.status == 413:
        return True
    return error.status == 400 and any(word in str(error).lower() for word in IMAGE_WORDS)


def verify_typed(client: TypeSafeClient, goal: str, field_before: Field, typed: str, field_after: Field | None) -> float:
    """Probability that the field now holds a sensible value for its purpose."""
    state = {
        "goal": goal,
        "field": field_before.summary(),
        "text_typed": typed,
        "field_value_now": field_after.value[:300] if field_after else None,
        "field_still_focused": bool(
            field_after and field_after.role == field_before.role and field_after.label == field_before.label
        ),
    }
    question = Noul(
        instructions=(
            "Did the typing succeed: does the field now contain the typed text, and is that "
            "text a sensible value for what this field asks for, given the goal?"
        )
    )
    return client.system_one(state=state, questions={"ok": question}).answers["ok"].noul
