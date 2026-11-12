from dataclasses import replace
from pathlib import Path

import pytest
from conftest import busy_page
from PIL import Image, ImageDraw

from typesafe_computer_use import perception
from typesafe_computer_use.models import AxNode, Field, Item, Popup, Screen
from typesafe_computer_use.perception import (
    drawn,
    goal_echoes,
    is_echo,
    merge_blocks,
    merge_sources,
    order_items,
    to_items,
)
from typesafe_computer_use.platform_adapter import desktop


def line(text, x1, y1, x2, y2, conf=1.0):
    return (text, conf, (float(x1), float(y1), float(x2), float(y2)))


def test_merges_stacked_lines_across_columns():
    lines = [
        line("Kash Patel defends", 1080, 1531, 1300, 1561),
        line("Two House Democrats", 1400, 1531, 1600, 1561),
        line("removing bestiality as", 1075, 1571, 1300, 1601),
        line("defect again on key vote", 1400, 1571, 1600, 1601),
        line("FBI applicants", 1080, 1606, 1300, 1636),
    ]
    texts = sorted(t for t, _, _ in merge_blocks(lines))
    assert texts == ["Kash Patel defends removing bestiality as FBI applicants", "Two House Democrats defect again on key vote"]


def test_does_not_merge_far_or_misaligned_lines():
    lines = [line("Home", 100, 100, 200, 130), line("World", 400, 100, 500, 130), line("Footer", 100, 900, 200, 930)]
    assert len(merge_blocks(lines)) == 3


def test_merged_block_keeps_min_confidence_and_union_box():
    lines = [line("a", 100, 100, 200, 130, conf=1.0), line("b", 102, 140, 260, 170, conf=0.5)]
    ((text, conf, box),) = merge_blocks(lines)
    assert text == "a b" and conf == 0.5 and box == (100, 100, 260, 170)


def test_reading_order_rows_then_columns():
    lines = [line("right", 800, 100, 900, 130), line("left", 100, 105, 200, 135), line("below", 100, 300, 200, 330)]
    assert [it.text for it in to_items(lines, 255)] == ["left", "right", "below"]


def test_budget_caps_items():
    lines = [line(str(i), 100, 100 + 40 * i, 200, 130 + 40 * i) for i in range(10)]
    assert len(to_items(lines, 3)) == 3


def test_goal_echo_matches_wrapped_command_lines():
    goal = "go to cnn and click onto something related to AI on the homepage"
    echoes = goal_echoes(goal)
    assert is_echo('clear && uv run clicker "go to cnn and click onto something', echoes)
    assert is_echo('related to AI on the homepage" --act', echoes)
    assert not is_echo("Trending: Trump and AI warnings", echoes)


def ocr_item(index, text, x1, y1, x2, y2, conf=0.9):
    return Item(index, text, conf, float(x1), float(y1), float(x2), float(y2))


def ax_item(index, text, x1, y1, x2, y2, role="button"):
    return Item(index, text, 1.0, float(x1), float(y1), float(x2), float(y2), role=role, source="ax")


def test_merge_folds_an_overlapping_control_onto_the_ocr_block_that_names_it():
    block = ocr_item(0, "Register Now", 100, 100, 300, 130)
    control = ax_item(0, "Register Now for Disrupt", 110, 102, 290, 128, role="link")
    (merged,) = merge_sources([block], [control])
    assert merged.source == "ax+ocr" and merged.role == "link"
    assert merged.text == "Register Now for Disrupt"  # the longer of the two labels
    assert (merged.x1, merged.y1, merged.x2, merged.y2) == (100.0, 100.0, 300.0, 130.0)  # the OCR box


def test_merge_matches_on_shared_words_not_only_containment():
    block = ocr_item(0, "Buy tickets now", 100, 100, 300, 130)
    control = ax_item(0, "Buy tickets", 100, 100, 300, 130)
    assert [it.source for it in merge_sources([block], [control])] == ["ax+ocr"]


def test_merge_keeps_both_when_the_boxes_overlap_but_the_text_does_not_agree():
    block = ocr_item(0, "Search the docs", 100, 100, 300, 130)
    control = ax_item(0, "Clear input", 100, 100, 300, 130)
    assert sorted(it.source for it in merge_sources([block], [control])) == ["ax", "ocr"]


def test_merge_keeps_both_when_the_text_agrees_but_the_boxes_are_apart():
    block = ocr_item(0, "Share", 100, 100, 200, 130)
    control = ax_item(0, "Share", 900, 600, 960, 630)
    assert sorted(it.source for it in merge_sources([block], [control])) == ["ax", "ocr"]


def test_merge_consumes_each_ocr_block_at_most_once():
    block = ocr_item(0, "Send", 100, 100, 200, 130)
    controls = [ax_item(0, "Send", 100, 100, 200, 130), ax_item(1, "Send", 104, 104, 196, 126)]
    merged = merge_sources([block], controls)
    assert sorted(it.source for it in merged) == ["ax", "ax+ocr"]


def icon_item(index, text, x1, y1, x2, y2, score=0.6, source="icon"):
    role = "icon" if source == "icon" else ""
    return Item(index, text, score, float(x1), float(y1), float(x2), float(y2), role=role, source=source)


def test_a_control_on_an_icon_becomes_one_item_named_by_the_control():
    found = [icon_item(0, "gear, settings", 1880, 30, 1916, 60), icon_item(1, "\u00d7", 1290, 446, 1320, 476, source="icon+ocr")]
    controls = [ax_item(0, "设置", 1882, 27, 1920, 62), ax_item(1, "Close", 1291, 446, 1323, 479)]
    merged = merge_sources(found, controls)
    assert sorted((it.text, it.source, it.role) for it in merged) == [
        ("Close", "ax+icon", "button"),
        ("设置", "ax+icon", "button"),
    ]
    assert (merged[0].x1, merged[0].y1) in {(1880.0, 30.0), (1290.0, 446.0)}, "the icon's box, where the pixels are"


def test_a_menu_row_over_another_control_keeps_the_words_read_on_it():
    """Explorer's New menu is a popup the tree does not list: its Folder row sits over the
    navigation pane's WPS cloud entry, which must not lend the row its name or its press."""
    found = [icon_item(0, "文件夹", 317, 232, 532, 263, score=0.99, source="icon+ocr")]
    controls = [ax_item(0, "WPS云盘", 320, 236, 520, 260, role="cell")]
    assert sorted((it.text, it.source) for it in merge_sources(found, controls)) == [("WPS云盘", "ax"), ("文件夹", "icon+ocr")]


def test_an_icon_whose_words_match_the_control_still_pairs_with_it():
    found = [icon_item(0, "新建", 313, 184, 403, 219, source="icon+ocr")]
    controls = [ax_item(0, "新建", 312, 184, 403, 218)]
    assert [(it.text, it.source) for it in merge_sources(found, controls)] == [("新建", "ax+icon")]


def test_a_control_without_a_label_takes_its_icons_caption():
    found = [icon_item(0, "back arrow", 10, 10, 40, 40)]
    controls = [ax_item(0, "", 8, 8, 42, 42)]
    assert [(it.text, it.source) for it in merge_sources(found, controls)] == [("back arrow", "ax+icon")]


def test_a_control_without_a_label_or_an_icon_names_nothing_and_goes():
    controls = [ax_item(0, "", 8, 8, 42, 42), ax_item(1, "Send", 100, 8, 160, 42)]
    assert [it.text for it in merge_sources([], controls)] == ["Send"]


def test_a_control_on_its_named_text_pairs_with_the_text_before_any_icon():
    found = [ocr_item(0, "Save", 105, 12, 150, 30), icon_item(1, "floppy disk", 100, 10, 120, 32)]
    controls = [ax_item(0, "Save", 100, 8, 155, 34)]
    assert sorted((it.text, it.source) for it in merge_sources(found, controls)) == [("Save", "ax+ocr"), ("floppy disk", "icon")]


def test_an_icon_far_from_every_control_stays_an_icon():
    found = [icon_item(0, "magnifier", 500, 500, 530, 530)]
    controls = [ax_item(0, "Send", 100, 8, 160, 42)]
    assert sorted((it.text, it.source) for it in merge_sources(found, controls)) == [("Send", "ax"), ("magnifier", "icon")]


def test_budget_drops_faint_text_then_unsure_icons_and_never_a_control():
    found = [ocr_item(i, f"text {i}", 100, 100 + 40 * i, 200, 130 + 40 * i, conf=0.5 + 0.1 * i) for i in range(3)]
    found += [icon_item(3 + i, f"icon {i}", 400, 100 + 40 * i, 430, 130 + 40 * i, score=0.1 + 0.1 * i) for i in range(2)]
    controls = [ax_item(0, f"control {i}", 800, 100 + 40 * i, 900, 130 + 40 * i) for i in range(2)]
    kept = {it.text for it in merge_sources(found, controls, budget=4)}
    assert kept == {"control 0", "control 1", "icon 1", "icon 0"}
    kept = {it.text for it in merge_sources(found, controls, budget=5)}
    assert kept == {"control 0", "control 1", "icon 1", "icon 0", "text 2"}


def test_merge_keeps_a_symbol_on_a_tab_or_a_row_and_any_read_with_a_letter_or_digit():
    blocks = [
        ocr_item(0, "\u00d7", 300, 40, 310, 50),  # a tab's close box: a target of its own
        ocr_item(1, "+", 100, 240, 110, 250),  # on a row, which holds more than one target
        ocr_item(2, "C", 164, 84, 170, 96),  # a letter is text, even when it is an icon misread
        ocr_item(3, "25", 1300, 500, 1320, 520),  # the value a field shows
        ocr_item(4, "☆", 900, 900, 910, 910),  # on no control at all
    ]
    controls = [
        ax_item(0, "Settings", 130, 30, 320, 60, role="tab"),
        ax_item(1, "Downloads", 90, 230, 1000, 260, role="cell"),
        ax_item(2, "Reload", 147, 73, 181, 107),
        ax_item(3, "Show per page", 1290, 490, 1400, 530, role="field"),
    ]
    assert len(merge_sources(blocks, controls)) == len(blocks) + len(controls)


def test_merge_numbers_everything_in_reading_order():
    blocks = [ocr_item(0, "below", 100, 300, 200, 330), ocr_item(1, "right", 800, 100, 900, 130)]
    controls = [ax_item(0, "left", 100, 105, 200, 135)]
    assert [(it.index, it.text) for it in merge_sources(blocks, controls)] == [(0, "left"), (1, "right"), (2, "below")]


def test_budget_drops_the_faintest_ocr_blocks_before_any_control():
    blocks = [ocr_item(i, f"text {i}", 100, 100 + 40 * i, 200, 130 + 40 * i, conf=0.3 + 0.1 * i) for i in range(3)]
    controls = [ax_item(0, "Send", 800, 100, 900, 130)]
    kept = merge_sources(blocks, controls, budget=2)
    assert sorted(it.text for it in kept) == ["Send", "text 2"]


def test_budget_falls_back_to_dropping_controls_when_only_controls_remain():
    controls = [ax_item(i, f"control {i}", 100, 100 + 40 * i, 200, 130 + 40 * i) for i in range(4)]
    assert len(merge_sources([], controls, budget=2)) == 2


def test_order_items_renumbers_rows_then_columns():
    items = [ocr_item(7, "right", 800, 100, 900, 130), ocr_item(2, "left", 100, 105, 200, 135)]
    assert [(it.index, it.text) for it in order_items(items)] == [(0, "left"), (1, "right")]


# ----- a control the capture does not show ---------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures"


def control(x: float, y: float, w: float, h: float, label: str = "Agency") -> AxNode:
    return AxNode(role="AXComboBox", label=label, x=x, y=y, w=w, h=h, pressable=True)


def page(size=(300, 120)) -> Image.Image:
    return Image.new("RGB", size, (232, 236, 244))


def test_a_flat_box_is_not_drawn():
    assert not drawn(page(), control(20, 20, 200, 30), 1.0)


def test_an_empty_field_is_drawn_by_its_border():
    image = page()
    ImageDraw.Draw(image).rectangle((20, 20, 220, 50), outline=(40, 40, 40))
    assert drawn(image, control(20, 20, 201, 31), 1.0)


def test_a_label_inside_is_drawn():
    image = page()
    ImageDraw.Draw(image).rectangle((60, 30, 90, 40), fill=(30, 30, 30))
    assert drawn(image, control(20, 20, 200, 30), 1.0)


def test_a_neighbours_border_over_the_top_edge_does_not_count():
    """Chosen's clipped search box starts on the bottom border of the control above it."""
    image = page()
    ImageDraw.Draw(image).rectangle((10, 0, 250, 21), outline=(40, 40, 40))
    assert not drawn(image, control(20, 20, 200, 30), 1.0)


def test_the_scale_maps_points_to_capture_pixels():
    image = page((600, 240))
    ImageDraw.Draw(image).rectangle((120, 60, 180, 80), fill=(30, 30, 30))
    assert drawn(image, control(20, 20, 200, 30), 2.0)
    assert not drawn(image, control(20, 80, 200, 30), 2.0)


def test_chosens_closed_dropdown_shows_its_trigger_and_hides_its_search_box():
    """A crop of justice.gov's Forms page, at (780, 560) on the screen, with the frames
    Chrome reported for the Agency filter: Chosen's trigger, and the search box of its closed panel."""
    image = Image.open(FIXTURES / "chosen-dropdown-closed.png").convert("RGB")
    assert drawn(image, control(837 - 780, 668 - 560, 405, 32, "-Any-"), 1.0)
    assert not drawn(image, control(842 - 780, 697 - 560, 435, 31), 1.0)


def test_perceive_offers_a_hidden_control_off_the_list(monkeypatch):
    image = page((400, 200))
    ImageDraw.Draw(image).rectangle((20, 20, 120, 50), outline=(40, 40, 40))
    shown, hidden = control(20, 20, 101, 31, "Title"), control(20, 120, 200, 30)
    monkeypatch.setattr(desktop, "actionable_elements", lambda pid, w, h: ([shown, hidden], [], False))
    monkeypatch.setattr(perception, "ocr", lambda *args: [])
    screen = Screen(image=image, scale=1.0, app="Google Chrome", field=None, url=None, pid=7)
    items = perception.perceive(screen, 255, "goal")
    assert [it.text for it in items] == ["Title"]
    assert screen.offscreen == [hidden]


def test_perceive_says_which_items_are_under_a_popup_by_their_final_index(monkeypatch):
    bubble = Popup("Restore pages?", 200.0, 0.0, 200.0, 100.0)
    shown = control(20, 20, 101, 31, "Title")
    under = replace(control(250, 20, 101, 31, "Organise"), covered_by=bubble)
    monkeypatch.setattr(desktop, "actionable_elements", lambda pid, w, h: ([under, shown], [], False))
    monkeypatch.setattr(perception, "ocr", lambda *args: [])
    screen = Screen(image=busy_page((400, 200)), scale=1.0, app="Google Chrome", field=None, url=None, pid=7)
    items = perception.perceive(screen, 255, "goal")
    assert [it.text for it in items] == ["Title", "Organise"]  # renumbered in reading order
    assert screen.covered == {1: bubble}


# ----- the focused field's own text ----------------------------------------------------------

# chrome/030eeff7 step 4, just after typing 'Do Not Track' into Chrome's settings search, with the
# boxes and frames that step recorded. OCR read the box's text, and the magnifier beside it, as one
# block, and the item question put 0.66 on it.
SEARCH = Field(role="AXTextField", label="Search settings", placeholder="", value="Do Not Track", x=683, y=167, w=624, h=24)


def settings_search(monkeypatch, field: Field | None) -> list[str]:
    lines = [
        line("Settings", 138, 170, 221, 192, conf=1.0),
        line("O、 Do Not Track", 654, 163, 753, 186, conf=0.91),
        line("+", 1312, 161, 1338, 196, conf=0.85),
        line("Restore pages?", 1620, 170, 1747, 187, conf=1.0),
    ]
    icons = [((1309.0, 159.0, 1340.0, 198.0), 0.8)]  # the detector's box around the 'Clear search' button
    nodes = [
        AxNode(role="AXTextField", label="Search settings", x=683, y=167, w=624, h=24, pressable=False),
        AxNode(role="AXButton", label="Clear search", x=1307, y=165, w=28, h=28, pressable=False),
    ]
    monkeypatch.setattr(desktop, "actionable_elements", lambda pid, w, h: (nodes, [], False))
    monkeypatch.setattr(perception, "read_screen", lambda *args: perception.Reading(lines, icons, 100.0, 0, True))
    screen = Screen(image=busy_page((1920, 1080)), scale=1.0, app="Google Chrome", field=field, url=None, pid=7)
    return [it.text for it in perception.perceive(screen, 255, "turn on Do Not Track")]


def test_the_text_in_the_focused_field_is_the_field_and_not_an_item_of_its_own(monkeypatch):
    # '+' goes too: it fuses into the detected icon, which the 'Clear search' button stands on.
    assert sorted(settings_search(monkeypatch, SEARCH)) == ["Clear search", "Restore pages?", "Search settings", "Settings"]


@pytest.mark.parametrize(
    "field",
    [None, replace(SEARCH, role="AXTextArea"), replace(SEARCH, role="AXButton", label="Ads privacy sub-page back button")],
    ids=["nothing focused", "a text area", "not a text field"],
)
def test_the_same_text_stays_when_that_field_is_not_a_focused_one_line_field(monkeypatch, field):
    assert "O、 Do Not Track" in settings_search(monkeypatch, field)


# ------------------------------------------------------------------ text and icons on one capture


def toolbar_page() -> Image.Image:
    """A white page with a dark ring at (300..340, 20..60) and nothing else drawn."""
    image = Image.new("RGB", (800, 400), "white")
    ImageDraw.Draw(image).ellipse((300, 20, 340, 60), outline="black", width=4)
    return image


def page_screen(image=None, field=None) -> Screen:
    return Screen(image=image or toolbar_page(), scale=1.0, app="Finder", field=field, url=None)


def found(screen, cache=None, goal="") -> list[tuple[str, str]]:
    return sorted((it.text, it.source) for it in perception.ocr(screen, 255, goal, cache))


def test_the_text_inside_a_button_joins_the_button(fake_models):
    fake_models.lines = [line("保存", 112, 18, 140, 34)]
    fake_models.icons = [((100.0, 10.0, 160.0, 42.0), 0.8)]
    assert found(page_screen()) == [("保存", "icon+ocr")]
    assert fake_models.calls["caption"] == 0, "an icon with text is never captioned"


def test_an_icon_inside_a_line_of_text_goes(fake_models):
    fake_models.lines = [line("Downloads folder", 100, 100, 300, 120)]
    fake_models.icons = [((110.0, 102.0, 124.0, 118.0), 0.4)]
    assert found(page_screen()) == [("Downloads folder", "ocr")]


def test_an_icon_beside_a_paragraph_stays_and_the_paragraph_still_joins(fake_models):
    fake_models.lines = [line("first line of a note", 350, 20, 600, 40), line("and its second line", 350, 44, 590, 64)]
    fake_models.icons = [((300.0, 20.0, 340.0, 60.0), 0.6)]
    fake_models.captions = lambda icon: "ring"
    assert found(page_screen()) == [("first line of a note and its second line", "ocr"), ("ring", "icon")]


def test_an_icon_without_text_is_captioned_and_marked_as_an_icon(fake_models):
    fake_models.icons = [((300.0, 20.0, 340.0, 60.0), 0.6)]
    fake_models.captions = lambda icon: "gear, settings"
    (item,) = perception.ocr(page_screen(), 255, "")
    assert (item.text, item.source, item.role, item.ocr_confidence) == ("gear, settings", "icon", "icon", 0.6)


def test_a_flat_box_is_never_captioned(fake_models):
    fake_models.icons = [((500.0, 200.0, 540.0, 240.0), 0.3)]  # white on white
    assert found(page_screen()) == [] and fake_models.calls["caption"] == 0


def test_an_empty_caption_is_no_item(fake_models):
    fake_models.icons = [((300.0, 20.0, 340.0, 60.0), 0.6)]
    fake_models.captions = lambda icon: ""
    assert found(page_screen()) == []


def test_a_faint_line_takes_no_part_in_the_fusion(fake_models):
    fake_models.lines = [line("ghost", 305, 30, 335, 50, conf=0.1)]
    fake_models.icons = [((300.0, 20.0, 340.0, 60.0), 0.6)]
    fake_models.captions = lambda icon: "ring"
    assert found(page_screen()) == [("ring", "icon")]


def test_the_same_icon_on_the_next_step_is_not_captioned_again(fake_models):
    fake_models.icons = [((300.0, 20.0, 340.0, 60.0), 0.6)]
    fake_models.captions = lambda icon: "ring"
    cache = perception.ParseCache()
    first = found(page_screen(), cache)
    moved = toolbar_page()
    ImageDraw.Draw(moved).rectangle((600, 300, 700, 380), fill="black")  # a change elsewhere: the icons are detected again
    second = found(page_screen(moved), cache)
    assert first == second == [("ring", "icon")]
    assert fake_models.calls["detect"] == 2 and fake_models.calls["caption"] == 1


def test_an_unchanged_screen_is_neither_read_nor_detected_again(fake_models):
    fake_models.lines = [line("Open", 10, 10, 50, 30)]
    fake_models.icons = [((300.0, 20.0, 340.0, 60.0), 0.6)]
    cache = perception.ParseCache()
    first = perception.ocr(page_screen(), 255, "", cache)
    second = perception.ocr(page_screen(), 255, "", cache)
    assert first == second
    assert fake_models.calls == {"ocr": 1, "detect": 1, "caption": 1}


def test_the_item_criteria_name_an_icon_as_one(fake_models):
    from typesafe_computer_use.decide import item_criteria

    fake_models.icons = [((300.0, 20.0, 340.0, 60.0), 0.6)]
    fake_models.captions = lambda icon: "gear"
    screen = page_screen()
    criteria = item_criteria(screen, perception.ocr(screen, 255, ""))
    assert criteria["0"].startswith("icon 'gear' (")
