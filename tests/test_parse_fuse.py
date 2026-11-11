import pytest

from typesafe_computer_use.parse.fuse import Icon, distinct_icons, fuse, inside, overlap


def line(text, x1, y1, x2, y2, conf=0.9):
    return (text, conf, (float(x1), float(y1), float(x2), float(y2)))


def icon(x1, y1, x2, y2, score=0.5):
    return ((float(x1), float(y1), float(x2), float(y2)), score)


def test_text_inside_a_button_becomes_the_button():
    texts, icons = fuse([line("保存", 110, 105, 150, 125, conf=0.8)], [icon(100, 100, 160, 130)])
    assert texts == []
    assert icons == [Icon((100.0, 100.0, 160.0, 130.0), 0.5, "保存", 0.8)]


def test_an_icon_inside_a_text_line_goes():
    texts, icons = fuse([line("Open the settings page", 0, 0, 300, 30)], [icon(100, 5, 120, 25)])
    assert [t for t, _, _ in texts] == ["Open the settings page"]
    assert icons == []


def test_of_two_nested_icons_the_smaller_stays():
    assert distinct_icons([icon(0, 0, 100, 100, 0.9), icon(10, 10, 40, 40, 0.3)]) == [icon(10, 10, 40, 40, 0.3)]


def test_icons_side_by_side_both_stay():
    assert len(distinct_icons([icon(0, 0, 30, 30), icon(40, 0, 70, 30)])) == 2


def test_an_icon_beside_a_paragraph_stays_because_fusion_runs_on_lines():
    lines = [line("first line of the paragraph", 40, 0, 400, 20), line("second line", 40, 24, 200, 44)]
    texts, icons = fuse(lines, [icon(5, 2, 35, 32)])
    assert len(texts) == 2
    assert icons == [Icon((5.0, 2.0, 35.0, 32.0), 0.5)]


def test_an_icon_without_text_is_left_to_caption():
    _, icons = fuse([], [icon(0, 0, 32, 32)])
    assert icons[0].text is None and icons[0].text_confidence is None


def test_lines_inside_one_icon_join_in_order_with_the_least_confidence():
    lines = [line("Sign", 10, 10, 40, 30, 0.9), line("in", 45, 10, 60, 30, 0.6)]
    _, (fused,) = fuse(lines, [icon(0, 0, 70, 40)])
    assert (fused.text, fused.text_confidence) == ("Sign in", 0.6)


def test_a_line_joins_only_the_first_icon_that_holds_it():
    # Two overlapping boxes that both hold the word, but neither is nested enough to be dropped.
    lines = [line("OK", 20, 10, 40, 30)]
    _, icons = fuse(lines, [icon(0, 0, 50, 40), icon(15, 5, 80, 45)])
    assert [i.text for i in icons] == ["OK", None]


def test_a_zero_area_icon_is_ignored():
    assert distinct_icons([icon(10, 10, 10, 40)]) == []


@pytest.mark.parametrize(
    "a, b, expected",
    [
        ((0, 0, 10, 10), (0, 0, 10, 10), 1.0),
        ((0, 0, 10, 10), (2, 2, 4, 4), 1.0),  # nested: all of the small box is shared
        ((0, 0, 10, 10), (20, 20, 30, 30), 0.0),
    ],
)
def test_overlap_is_the_most_of_iou_and_either_share(a, b, expected):
    assert overlap(a, b) == pytest.approx(expected)


def test_inside_needs_more_than_the_share():
    assert inside((0, 0, 10, 10), (0, 0, 9, 10))  # 90%
    assert not inside((0, 0, 10, 10), (0, 0, 8, 10))  # exactly 80%
