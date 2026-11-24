"""The writer model: the only place free text is generated, when the classifier asks for it and whenever the classifier stops."""

from __future__ import annotations

import base64
import io
import json
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import anthropic
import openai
from PIL import Image

from .config import OPENAI_HOST, settings
from .dates import now_context
from .models import Guidance, Item, Screen
from .openai_writer import OpenAIWriter

ANSWER_IMAGE_EDGE = 1568  # the longest edge a vision model reads without shrinking the image itself
PLACEHOLDER_KEY = "not-needed"  # an endpoint you host yourself does not check a key


type Writer = anthropic.Anthropic | OpenAIWriter  # both answer `messages.create` the Anthropic way


class WriterError(Exception):
    """The writer could not be reached, or answered with nothing usable. The step it served is refused."""


def openai_credentials() -> tuple[str, str, float] | None:
    """The OpenAI-compatible endpoint the writer and the driver share, or None when there is none.

    A key only ever goes to the endpoint it belongs to. The default endpoint is OpenAI's own, and its
    key is OPENAI_API_KEY, or CLICKER_WRITER_API_KEY when that is set. Another base URL is paired with
    CLICKER_WRITER_API_KEY only, or with a placeholder when that is unset. Returns base URL, key, and
    the timeout in seconds.
    """
    cfg = settings()
    if cfg.writer_api != "openai" or not cfg.writer_base_url:
        return None
    official = urlparse(cfg.writer_base_url).hostname == OPENAI_HOST
    key = cfg.writer_api_key or (cfg.openai_api_key if official else None)
    if key is None:
        if official:
            return None
        key = PLACEHOLDER_KEY
    return cfg.writer_base_url, key, cfg.writer_timeout


def make_writer() -> Writer | None:
    """A client, or None when there is nothing to write with.

    On the Anthropic API with no base URL of its own, the SDK reads the ANTHROPIC_* key, token, and
    base URL as it always does. Every client gives up after `writer_timeout` seconds and retries once.
    The OpenAI endpoint and its key come from `openai_credentials`.
    """
    cfg = settings()
    if cfg.writer_api == "openai":
        chosen = openai_credentials()
        if chosen is None:
            return None
        base_url, key, timeout = chosen
        return OpenAIWriter(base_url, key, timeout)
    base_url = cfg.writer_base_url
    timeout = cfg.writer_timeout
    key = cfg.writer_api_key or PLACEHOLDER_KEY
    if base_url:
        # Proxies differ in which header they read, so the key goes in both. Passing any key at all
        # also keeps the SDK from adding ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN on its own.
        return anthropic.Anthropic(base_url=base_url, api_key=key, auth_token=key, timeout=timeout, max_retries=1)
    client = anthropic.Anthropic(timeout=timeout, max_retries=1)
    if client.api_key or client.auth_token:
        return client
    return None


def provider(writer: Writer) -> str:
    """Where the writer sends its requests, for logging. Credentials and query in the URL are left out."""
    url = writer.base_url
    port = f":{url.port}" if url.port else ""
    where = f"{url.scheme}://{url.host}{port}{url.path.rstrip('/')}"
    cfg = settings()
    return f"{where} ({cfg.writer_api} API)  models: {cfg.writer_model} (writing), {cfg.answer_model} (answering)"


def _structured(
    writer: Writer,
    system: str,
    packet: dict,
    properties: dict,
    max_tokens: int,
    answering: bool = False,
    image: Image.Image | None = None,
    model: str | None = None,
    defaults: dict | None = None,
) -> dict:
    """One JSON reply from the writer's model, or from the answer model when `answering`, or from `model`
    when one is named. An answering call asks for the answer model's reasoning effort either way.

    `defaults` fills a field the reply left out. A flag still has to be present unless it is named
    here: one missing decides the next step on a guess."""
    cfg = settings()
    schema = {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}
    content: list[dict] = [{"type": "text", "text": json.dumps(packet)}]
    if image is not None:
        content.insert(0, _image_block(image))
    extra: dict = {}
    if cfg.custom_writer_endpoint:
        # Anthropic enforces output_config. Another endpoint may ignore it without a word, so the
        # schema is spelled out in the prompt as well.
        system = f"{system}\n\nAnswer with a single JSON object and nothing else, matching this schema:\n{json.dumps(schema)}"
        # Another endpoint may think by default, out of the same max_tokens: a 200-token call then
        # comes back with no text at all. Anthropic thinks only when asked.
        extra["thinking"] = {"type": "disabled"}
    if cfg.writer_api == "openai":
        # An OpenAI model reasons by default, out of max_tokens too; the effort is its own setting.
        effort = cfg.answer_reasoning if answering else cfg.writer_reasoning
        if effort:
            extra["reasoning"] = effort
    try:
        response = writer.messages.create(
            model=model or (cfg.answer_model if answering else cfg.writer_model),
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
            **extra,
        )
    except (anthropic.APIError, openai.APIError) as e:
        raise WriterError(f"the request failed: {e}") from e
    return checked(parse_json("".join(b.text for b in response.content if b.type == "text")), properties, defaults)


def parse_json(text: str) -> dict:
    """The first JSON object in a reply, as long as one is in there.

    A model that was asked for JSON usually returns exactly that. Some wrap it in code fences or a
    sentence, so the object is looked for rather than assumed to fill the whole reply. A reply cut
    off before its object closes holds none, and nothing is guessed out of it.
    """
    decoder = json.JSONDecoder()
    for start in (i for i, ch in enumerate(text) if ch == "{"):
        try:
            data, _ = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    raise WriterError(f"the writer answered without usable JSON: {text[:400]!r}")


def checked(data: dict, properties: dict, defaults: dict | None = None) -> dict:
    """The reply with each field of its schema's type. Anthropic enforces the schema itself; another
    endpoint may leave a field out or send a string where a flag belongs. A flag decides what happens
    next, so one missing is an error unless `defaults` names it. A missing string is empty, which
    every caller reads as nothing to type, open, or say."""
    out = dict(data)
    defaults = defaults or {}
    for name, spec in properties.items():
        if name not in out and name in defaults:
            out[name] = defaults[name]
        elif spec["type"] == "string" and name not in out:
            out[name] = ""
        kind = {"boolean": bool, "string": str, "array": list, "number": (int, float), "object": dict}[spec["type"]]
        if not isinstance(out.get(name), kind):
            raise WriterError(f"the writer's reply has no {spec['type']} {name!r}: {json.dumps(data)[:400]}")
    return out


def _image_block(image: Image.Image) -> dict:
    """The capture as a PNG the model can read. PNG because screen text does not survive JPEG well."""
    shrunk = image.convert("RGB")
    shrunk.thumbnail((ANSWER_IMAGE_EDGE, ANSWER_IMAGE_EDGE))
    buffer = io.BytesIO()
    shrunk.save(buffer, format="PNG")
    data = base64.b64encode(buffer.getvalue()).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}


@dataclass(frozen=True)
class Fill:
    """What goes into the focused field: the exact text, empty when the writer declined, and whether
    Return follows it."""

    text: str
    submit: bool = False


def compose_text(
    writer: Writer,
    goal: str,
    screen: Screen,
    items: list[Item],
    history: list[str],
    guidance: Guidance | None = None,
) -> Fill:
    """The exact string to type into the focused field, and whether to submit it with Return.

    Return never follows text in a text area, where it starts a new line instead."""
    packet = {
        "goal": goal,
        **(guidance.state() if guidance else {}),
        "now": now_context(),
        "frontmost_app": screen.app,
        "previous_actions": history[-8:],
        "focused_field": screen.field.summary() if screen.field else None,
        "text_near_field": screen.near_field(items),
        "all_screen_text": [it.text for it in items][:120],
    }
    data = _structured(
        writer,
        system=(
            "You fill in one text field on a user's screen. You receive the user's goal, recent "
            "actions, the focused field's label and placeholder, and nearby screen text, and, when "
            "there are any, the step the agent is now working on and what the user said when asked. "
            "Decide the exact string to type. Never invent credentials, passwords, or personal data; for such "
            "fields, or when the field should not be filled, set fill to false. Set submit to true when this "
            "text completes what the goal asks of the field and Return should confirm it now, as a Save or OK "
            "button would: a name or value the goal says to create, rename, change, or save, or a search it "
            "says to run. Set it to false when the form has other fields still to fill, or when the goal only "
            "needs the text entered. Give the reason before deciding submit."
        ),
        packet=packet,
        properties={  # in this order: the reason is written before submit is decided
            "fill": {"type": "boolean"},
            "text": {"type": "string"},
            "reason": {"type": "string"},
            "submit": {"type": "boolean"},
        },
        max_tokens=256,
    )
    text = data["text"].strip() if data["fill"] else ""
    multiline = screen.field is not None and screen.field.role == "AXTextArea"
    return Fill(text, submit=bool(text) and data["submit"] and not multiline)


def valid_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and "." in parsed.netloc and not any(ch.isspace() for ch in url)


# --------------------------------------------------------------------------- #
# Browser backend. Free text stays in this module, per CONTRIBUTING: the writer
# is the only place free text is generated, replies are structured, and a
# code-side guard runs before anything is typed.
# --------------------------------------------------------------------------- #

# Code-side guard. The writer is told not to fill credentials, and this is the
# second line of defence: never add a path that types a password.
CREDENTIAL_HINTS = (
    "password",
    "passwd",
    "passcode",
    "passphrase",
    "one-time",
    "one time",
    "otp",
    "2fa",
    "mfa",
    "security code",
    "pin",
    "cvv",
    "cvc",
    "card number",
    "credit card",
    "expiry",
    "social security",
    "ssn",
    "secret",
    "api key",
    "token",
    "private key",
    "recovery code",
    "seed phrase",
)


# A file name such as secret.txt is not a credential, even though the stem is one of the hints.
_FILENAME = re.compile(r"\b[\w-]+\.[a-z0-9]{1,8}\b", re.IGNORECASE)


def looks_credential(label: str) -> bool:
    """True when a hint is a whole word. A file name with an extension is not a credential."""
    lowered = _FILENAME.sub(" ", (label or "").lower())
    return any(re.search(rf"(?<!\w){re.escape(hint)}(?!\w)", lowered) for hint in CREDENTIAL_HINTS)


def compose_browser_text(
    writer: Writer,
    goal: str,
    *,
    field_label: str,
    page_title: str,
    url: str,
    nearby_text: list[str],
    history: list[str],
) -> str:
    """The exact string to type into a browser field. Empty means declined or refused."""
    if looks_credential(field_label):
        return ""
    packet = {
        "goal": goal,
        "now": now_context(),
        "page": {"title": page_title, "url": url},
        "focused_field": field_label,
        "text_near_field": nearby_text,
        "previous_actions": history[-8:],
    }
    data = _structured(
        writer,
        system=(
            "You fill in one text field on a web page for a user working toward a goal. You "
            "receive the goal, recent actions, the field's label or placeholder, and nearby page "
            "text. Decide the exact string to type. Never invent credentials, passwords, one-time "
            "codes, card numbers, or personal data; for such fields, or when the field should not "
            "be filled, set fill to false. Keep it short and literal - no explanation."
        ),
        packet=packet,
        properties={"fill": {"type": "boolean"}, "text": {"type": "string"}, "reason": {"type": "string"}},
        max_tokens=256,
    )
    text = data["text"].strip() if data["fill"] else ""
    # Guard again on the way out: a label that looked innocent can still attract
    # a credential-shaped value.
    if looks_credential(field_label) or looks_credential(text):
        return ""
    return text


def compose_url(writer: Writer, goal: str, history: list[str], guidance: Guidance | None = None) -> str:
    """The URL to open for this goal. Empty means no sensible site, or an invalid proposal."""
    data = _structured(
        writer,
        system=(
            "Given a user's goal for their web browser, give the single best https URL to open first. "
            "Prefer the site's homepage or the most direct public page. If no website is implied, set ok to false."
        ),
        packet={"goal": goal, **(guidance.state() if guidance else {}), "now": now_context(), "previous_actions": history[-8:]},
        properties={"ok": {"type": "boolean"}, "url": {"type": "string"}, "reason": {"type": "string"}},
        max_tokens=200,
    )
    url = data["url"].strip() if data["ok"] else ""
    return url if valid_url(url) else ""


@dataclass(frozen=True)
class Answer:
    """What the writer made of a stop: the words for the user, and how the run goes on when it can.

    At most one of `focus` and `question` is acted on. `route` says who moves next: the classifier,
    the driver, or nobody. A question goes to the user first, before that choice is taken.
    """

    text: str
    achieved: bool  # whether the screen itself shows the goal reached, in the writer's judgement
    focus: str = ""  # the next sub-goal for the classifier, in terms of the screen
    question: str = ""  # what only the user can say
    route: str = ""  # classifier, driver, or end
    missing: tuple[tuple[str, str], ...] = ()  # (name, absent|still_here|other) when the goal is not reached


ANSWER_SYSTEM = (
    "An agent is driving a user's computer toward the user's goal. A small classifier picks each "
    "action, and it has stopped and handed the run to you. You receive the goal, the actions taken, "
    "why the classifier stopped, a capture of the screen as it is now, the text read from that "
    "screen, and the text of the screens it passed through on the way, oldest first. You may also "
    "receive the focus the classifier was working on, the earlier times it stopped with the focus "
    "you gave each time, what the user said when asked, and selected_items, the rows the app "
    "itself reports as selected.\n\n"
    "Tell the user the result. When the goal asks for information, lead with that information, "
    "taken only from those screens and from what the user said: never from memory, and never a "
    "guess. When the goal asks for something to be done, say whether the screen shows it done. "
    "When the screen does not hold the result, say so plainly, then say what is on screen and the "
    "one next step that would get there. on_screen names what this screen lists. A name there is "
    "on this screen: a blank area of the capture cannot say it is absent. Text from an earlier "
    "screen, such as an empty folder, describes only that screen. Each earlier screen is older "
    "than the actions_since actions taken after it, and those actions may have changed the place "
    "it shows. An earlier screen cannot deny what this screen shows, and cannot prove that a place "
    "acted on later is still as it was. A file or folder is in this "
    "folder only when on_screen.file_rows lists it. A name in the sidebar, in Quick access, or in "
    "the address bar is a place to go, not a row of this folder, and does not show that it is "
    "here. When the goal asks for a new folder here and file_rows does not list it, the next step "
    "creates it. Do not open a same-named item somewhere else. A file is selected only when "
    "its name is in selected_items. The highlight in the capture is not the selection. When "
    "several files must move and that list names only some of them, the next focus adds one "
    "missing file to the selection. Do not say the others are selected, and do not make the next "
    "step a cut. "
    "Plain text, no markdown, four sentences at most. Set achieved to true only when on_screen "
    "and the goal together show the goal reached, every part of it: which item, where it goes, "
    "and what value it takes. When this screen is the place the goal puts things, and "
    "on_screen.file_rows lists them, the goal is reached. When this screen is the place the goal "
    "clears, and on_screen.file_rows still lists them, the goal is not reached. Do not set a focus "
    "only to confirm a name "
    "on_screen already lists. Return on a file row opens that file. When the goal is not reached, "
    "set missing to one entry for each part this screen does not show done. Copy name from "
    "on_screen. Set state to absent when the goal needs that name on this screen and it is not "
    "here, still_here when the goal needs it gone and it is still here, or other for any other "
    "unfinished part. When listed_names_the_previous_answer_denied is present, the previous answer "
    "said those names were absent. They are on this screen. Judge again from on_screen. There is "
    "no capture this time. When driver_said_done is present, the driver already judged the goal "
    "reached, and that text is its reason. Judge again from on_screen. If you disagree, name in "
    "missing the part this screen does not show done.\n\n"
    "When the goal is not reached, set route to classifier, driver, or end. "
    "Set route to classifier when the next step is one action the classifier can take, and set "
    "focus to that action: one short imperative sentence naming it in terms of what this screen "
    "shows, quoting the text of the item to use when there is one. The classifier can click an "
    "on-screen item, type into a focused field, replacing what the field holds, press Return or "
    "Escape, scroll, go back, wait, and open a website by its https address. On the desktop it can "
    "also double-click an item to open it, add an item to the selection, and press Control "
    "(Command on a Mac) with x, c, v, or a. It cannot read, compare, or remember, so a focus is "
    "one move, not a plan. Name that one action: do not tell it to repeat a click, or to keep "
    "clicking until something appears. When highlighted_menu_item names the item the next step "
    "needs, set focus to press Return. When a date on a calendar is months away, the one action "
    "is to click the heading that shows the month and year, which opens the year or month view. "
    "It cannot right-click, or press any other key. A focus that needs one of those is for the "
    "driver: name the button, menu, or link on screen that does the same when one is there. "
    "Set route to driver when the next step needs several actions in a row, such as opening a "
    "menu, waiting for a rename box, and typing a name; or the classifier has already gone wrong "
    "on this screen; or the next step needs a key the classifier does not have. Leave focus empty. "
    "Set route to end when the goal is reached, when the next step is one only the user should "
    "take, such as a login or a payment, or when no action would help. Leave focus empty. "
    "When missing still names a part as absent or still_here, do not set route to end: set it "
    "to driver. "
    "Never conclude from "
    "memory that a setting, feature, or page does not exist: apps move and rename them between "
    "versions. While the screen still shows a place to look, such as a search result, a row "
    "marked as matching, a menu, or a section not yet opened, give opening it as the focus. "
    "offscreen_controls lists controls on this page that are not in view. When the goal needs one "
    "of them, it is on this page: the next step is to scroll the pane that holds it, or to use "
    "the control. Do not leave the page to look for it somewhere else. "
    "Do not give again a focus from earlier_stops that changed "
    "nothing. Set question to ask the user, only when user_can_be_asked is true, and only for what "
    "the screens cannot tell you and the goal leaves open: a choice between options the user would "
    "care about, or a fact only the user has. One short question. Never ask for a password or any "
    "other credential, and never ask what user_said already answers. When the run ends short of "
    "the goal, say plainly what the agent could not do."
)


ROUTES = ("classifier", "driver", "end")


def _settled_route(raw: str, achieved: bool, focus: str, missing: tuple[tuple[str, str], ...] = ()) -> str:
    """One of classifier, driver, or end. A reached goal ends. A classifier with no focus is the driver.

    End while a named part is still absent or still here is the driver: the run has somewhere to go.
    """
    if achieved:
        return "end"
    route = raw.strip().lower()
    if route == "classifier" or route not in ("driver", "end"):
        route = "classifier" if focus else "driver"
    if route == "end" and any(state in ("absent", "still_here") for _, state in missing):
        return "driver"
    return route


OFFSCREEN_LIMIT = 40  # controls named to the writer; the rest stay on the page for a later step


def _menu_highlight(screen: Screen) -> dict:
    """The menu item Return would choose, named only while a menu item has the keyboard."""
    field = screen.field
    if field is None or field.role != "AXMenuBarItem" or not field.label:
        return {}
    return {"highlighted_menu_item": field.label}


def _on_screen(items: list[Item]) -> dict:
    """Names the current screen lists. A blank area of the capture cannot remove one."""
    names: list[str] = []
    file_rows: list[str] = []
    for it in items:
        if it.text and it.text not in names:
            names.append(it.text)
        if it.role == "cell" and "selection_item" in it.controls and it.text and it.text not in file_rows:
            file_rows.append(it.text)
    return {"names": names, "file_rows": file_rows}


_QUOTED = re.compile(r"'([^']+)'|\"([^\"]+)\"|「([^」]+)」")
_EMPTY = re.compile(r"\bempty\b|是空|为空|空的", re.IGNORECASE)
_ABSENT = re.compile(r"\b(?:not|missing|absent)\b|不在|没有", re.IGNORECASE)


def denied_listed_names(text: str, achieved: bool, items: list[Item]) -> list[str]:
    """Names `on_screen` lists that this text calls missing or paints the screen empty over."""
    if achieved:
        return []
    facts = _on_screen(items)
    found: list[str] = []
    blob = text
    if facts["file_rows"] and _EMPTY.search(blob):
        found.extend(facts["file_rows"])
    listed = set(facts["names"])
    for sentence in re.split(r"[.\n。!?]+", blob):
        if _ABSENT.search(sentence) is None:
            continue
        for match in _QUOTED.finditer(sentence):
            name = next(group for group in match.groups() if group)
            if name in listed and name not in found:
                found.append(name)
    return found


_MISSING_STATES = ("absent", "still_here", "other")


def _missing_parts(raw: object, achieved: bool) -> tuple[tuple[str, str], ...]:
    """The parts this screen does not show done. A reached goal names none of them."""
    if achieved or not isinstance(raw, list):
        return ()
    found: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        state = str(item.get("state") or "").strip()
        found.append((name, state if state in _MISSING_STATES else "other"))
    return tuple(found)


def listed_but_called_absent(answer: Answer, items: list[Item]) -> list[str]:
    """File rows `missing` calls absent. A row that should leave and is still here is not a contradiction."""
    rows = set(_on_screen(items)["file_rows"])
    found: list[str] = []
    for name, state in answer.missing:
        if state == "absent" and name in rows and name not in found:
            found.append(name)
    return found


def _offscreen_controls(screen: Screen) -> list[dict]:
    """Controls on this page that are not in view, numbered as focus_offscreen addresses them."""
    return [{"i": i, "control": f"{node.role_word} {node.label!r}"} for i, node in enumerate(screen.offscreen[:OFFSCREEN_LIMIT])]


def _selected_files(items: list[Item]) -> list[str]:
    """File-row names the tree reports as selected, in the order they appear."""
    names: list[str] = []
    for it in items:
        if it.selected and it.role == "cell" and "selection_item" in it.controls and it.text not in names:
            names.append(it.text)
    return names


DRIVE_ACTIONS = (
    "click",
    "double_click",
    "right_click",
    "type",
    "key",
    "shortcut",
    "scroll_down",
    "scroll_up",
    "drag",
    "set_value",
    "focus_offscreen",
    "wait",
    "done",
    "give_up",
)

HOST_SYSTEM = (
    "An agent is about to drive a computer toward the user's goal. Your only job is to put the "
    "application that goal needs in front. You do not click, type, scroll, or save. You receive the "
    "goal, the frontmost process, and the visible windows. Each window has an id, a title, and a "
    "process name.\n"
    "Choose one action:\n"
    "ready: the frontmost process is already the application the goal needs. Leave id and executable empty.\n"
    "focus: one of the listed windows is that application, even if it is not frontmost. Set id to its id.\n"
    "launch: it is not open. Set executable to a single program name such as notepad or WINWORD.EXE. "
    "No path, no spaces, and no arguments.\n"
    "Leave whichever of id and executable the action does not use empty. Do not use the Start menu "
    "or its search. Give the reason in one sentence."
)

HOST_PROPERTIES = {
    "action": {"type": "string"},
    "id": {"type": "string"},
    "executable": {"type": "string"},
    "reason": {"type": "string"},
}


PLAN_SYSTEM = (
    "Turn the user's goal into a checkable state and at most three subgoals for the screen in front "
    "of you. You do not click. Each subgoal names an action the classifier already has, plus the "
    "control to find and the state that shows it worked. do is one of click, open, press_offscreen, "
    "scroll, press_enter, press_escape, go_back, wait, shortcut, or ensure. shortcut also sets key "
    "to one of x, c, v, or a (cut, copy, paste, select all). ensure only checks. space.role is "
    "a short role such as button or checkbox. space.controls is a UI Automation pattern such as "
    "toggle. space.label is a case-insensitive regular expression matched against the control and "
    "the rest of its row. verify and require use checked, expanded, current, and exists. Either may "
    "also set label, read the same way as space.label, to assert a different control. "
    "goal_state.require must be a state that can be read on a control: checked, expanded, or current, "
    "or exists together with a label. A click, open, or press_offscreen subgoal must set space.label. "
    "Its verify must be something that changes because of the action: a state such as checked, exists "
    "set to false, or exists on a different control named by verify.label. Do not verify only that the "
    "control you are about to use already exists. The first subgoal's label has to match a control on "
    "the screen in front of you; a press_offscreen label matches an off-screen control. When rejected "
    "is in the packet, the previous plan was sent back for those reasons. Write a new plan that fixes "
    "every one of them."
)

PLAN_PROPERTIES = {
    "goal_state": {"type": "object"},
    "subgoals": {"type": "array"},
}


def compose_plan(
    writer: Writer,
    goal: str,
    screen,
    items: list,
    history: list[str],
    guidance,
    previous: list | None = None,
    rejected: list[str] | None = None,
    banned_offscreen: set[str] | None = None,
):
    """一份计划。读不成、动作不在名单里、正则编译不了，或子目标超过 3 个，就返回空计划。

    `rejected` 是上一份没通过检查的原因，原样放进状态包。`banned_offscreen` 是这一屏被拒过的屏外控件，
    不再交给规划器。
    """
    from .agents.planner import plan_from
    from .decide import base_state

    packet = base_state(goal, screen, items, history, None, guidance, banned_offscreen=banned_offscreen)
    # 分类器的状态包再加上每条的角色和控件状态。previous_subgoals 让下一次规划看见已经断言过的结果。
    packet["items"] = [
        {
            "i": it.index,
            "text": it.text,
            "role": it.role,
            **({"checked": it.checked} if it.checked is not None else {}),
            **({"expanded": it.expanded} if it.expanded is not None else {}),
            **({"current": it.current} if it.current is not None else {}),
        }
        for it in items
    ]
    packet["previous_subgoals"] = list(previous or [])
    if rejected:
        packet["rejected"] = list(rejected)
    try:
        data = _structured(writer, PLAN_SYSTEM, packet, PLAN_PROPERTIES, 800)
    except WriterError:
        return plan_from({})
    return plan_from(data)


def compose_host(writer: Writer, goal: str, windows: list[tuple[str, str, str]], frontmost: str) -> dict:
    """Which application to put in front before the classifier runs. One of ready, focus, or launch."""
    packet = {
        "goal": goal,
        "frontmost": frontmost,
        "windows": [{"id": ident, "title": title, "process": process} for ident, title, process in windows],
    }
    return _structured(writer, HOST_SYSTEM, packet, HOST_PROPERTIES, 300)


@dataclass(frozen=True)
class DriveStep:
    """One action the driver chose, as the model gave it. The driver checks it before anything happens."""

    reason: str
    action: str
    item: str = ""  # an item number, for click, double_click, right_click, drag, and type
    target: str = ""  # the text of that item, copied from the screen; checked before the action
    text: str = ""  # what to type, for type
    key: str = ""  # the key, for key and shortcut
    modifier: str = ""  # "", add, or range, for click
    to: str = ""  # the item a drag ends on
    dx: str = ""  # a drag's horizontal pixels, when it does not end on an item
    dy: str = ""  # a drag's vertical pixels
    achieved: bool = False  # for done


def _last_action(history: list[str]) -> dict:
    """The latest history line, and whether it was refused rather than taken."""
    did = history[-1] if history else ""
    return {"did": did, "refused": did.startswith("refused") or "refused:" in did}


def _shown(raw: object) -> str:
    """A reply fragment short enough to log beside the reason it was refused."""
    try:
        text = json.dumps(raw)
    except TypeError:
        text = repr(raw)
    return text[:400]


def _action_name(raw: dict) -> str | None:
    """The action this entry names. `type` or `name` counts only when `action` itself is absent."""
    named = raw.get("action")
    if isinstance(named, str) and named.strip():
        return named.strip().lower()
    for key in ("type", "name"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip().lower() in DRIVE_ACTIONS:
            return value.strip().lower()
    return None


# The prompt says "press run" and "press start". A reply that puts either name in `action`, with no
# key of its own, means that key.
KEY_AS_ACTION = frozenset({"run", "start"})


def _drive_step(data: dict) -> DriveStep:
    """The reply's action, or an error when it names none the driver has."""
    action = _action_name(data)
    if action is None:
        raise WriterError(f"the driver's reply needs an action name: {_shown(data)}")
    key = str(data.get("key") or "").strip().lower()
    if action in KEY_AS_ACTION and not key:
        action, key = "key", action
    if action not in DRIVE_ACTIONS:
        raise WriterError(f"{action!r} is not a driver action: {_shown(data)}")
    return DriveStep(
        reason=str(data.get("reason") or "").strip(),
        action=action,
        item=str(data.get("item") or "").strip(),
        target=str(data.get("target") or "").strip(),
        text=str(data.get("text") or ""),
        key=key,
        modifier=str(data.get("modifier") or "").strip().lower(),
        to=str(data.get("to") or "").strip(),
        dx=str(data.get("dx") or "").strip(),
        dy=str(data.get("dy") or "").strip(),
        achieved=bool(data.get("achieved", False)),
    )


def compose_answer(
    writer: Writer,
    goal: str,
    screen: Screen,
    items: list[Item],
    history: list[str],
    stopped: str,
    earlier: list[dict] | None = None,
    guidance: Guidance | None = None,
    earlier_stops: list[dict] | None = None,
    can_ask: bool = False,
    *,
    picture: bool = True,
    denied: list[str] | None = None,
    driver_said_done: str = "",
    goal_state_held: bool = False,
) -> Answer:
    """What to tell the user now that the classifier has stopped, and how the run could go on.

    The classifier can stop on the right page but cannot say what the page says. The writer reads the
    capture itself as well as its text, since OCR misreads a letter here and there and drops layout.
    `earlier` is the text of the screens before this one, for a goal whose answer was on the way.
    `earlier_stops` are the times the classifier stopped before, each with the focus it was sent
    back with, so a focus that led nowhere is not given twice.
    """
    packet = {
        "goal": goal,
        **(guidance.state() if guidance else {}),
        "now": now_context(),
        "why_the_run_stopped": stopped,
        "actions_taken": history,
        **({"earlier_stops": earlier_stops} if earlier_stops else {}),
        "user_can_be_asked": can_ask,
        "frontmost_app": screen.app,
        "browser_active_tab_url": screen.url,
        **_menu_highlight(screen),
        "offscreen_controls": _offscreen_controls(screen),
        "on_screen": _on_screen(items),
        **({"listed_names_the_previous_answer_denied": denied} if denied else {}),
        "selected_items": list(dict.fromkeys(it.text for it in items if it.selected)),
        "screen_text_in_reading_order": [
            f"{it.text} (under {screen.covered[it.index].title})" if it.index in screen.covered else it.text for it in items
        ],
        **({"earlier_screens": earlier} if earlier else {}),
        **({"driver_said_done": driver_said_done} if driver_said_done else {}),
        **({"goal_state_held": True} if goal_state_held else {}),
    }
    data = _structured(
        writer,
        system=ANSWER_SYSTEM,
        packet=packet,
        properties={
            "achieved": {"type": "boolean"},
            "answer": {"type": "string"},
            "focus": {"type": "string"},
            "question": {"type": "string"},
            "route": {"type": "string", "enum": list(ROUTES)},
            "missing": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "state": {"type": "string", "enum": list(_MISSING_STATES)},
                    },
                    "required": ["name", "state"],
                    "additionalProperties": False,
                },
            },
        },
        max_tokens=2048,
        answering=True,
        image=screen.image if picture and settings().writer_vision else None,
        defaults={"missing": []},
    )
    focus = data["focus"].strip()
    achieved = bool(data["achieved"])
    missing = _missing_parts(data.get("missing"), achieved)
    return Answer(
        text=data["answer"].strip(),
        achieved=achieved,
        focus=focus,
        question=data["question"].strip(),
        route=_settled_route(str(data.get("route", "")), achieved, focus, missing),
        missing=missing,
    )
