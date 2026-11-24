"""The planner matches a control by its pattern and label, and reads state without a model."""

from yuanji_computer_use.agents.planner import Predicate, Space, matching
from yuanji_computer_use.models import Item


def _item(
    index: int, text: str, *, role: str = "", source: str = "ax", controls: tuple[str, ...] = (), y: float = 100, **state
) -> Item:
    return Item(index, text, 1.0, 100, y, 400, y + 30, role=role, source=source, controls=controls, **state)


def test_a_row_title_and_a_switch_of_the_same_name_are_not_the_same_control():
    title = _item(0, "虚拟网卡模式", role="button", controls=("invoke",))
    switch = _item(1, "虚拟网卡模式", role="button", controls=("toggle",), y=100)
    junk = _item(2, "a loading or buffering indicator.", role="image", source="icon", y=400)
    space = Space(controls=("toggle",), label="TUN|虚拟网卡")

    found = matching(space, [title, switch, junk])

    assert found == [switch]


def test_an_icon_whose_caption_does_not_match_is_left_out():
    icon = _item(0, "a loading or buffering indicator.", role="image", source="icon", y=100)
    assert matching(Space(label="TUN|虚拟网卡"), [icon]) == []


def test_an_ocr_item_makes_a_checked_assertion_unknown():
    item = _item(0, "虚拟网卡模式", source="ocr", checked=False)
    assert Predicate(checked=False).evaluate([item]) == "unknown"


def test_a_missing_item_makes_exists_false_and_state_unknown():
    assert Predicate(exists=True).evaluate([]) == "false"
    assert Predicate(checked=False).evaluate([]) == "unknown"
