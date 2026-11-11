"""The `rapidocr` OCR backend of `osworld.ocr`, until that choice of backends goes.

The code is `parse.ocr`. Importing this module fails where RapidOCR is not installed, which is how
`osworld.ocr` tells that the backend is unavailable.
"""

from __future__ import annotations

import rapidocr  # noqa: F401

from .parse.ocr import INK_CONTRAST, _engine, hug_ink, lines_of, reading_order, recognize_text, to_line

__all__ = ["INK_CONTRAST", "_engine", "hug_ink", "lines_of", "reading_order", "recognize_text", "to_line"]
