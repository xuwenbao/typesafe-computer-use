"""UI Automation pattern ids, with no Windows imports.

`windows.py` and the script that walks a WAA VM's tree both read this table, so a control
reports the same operations on the maintainer's machine and in the benchmark.
"""

from __future__ import annotations

INVOKE = "invoke"
TOGGLE = "toggle"
EXPAND_COLLAPSE = "expand_collapse"
SELECTION_ITEM = "selection_item"
SCROLL_ITEM = "scroll_item"
RANGE_VALUE = "range_value"
VALUE = "value"

# UI Automation's own pattern ids. The names above are what jev stores on a control.
PATTERN_IDS = {
    INVOKE: 10000,
    TOGGLE: 10015,
    EXPAND_COLLAPSE: 10005,
    SELECTION_ITEM: 10010,
    SCROLL_ITEM: 10017,
    RANGE_VALUE: 10003,
    VALUE: 10002,
}

ID_TO_NAME = {pattern_id: name for name, pattern_id in PATTERN_IDS.items()}

# What `Desktop.ax_operate` accepts. expand_toggle reads the state and expands or collapses.
OPERATIONS = ("toggle", "select", "add_select", "expand_toggle", "scroll_into_view", "set_range")
OPERATION_PATTERN = {
    "toggle": TOGGLE,
    "select": SELECTION_ITEM,
    "add_select": SELECTION_ITEM,
    "expand_toggle": EXPAND_COLLAPSE,
    "scroll_into_view": SCROLL_ITEM,
    "set_range": RANGE_VALUE,
}
