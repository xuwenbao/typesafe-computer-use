"""The classifier's request on a captured step: what it sent, and what it sends now.

TypeSafe bills every input token and caches none, so the request's size is the step's cost. The
fixture is Chrome's settings page as one step's payload file recorded it: the state and the item
criteria that went out, and every item with its box. The items make up most of both, and the
rest of the request, the other questions and their instructions, is the same for every step.
The merge that built those items is gone with the capture, so the test renders them as a step
does now, and measures what the rendering alone saves.
"""

import json
from pathlib import Path

from PIL import Image

from typesafe_computer_use.decide import base_state, item_criteria
from typesafe_computer_use.models import Item, Screen

FIXTURE = Path(__file__).parent / "fixtures" / "chrome-settings-step.json"


def wire(value) -> int:
    """Bytes on the wire, encoded as the SDK encodes a request body: compact, UTF-8."""
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def test_a_captured_step_sends_less_and_loses_no_item():
    captured = json.loads(FIXTURE.read_text(encoding="utf-8"))
    sent = captured["state"]
    screen = Screen(
        image=Image.new("RGB", captured["size"]),
        scale=1.0,
        app=sent["frontmost_app"],
        field=None,
        url=sent["browser_active_tab_url"],
    )
    items = [Item(i, *row) for i, row in enumerate(captured["items"])]
    kept = items

    state = base_state(sent["goal"], screen, kept, sent["previous_actions"], sent["already_tried_on_this_screen"])
    state["now"] = sent["now"]  # the clock the step ran at
    state.update({key: sent[key] for key in ("current_focus", "focused_field")})
    criteria = item_criteria(screen, kept)
    before, after = wire(sent) + wire(captured["item_criteria"]), wire(state) + wire(criteria)
    assert after < 0.97 * before, f"{before} -> {after} bytes"

    # What went: the clock's date on the tabs and buttons under it. Every item stayed.
    assert any("dated" in text for text in captured["item_criteria"].values())
    assert not any("dated" in text for text in criteria.values())
    assert len(criteria) == len(items)
