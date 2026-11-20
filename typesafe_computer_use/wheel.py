"""How many wheel units one scroll action sends on Windows.

`pyautogui.scroll` and `SendInput` both take a wheel delta, and 120 is one notch. The actions ask
for lines, three to a notch, which is the Windows default. This module has no platform imports, so
the WAA adapter can use it on Linux, where `windows.py` cannot be imported.
"""

from __future__ import annotations

WHEEL_DELTA = 120  # one notch of the wheel
LINES_PER_NOTCH = 3  # the Windows default; the actions ask for lines, as on macOS


def wheel_delta(lines: int) -> int:
    """The wheel delta that scrolls `lines` lines; positive scrolls up, as on macOS."""
    return round(lines * WHEEL_DELTA / LINES_PER_NOTCH)
