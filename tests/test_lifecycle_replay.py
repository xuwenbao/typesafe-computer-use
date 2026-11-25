"""Baselines for one run's visible result, taken before the agent refactor.

Each scenario drives the real loop through `tests/world.py` and records the history, the outcome,
the writer's route, the keys of `run.json`, and every other file in the run folder. Clocks, durations,
and the fresh picture reference are taken out, so a later refactor can be checked against these files
byte for byte. Pictures are stored as a hash: a 2000x1200 capture is deterministic but large.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
from world import FakeWriter, Page, World, drive, scripted

from yuanji_computer_use.bound import CaptureTimeout

FIX = Path(__file__).parent / "fixtures" / "lifecycle"
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
DROP = {"seconds", "timing"}
FIXED_NOW = {"local_time": "2026-11-24 10:00 Tuesday", "timezone": "UTC", "today": "2026-11-24"}

FOCUS = {"focus": "Click the row the goal names", "route": "classifier"}


def _always(kind: str, target: str):
    return lambda _state, _questions: (kind, target)


def _page(name: str, items: list, nxt: str | None = None, url: str = "https://example.com/x") -> Page:
    return Page(name=name, items=items, url=url, on={f"click:{items[0]}": nxt} if nxt else {})


def _cases() -> dict:
    """One world, policy, and drive options per outcome the loop can name."""
    listing = World([_page("list", ["Tickets"], "bought"), _page("bought", ["You are going"], url="https://example.com/done")])
    stay = World([Page(name="stay", items=["Stay"], url="https://example.com/stay")])
    walk = World(
        [
            _page("a", ["Next"], "b", "https://example.com/a"),
            _page("b", ["Next"], "c", "https://example.com/b"),
            _page("c", ["Landed"], url="https://example.com/c"),
        ]
    )
    stopped = World([Page(name="only", items=["Nothing here"], url="https://example.com/only")])

    def after_focus(state, _questions):
        texts = [it["text"] for it in state["screen_items_in_reading_order"]]
        if state.get("current_focus") and "Tickets" in texts:
            return ("click_item", "Tickets")
        if state.get("current_focus"):
            return ("done", None)
        return ("click_item", "Tickets", 0.1)

    return {
        "done": (listing, scripted(("done", None)), {}, None),
        "low_confidence_handoff": (listing, after_focus, {}, FakeWriter(reviews=[FOCUS])),
        "stalled": (stay, _always("click_item", "Stay"), {}, None),
        "stuck": (stay, _always("click_item", "Stay"), {}, FakeWriter(reviews=[FOCUS, FOCUS])),
        "step_limit": (walk, _always("click_item", "Next"), {"steps": 2}, None),
        "dry_run": (walk, scripted(("click_item", "Next")), {"act": False}, None),
        "capture_timeout": (stopped, scripted(("done", None)), {"capture": _timeout}, None),
        "driver_done": (
            stopped,
            scripted(("none", None)),
            {"driver_steps": 5},
            FakeWriter(reviews=[{"achieved": False}], drives=[{"action": "done", "achieved": True}]),
        ),
        "driver_failed": (
            stopped,
            scripted(("none", None)),
            {"driver_steps": 5},
            FakeWriter(reviews=[{"achieved": False}, {"achieved": False}], drives=[{"action": "text"}, {"action": "text"}]),
        ),
    }


def _timeout(*_args, **_kwargs):
    raise CaptureTimeout("screenshot")


def _scrub(value):
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items() if key not in DROP}
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, str):
        return UUID.sub("ref", value)
    return value


# Older runs recorded these keys. A replay drops them before comparing, and does not
# require them: the planner that wrote them is gone, and the fixtures stay as recorded.
_PLAN_KEYS = {"plan", "verdicts", "widened", "replans", "planner_calls", "planner_seconds"}


def _snapshot(out: Path, state) -> dict:
    files = {}
    for path in sorted(out.iterdir()):
        if path.name in {"run.json", "run.log"}:
            continue
        raw = path.read_bytes()
        if path.suffix == ".json":
            files[path.name] = _scrub(json.loads(raw))
        elif path.suffix in {".txt", ".log"}:
            files[path.name] = UUID.sub("ref", raw.decode())
        else:
            files[path.name] = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    summary = json.loads((out / "run.json").read_text())
    return {
        "outcome": state.outcome,
        "route": state.route,
        "history": state.history,
        "run_json_keys": sorted(set(summary) - _PLAN_KEYS),
        "files": files,
    }


def _freeze_clock(monkeypatch) -> None:
    for module in ("yuanji_computer_use.decide", "yuanji_computer_use.writer", "yuanji_computer_use.driver_agent"):
        monkeypatch.setattr(f"{module}.now_context", lambda: dict(FIXED_NOW))
    # `dates.now_context` itself, in case a later import reads it from there.
    monkeypatch.setattr("yuanji_computer_use.dates.now_context", lambda: dict(FIXED_NOW))


@pytest.mark.parametrize("name", list(_cases()))
def test_lifecycle_matches_the_recorded_run(name, monkeypatch, tmp_path, clean_env):
    _freeze_clock(monkeypatch)
    world, policy, options, writer = _cases()[name]
    state = drive(world, policy, goal="finish the task", monkeypatch=monkeypatch, tmp_path=tmp_path, writer=writer, **options)
    got = _snapshot(tmp_path / "run", state)
    path = FIX / f"{name}.json"
    text = json.dumps(got, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    assert path.read_text() == text
