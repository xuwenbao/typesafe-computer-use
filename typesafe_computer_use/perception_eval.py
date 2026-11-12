"""Score the desktop perception on saved captures: `clicker-perception-eval`.

An evaluation set is a folder of PNG captures, each beside a `<name>.labels.json` that lists the
things a person would click on it:

    {
      "tags": ["zh", "icon-toolbar"],
      "region": [0, 0, 1440, 900],
      "targets": [
        {"box": [10, 20, 80, 40], "kind": "text", "text": "保存"},
        {"box": [100, 100, 132, 132], "kind": "icon"}
      ]
    }

Boxes are x1, y1, x2, y2 in the capture's own pixels. `region` is the part a step would read, the
frontmost window, and is the whole capture when left out. `tags` say what the capture covers, so
the set can be checked for the kinds of screen it must hold (`COVERAGE`).

A target is hit when an item's center falls inside its box. A text target's error is the character
error rate of its text against the best-matching run of the item's text, since an item can be a
paragraph that holds the target's line. The report is JSON, and two reports compare target by
target. The tool reads image files only: it captures no screen and sends no input, though the
pipeline it scores may call the platform's OCR on the file.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from .models import Item

KINDS = ("text", "icon")
COVERAGE = ("zh", "retina-small-text", "icon-toolbar", "waa", "windows")
LABELS_SUFFIX = ".labels.json"

Box = tuple[float, float, float, float]
Read = Callable[[Image.Image], tuple[list[Item], dict[str, float]]]  # a capture to items, and seconds per phase


class LabelError(ValueError):
    """A labels file that does not follow the format, naming the file and the target."""


@dataclass(frozen=True)
class Target:
    box: Box
    kind: str
    text: str = ""


@dataclass(frozen=True)
class Sample:
    name: str
    image: Path
    targets: list[Target]
    region: Box | None = None
    tags: list[str] = field(default_factory=list)


def parse_labels(data: object, where: str) -> tuple[list[Target], Box | None, list[str]]:
    """The targets, region, and tags of one labels file. `where` names the file in an error."""
    if not isinstance(data, dict) or not isinstance(data.get("targets"), list):
        raise LabelError(f"{where}: expected an object with a list of targets")
    region = data.get("region")
    if region is not None:
        region = _box(region, f"{where}: region")
    tags = data.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise LabelError(f"{where}: tags must be a list of strings")
    targets = []
    for i, raw in enumerate(data["targets"]):
        at = f"{where}: target {i}"
        if not isinstance(raw, dict):
            raise LabelError(f"{at}: expected an object")
        kind = raw.get("kind")
        if kind not in KINDS:
            raise LabelError(f"{at}: kind must be one of {', '.join(KINDS)}, not {kind!r}")
        text = raw.get("text", "")
        if not isinstance(text, str):
            raise LabelError(f"{at}: text must be a string")
        if kind == "text" and not text.strip():
            raise LabelError(f"{at}: a text target needs its text")
        targets.append(Target(_box(raw.get("box"), at), kind, text))
    return targets, region, tags


def _box(raw: object, where: str) -> Box:
    if not (isinstance(raw, list) and len(raw) == 4 and all(isinstance(v, int | float) for v in raw)):
        raise LabelError(f"{where}: box must be four numbers, x1, y1, x2, y2")
    x1, y1, x2, y2 = (float(v) for v in raw)
    if x2 <= x1 or y2 <= y1:
        raise LabelError(f"{where}: box must have x2 > x1 and y2 > y1")
    return (x1, y1, x2, y2)


def load_set(folder: Path) -> list[Sample]:
    """Every labelled capture in `folder`, by name. A capture without labels is not part of the set."""
    samples = []
    for labels in sorted(folder.glob(f"*{LABELS_SUFFIX}")):
        name = labels.name.removesuffix(LABELS_SUFFIX)
        image = labels.with_name(name + ".png")
        if not image.is_file():
            raise LabelError(f"{labels}: no capture beside it named {image.name}")
        targets, region, tags = parse_labels(json.loads(labels.read_text(encoding="utf-8")), str(labels))
        samples.append(Sample(name, image, targets, region, tags))
    return samples


def missing_coverage(samples: list[Sample]) -> list[str]:
    """The kinds of screen in `COVERAGE` that no capture in the set is tagged with."""
    seen = {tag for sample in samples for tag in sample.tags}
    return [tag for tag in COVERAGE if tag not in seen]


# ------------------------------------------------------------------ metrics


def hits(target: Target, items: list[Item]) -> list[Item]:
    """The items whose center falls inside the target's box."""
    x1, y1, x2, y2 = target.box
    return [it for it in items if x1 <= it.center[0] <= x2 and y1 <= it.center[1] <= y2]


def normalize(text: str) -> str:
    return " ".join(text.split())


def cer(reference: str, read: str) -> float:
    """Character error rate of `reference` against the run of `read` that matches it best: edits over
    the reference's length, where skipping text before or after the run costs nothing."""
    ref, hyp = normalize(reference), normalize(read)
    if not ref:
        return 0.0
    previous = [0] * (len(hyp) + 1)  # a match may start anywhere in `hyp`
    for i, r in enumerate(ref, 1):
        current = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (r != h))
        previous = current
    return min(previous) / len(ref)  # and end anywhere


@dataclass(frozen=True)
class Scored:
    target: Target
    hit: bool
    cer: float | None  # text targets that were hit only
    item_text: str | None

    def record(self) -> dict:
        return {
            "kind": self.target.kind,
            "box": list(self.target.box),
            "text": self.target.text,
            "hit": self.hit,
            "cer": None if self.cer is None else round(self.cer, 4),
            "item_text": self.item_text,
        }


def score(target: Target, items: list[Item]) -> Scored:
    """A target against the items read off its capture. A text target takes the hit whose text reads it best."""
    found = hits(target, items)
    if not found:
        return Scored(target, False, None, None)
    if target.kind != "text":
        return Scored(target, True, None, found[0].text)
    best = min(found, key=lambda it: cer(target.text, it.text))
    return Scored(target, True, cer(target.text, best.text), best.text)


def summarize(images: list[dict]) -> dict:
    """Recall per kind, the mean error over the text targets that were hit, and the mean seconds per phase."""
    targets = [t for image in images for t in image["targets"]]
    out: dict = {}
    for kind in KINDS:
        of_kind = [t for t in targets if t["kind"] == kind]
        found = sum(t["hit"] for t in of_kind)
        out[kind] = {"targets": len(of_kind), "hits": found, "recall": round(found / len(of_kind), 4) if of_kind else None}
    errors = [t["cer"] for t in targets if t["cer"] is not None]
    out["cer"] = round(sum(errors) / len(errors), 4) if errors else None
    phases = sorted({name for image in images for name in image["seconds"]})
    out["seconds"] = (
        {name: round(sum(image["seconds"].get(name, 0.0) for image in images) / len(images), 3) for name in phases}
        if images
        else {}
    )
    return out


# ------------------------------------------------------------------ running a pipeline


def current_pipeline(image: Image.Image) -> tuple[list[Item], dict[str, float]]:
    """The desktop perception's text and icons on one capture, without an accessibility tree."""
    from .config import MAX_OPTIONS
    from .models import Screen
    from .perception import ocr
    from .timing import EXTRAS, phase

    timing: dict[str, float] = {}
    screen = Screen(image=image, scale=1.0, app="", field=None, url=None)
    with phase(timing, "total"):
        items = ocr(screen, MAX_OPTIONS, "", None, timing)
    return items, {name: seconds for name, seconds in timing.items() if name not in EXTRAS}


def pipeline_info() -> dict:
    from .perception import pipeline_record

    return {**pipeline_record(), "platform": sys.platform}


def read_sample(sample: Sample, read: Read) -> tuple[list[Item], dict[str, float]]:
    """The items of one capture, read over its region, with boxes in the whole capture's pixels."""
    with Image.open(sample.image) as opened:
        image = opened.convert("RGB")
    if sample.region is None:
        return read(image)
    x1, y1, x2, y2 = (round(v) for v in sample.region)
    items, seconds = read(image.crop((x1, y1, x2, y2)))
    shifted = [
        Item(it.index, it.text, it.ocr_confidence, it.x1 + x1, it.y1 + y1, it.x2 + x1, it.y2 + y1, it.role, it.source)
        for it in items
    ]
    return shifted, seconds


def evaluate(samples: list[Sample], read: Read, info: dict | None = None) -> dict:
    """The report for one pipeline over one set."""
    images = []
    for sample in samples:
        started = time.perf_counter()
        items, seconds = read_sample(sample, read)
        seconds = {**seconds, "wall": round(time.perf_counter() - started, 3)}
        images.append(
            {
                "name": sample.name,
                "tags": sample.tags,
                "items": len(items),
                "seconds": seconds,
                "targets": [score(t, items).record() for t in sample.targets],
            }
        )
    return {**(info or {}), "summary": summarize(images), "images": images}


def compare(base: dict, new: dict) -> dict:
    """What changed from `base` to `new`: the summary's differences, and each target that was hit in
    one and missed in the other, matched by capture name and position in its labels."""

    def delta(a: float | None, b: float | None) -> float | None:
        return None if a is None or b is None else round(b - a, 4)

    s0, s1 = base["summary"], new["summary"]
    summary = {kind: {"recall": delta(s0[kind]["recall"], s1[kind]["recall"])} for kind in KINDS}
    summary["cer"] = delta(s0["cer"], s1["cer"])
    summary["seconds"] = {name: delta(s0["seconds"].get(name), s1["seconds"].get(name)) for name in s1["seconds"]}
    lost, gained = [], []
    before = {image["name"]: image["targets"] for image in base["images"]}
    for image in new["images"]:
        old = before.get(image["name"])
        if old is None:
            continue
        for i, (a, b) in enumerate(zip(old, image["targets"], strict=False)):
            entry = {"image": image["name"], "target": i, "kind": b["kind"], "text": b["text"]}
            if a["hit"] and not b["hit"]:
                lost.append(entry)
            elif b["hit"] and not a["hit"]:
                gained.append(entry)
    return {
        "base": base.get("pipeline"),
        "new": new.get("pipeline"),
        "summary": summary,
        "lost": lost,
        "gained": gained,
    }


# ------------------------------------------------------------------ command line


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="clicker-perception-eval", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="score the current pipeline on an evaluation set")
    run.add_argument("folder", type=Path, help="captures with their .labels.json files")
    run.add_argument("--out", type=Path, help="write the report here instead of printing it")
    check = sub.add_parser("check", help="validate an evaluation set's labels and coverage, reading no image")
    check.add_argument("folder", type=Path)
    diff = sub.add_parser("compare", help="what changed from one report to another")
    diff.add_argument("base", type=Path)
    diff.add_argument("new", type=Path)
    args = parser.parse_args(argv)

    try:
        if args.command == "compare":
            out = compare(*(json.loads(p.read_text(encoding="utf-8")) for p in (args.base, args.new)))
        else:
            samples = load_set(args.folder)
            if not samples:
                raise LabelError(f"{args.folder}: no {LABELS_SUFFIX} files")
            if args.command == "check":
                out = {"captures": len(samples), "missing_coverage": missing_coverage(samples)}
            else:
                out = evaluate(samples, current_pipeline, pipeline_info())
    except LabelError as exc:
        print(f"clicker-perception-eval: {exc}", file=sys.stderr)
        return 2
    text = json.dumps(out, ensure_ascii=False, indent=2)
    if getattr(args, "out", None):
        args.out.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
