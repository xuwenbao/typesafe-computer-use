"""Icon boxes and OCR lines fused into one element per thing, after OmniParser's `remove_overlap_new`.

Pure geometry on capture pixels; no model, no PyTorch. The rules, in order:

1. Of two icon boxes that overlap by more than `ICON_OVERLAP`, the larger goes. Overlap is the
   most of IoU and the intersection's share of either box, so a box nested in another counts.
2. An icon box that lies mostly (`INSIDE`) within an OCR line is part of that text, and goes.
3. An OCR line that lies mostly within an icon box is that icon's text, and leaves the lines.
   A line joins the first icon that holds it only, so one word never becomes two options.
4. Every other icon has no text: it is captioned later.

Fusion runs on lines, before lines join into paragraphs: a paragraph's box is wide enough to
swallow the small icon beside it under rule 2.
"""

from __future__ import annotations

from dataclasses import dataclass

Box = tuple[float, float, float, float]
Line = tuple[str, float, Box]  # text, confidence, box

ICON_OVERLAP = 0.7
INSIDE = 0.8


@dataclass(frozen=True)
class Icon:
    """One detected icon. `text` is the OCR inside it, joined, or None for an icon left to caption."""

    box: Box
    score: float
    text: str | None = None
    text_confidence: float | None = None  # the least confident of the lines that made `text`


def area(box: Box) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def intersection(a: Box, b: Box) -> float:
    return area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))


def overlap(a: Box, b: Box) -> float:
    """The most of IoU and the intersection's share of either box."""
    inter, area_a, area_b = intersection(a, b), area(a), area(b)
    if area_a <= 0 or area_b <= 0:
        return 0.0
    return max(inter / (area_a + area_b - inter), inter / area_a, inter / area_b)


def inside(inner: Box, outer: Box, share: float = INSIDE) -> bool:
    """Whether more than `share` of `inner` lies within `outer`."""
    a = area(inner)
    return a > 0 and intersection(inner, outer) / a > share


def distinct_icons(icons: list[tuple[Box, float]], threshold: float = ICON_OVERLAP) -> list[tuple[Box, float]]:
    """Rule 1: drop each icon box that overlaps a smaller one by more than `threshold`."""
    kept = []
    for i, (box, score) in enumerate(icons):
        if area(box) <= 0:
            continue
        smaller = any(
            j != i and area(other) > 0 and overlap(box, other) > threshold and area(box) > area(other)
            for j, (other, _) in enumerate(icons)
        )
        if not smaller:
            kept.append((box, score))
    return kept


def fuse(lines: list[Line], icons: list[tuple[Box, float]]) -> tuple[list[Line], list[Icon]]:
    """The OCR lines that stay text, and the icons with their text or None, both in the order given."""
    taken: set[int] = set()
    fused: list[Icon] = []
    for box, score in distinct_icons(icons):
        if any(inside(box, line_box) for _, _, line_box in lines):
            continue
        held = [i for i, (_, _, line_box) in enumerate(lines) if i not in taken and inside(line_box, box)]
        taken.update(held)
        if held:
            text = " ".join(lines[i][0] for i in held)
            fused.append(Icon(box, score, text, min(lines[i][1] for i in held)))
        else:
            fused.append(Icon(box, score))
    return [line for i, line in enumerate(lines) if i not in taken], fused
