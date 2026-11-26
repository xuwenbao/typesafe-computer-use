from dataclasses import replace
from types import SimpleNamespace

from yuanji_computer_use.agents.policy.stages import MAX_REPEATS, answers, repeating, tried_here
from yuanji_computer_use.agents.session import Session
from yuanji_computer_use.agents.writer import earlier_screens
from yuanji_computer_use.llm.classifier import Decision
from yuanji_computer_use.models import LINES_PER_DIFFERENCE, Field, Item, same_screen, signature


def texts(*words: str):
    """Lines one per row, top to bottom."""
    return tuple((word, row) for row, word in enumerate(words))


def test_a_signature_names_the_app_the_page_the_focus_and_the_text_in_reading_order(screen, make_item):
    field = Field(role="AXTextField", label="Search", placeholder="", value="", x=0, y=0, w=10, h=10)
    focused = replace(screen, field=field, url="https://example.com/")
    assert signature(focused, [make_item(0, "Search"), make_item(1, "Go", y1=300, y2=340)]) == (
        "Google Chrome",
        "https://example.com/",
        "AXTextField:Search",
        (("Search", 3), ("Go", 8)),  # rows of 20 points, at scale 2: centers at 115 px and 320 px
        (),
    )
    assert signature(screen, [])[2] is None


def test_the_same_text_on_another_page_or_app_or_focus_is_another_screen():
    here = ("Google Chrome", "https://a/", None, texts("Home", "Tickets"))
    assert same_screen(here, here)
    assert not same_screen(here, ("Finder", "https://a/", None, texts("Home", "Tickets")))
    assert not same_screen(here, ("Google Chrome", "https://b/", None, texts("Home", "Tickets")))
    assert not same_screen(here, ("Google Chrome", "https://a/", "AXTextField:Search", texts("Home", "Tickets")))


def test_a_clock_or_a_ticker_does_not_make_a_new_screen():
    lines = [f"Gate {n}" for n in range(9)]
    before = ("Google Chrome", None, None, texts(*lines, "12:00"))
    after = ("Google Chrome", None, None, texts(*lines, "12:01"))
    assert same_screen(before, after)
    assert LINES_PER_DIFFERENCE == 10


def test_a_tabs_memory_figure_does_not_make_a_new_screen(screen, make_item):
    """Chrome names each tab with the memory it uses, and the figure drifts from one capture
    to the next. With the clock that was two changed lines, and a click that did nothing looked like one
    that did."""

    def capture(tab: str, clock: str):
        lines = [tab, clock, *[f"Setting {n}" for n in range(9)]]
        return signature(screen, [make_item(n, text, y1=100 + 40 * n, y2=130 + 40 * n) for n, text in enumerate(lines)])

    before = capture("Settings - Memory usage - 56.0 MB", "Sep 29 03:07")
    assert same_screen(before, capture("Settings - Memory usage - 57.3 MB", "Sep 29 03:08"))
    assert same_screen(before, capture("Settings - High memory usage - 1.2 GB", "Sep 29 03:08"))
    assert before[3][0] == ("Settings", 3)
    assert not same_screen(before, capture("Downloads - Memory usage - 56.0 MB", "Sep 29 03:08"))  # another tab is a change
    assert signature(screen, [make_item(0, "Memory usage - 56.0 MB")])[3][0][0] == "Memory usage - 56.0 MB"  # page text stays


def test_a_small_modal_on_a_dense_page_is_a_new_screen():
    lines = [f"Row {n}" for n in range(40)]
    page = ("Google Chrome", None, None, texts(*lines))
    with_modal = ("Google Chrome", None, None, texts(*lines, "Sign up for news", "Close"))
    assert not same_screen(page, with_modal)  # two lines in forty-two is a change, however small the share


def test_one_line_changing_on_a_short_page_is_a_new_screen():
    assert not same_screen(
        ("Notes", None, None, texts("Next", "Step 1 of 3")), ("Notes", None, None, texts("Next", "Step 2 of 3"))
    )
    assert not same_screen(
        ("Notes", None, None, texts(*[f"L{n}" for n in range(5)])),
        ("Notes", None, None, texts(*[f"L{n}" for n in range(4)], "L9")),
    )


def test_a_page_that_gained_a_section_is_a_new_screen():
    before = ("Google Chrome", None, None, texts("Home", "Tickets"))
    after = ("Google Chrome", None, None, texts("Home", "Tickets", "Buy", "Terms", "Dates"))
    assert not same_screen(before, after)


def test_a_dense_page_scrolled_by_a_tenth_is_a_new_screen():
    lines = [f"Row {n}" for n in range(30)]
    before = ("Google Chrome", None, None, texts(*lines))
    after = ("Google Chrome", None, None, texts(*lines[3:], "Row 30", "Row 31", "Row 32"))
    assert not same_screen(before, after)  # 27 of 30 lines are still there, but every one moved
    assert same_screen(("Google Chrome", None, None, texts()), ("Google Chrome", None, None, texts()))


def test_tried_here_lists_the_actions_taken_on_the_current_screen_oldest_first():
    home = ("Google Chrome", "https://a/", None, texts("Home", "Tickets", "Blog"))
    blog = ("Google Chrome", "https://a/blog", None, texts("Blog", "Posts"))
    state = Session(last=home, seen=[(home, "clicked 'Blog'"), (blog, "went back"), (home, "clicked 'Tickets'")])
    assert tried_here(state) == ["clicked 'Blog'", "clicked 'Tickets'"]
    assert tried_here(Session()) == []


def test_earlier_screens_are_the_distinct_ones_before_the_last_oldest_first():
    home = ("Google Chrome", "https://a/", None, texts("Home", "Tickets"))
    rows = [f"Row {n}" for n in range(9)]
    tickets = ("Google Chrome", "https://a/tickets", None, texts("Standard $45", *rows, "12:00"))
    tickets_later = ("Google Chrome", "https://a/tickets", None, texts("Standard $45", *rows, "12:01"))  # the same screen
    checkout = ("Google Chrome", "https://a/checkout", None, texts("Order summary", "Pay now"))
    state = Session(seen=[(home, "clicked 'Tickets'"), (tickets, "waited"), (tickets_later, "clicked 'Buy'")])
    assert earlier_screens(state, checkout) == [
        {"app": "Google Chrome", "url": "https://a/", "text": ["Home", "Tickets"], "actions_since": 3},
        {"app": "Google Chrome", "url": "https://a/tickets", "text": ["Standard $45", *rows, "12:01"], "actions_since": 1},
    ]
    assert earlier_screens(state, checkout, budget=12) == [  # eleven lines fit; home's two more do not
        {"app": "Google Chrome", "url": "https://a/tickets", "text": ["Standard $45", *rows, "12:01"], "actions_since": 1},
    ]
    assert earlier_screens(state, tickets) == [
        {"app": "Google Chrome", "url": "https://a/", "text": ["Home", "Tickets"], "actions_since": 3}
    ]
    assert earlier_screens(Session(), checkout) == []


def test_a_wait_is_neither_a_repeat_nor_something_the_classifier_is_told_it_tried():
    loading = ("Google Chrome", "https://a/tickets", None, texts("Loading..."))
    state = Session(last=loading)
    lines: list[str] = []
    for _ in range(MAX_REPEATS + 1):
        assert not repeating(state, "waited", True, lines.append)
    assert state.seen == [(loading, None)] * (MAX_REPEATS + 1)  # the screen is on record, with no action to steer around
    assert tried_here(state) == [] and lines == []
    assert not repeating(state, "clicked 'Buy'", False, lines.append)  # the first time on this screen
    assert not repeating(state, "clicked 'Buy'", False, lines.append)  # one repeat is a warning shot
    assert repeating(state, "clicked 'Buy'", False, lines.append) and state.outcome == "stalled"
    assert tried_here(state) == ["clicked 'Buy'"] * MAX_REPEATS + ["clicked 'Buy'"]


def _stateful(text: str, **kwargs) -> Item:
    return Item(0, text, 1.0, 100, 100, 400, 140, source="ax", **kwargs)


def test_flipping_a_switch_is_a_screen_change(screen):
    off = _stateful("TUN", role="button", controls=("toggle",), checked=True)
    on = _stateful("TUN", role="button", controls=("toggle",), checked=False)
    assert not same_screen(signature(screen, [off]), signature(screen, [on]))


def test_a_progress_value_is_not_a_screen_change(screen):
    early = _stateful("Loading", role="progress", current="10")
    later = _stateful("Loading", role="progress", current="40")
    assert same_screen(signature(screen, [early]), signature(screen, [later]))


def test_the_step_record_carries_the_stop_rules_standing(screen, make_item):
    kind = SimpleNamespace(choice="click_item", confidence=0.9, probabilities={"click_item": 0.9})
    item = SimpleNamespace(choice="0", confidence=0.8, probabilities={"0": 0.8})
    site = SimpleNamespace(choice="none", confidence=1.0, probabilities={"none": 1.0})
    decision = Decision(kind=kind, item=item, site=site)
    state = Session(idle=1, repeats=0)
    record = answers(decision, screen, [make_item(0, "Buy")], {"total": 0.5}, ["clicked 'Terms'"], state)
    assert record["already_tried_on_this_screen"] == ["clicked 'Terms'"]
    assert record["idle_actions"] == 1 and record["repeated_actions"] == 0
    assert record["chosen"] == "0" and record["items"][0]["text"] == "Buy"
