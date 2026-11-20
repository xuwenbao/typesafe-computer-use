"""One screenshot, then the focused control and the address bar, in a process of its own.

The parent kills this process when the read does not return. Nothing it calls is marshaled back
onto the parent's thread. The screenshot is on disk before `shot` is printed, so the parent can
keep the picture when the later read is the one that sticks.
"""

from __future__ import annotations

import json


def main() -> None:
    import sys

    png = sys.argv[1]
    browser = sys.argv[2] if len(sys.argv) > 2 else ""
    from typesafe_computer_use.windows_session import attach_input_desktop

    attach_input_desktop()
    from typesafe_computer_use.windows import browser_url, focused_field, screenshot

    screenshot().save(png)
    print("shot", flush=True)
    field = None
    url = None
    try:
        field = focused_field()
    except Exception:
        field = None
    try:
        url = browser_url(browser) if browser else None
    except Exception:
        url = None
    print(json.dumps({"field": None if field is None else field.record(), "url": url}), flush=True)


if __name__ == "__main__":
    main()
