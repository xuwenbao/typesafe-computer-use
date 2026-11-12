"""What a WindowsAgentArena run scored, read from the result folders its runner writes.

WAA files each task under `<results>/<action_space>/<observation_type>/<model>/<trial>/<domain>/<id>/`:
`result.txt` holds the score and `traj.jsonl` one line per action, stamped `%Y%m%d@%H%M%S`. jev adds
its own run folder there, `jev/`, whose `run.json` holds the outcome, the tokens per model, and the
`waa` record (the commit, the image, the browser, and the tree fetches). There is no `usage.json`.

`scripts/waa results` prints this, and after it a summary: the means over the tasks that scored 1 or
more and, apart, over the tasks that scored below 1. A task with no score is listed, not averaged.
Earlier runs that `scripts/waa` moved aside live under `<results>/archive/` and are left out.

    python -m typesafe_computer_use.waa.results [results folder]
"""

from __future__ import annotations

import itertools
import json
import statistics
import sys
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path

ARCHIVE = "archive"
RUN_FOLDER = "jev"
TIMESTAMP = "%Y%m%d@%H%M%S"


@dataclass(frozen=True)
class Usage:
    requests: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0


@dataclass(frozen=True)
class Jev:
    """What jev's run.json says about the run."""

    outcome: str | None
    seconds: float | None
    perception: str | None
    browser: str | None
    tree: str | None
    tree_fetches: int | None
    tree_failures: int | None
    usage: dict[str, Usage] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskResult:
    folder: Path
    action_space: str
    observation_type: str
    model: str
    trial: str
    domain: str
    task_id: str
    score: str | None
    steps: int
    actions: int
    seconds: float | None
    errors: list[str]
    jev: Jev | None
    usage: dict[str, Usage] = field(default_factory=dict)


def read(root: Path) -> list[TaskResult]:
    """Every task folder under `root`, leaving out the archive."""
    found = []
    for folder in sorted(root.glob("*/*/*/*/*/*")):
        if not folder.is_dir() or folder.relative_to(root).parts[0] == ARCHIVE:
            continue
        if not any((folder / name).exists() for name in ("result.txt", "traj.jsonl", RUN_FOLDER)):
            continue
        found.append(task(folder))
    return sorted(found, key=lambda r: (r.model, r.domain, r.task_id, r.trial))


def task(folder: Path) -> TaskResult:
    """One task's result folder."""
    action_space, observation_type, model, trial, domain, task_id = folder.parts[-6:]
    score_file = folder / "result.txt"
    score = score_file.read_text(encoding="utf-8").strip() if score_file.is_file() else None
    steps, actions, seconds, errors = _trajectory(folder / "traj.jsonl")
    jev = _jev(folder / RUN_FOLDER / "run.json")
    return TaskResult(
        folder=folder,
        action_space=action_space,
        observation_type=observation_type,
        model=model,
        trial=trial,
        domain=domain,
        task_id=task_id,
        score=score,
        steps=steps,
        actions=actions,
        seconds=seconds,
        errors=errors,
        jev=jev,
        usage=jev.usage if jev is not None else {},
    )


def _trajectory(path: Path) -> tuple[int, int, float | None, list[str]]:
    if not path.is_file():
        return 0, 0, None, []
    steps, actions, errors, times = 0, 0, [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        if "Error" in entry:
            errors.append(str(entry["Error"]))
        if "action" in entry:
            actions += 1
        if isinstance(entry.get("step_num"), int):
            steps = max(steps, entry["step_num"])
        with suppress(KeyError, ValueError):
            times.append(datetime.strptime(str(entry["action_timestamp"]), TIMESTAMP))
    seconds = (max(times) - min(times)).total_seconds() if times else None
    return steps, actions, seconds, errors


def _jev(path: Path) -> Jev | None:
    if not path.is_file():
        return None
    try:
        summary = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return Jev(None, None, None, None, None, None, None)
    waa = summary.get("waa") or {}
    fetches, failures = waa.get("tree_fetches"), waa.get("tree_failures")
    perception = (summary.get("perception") or {}).get("pipeline")
    return Jev(
        outcome=summary.get("outcome"),
        seconds=summary.get("seconds"),
        perception=perception,
        browser=waa.get("browser"),
        tree=waa.get("tree"),
        tree_fetches=fetches if isinstance(fetches, int) else None,
        tree_failures=failures if isinstance(failures, int) else None,
        usage=_usage(summary),
    )


def _usage(summary: dict) -> dict[str, Usage]:
    return {
        model: Usage(**{name: int(counts.get(name) or 0) for name in Usage.__dataclass_fields__})
        for model, counts in (summary.get("usage") or {}).items()
        if isinstance(counts, dict)
    }


def duration(seconds: float) -> str:
    """72.4 -> '1m 12s'."""
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes}m {rest:02d}s" if minutes else f"{rest}s"


def describe(result: TaskResult) -> list[str]:
    """One task as a few lines of text."""
    lines = [f"{result.model}  {result.domain}/{result.task_id}  trial {result.trial}"]

    def row(label: str, text: str) -> None:
        lines.append(f"  {label:<12} {text}")

    row("score", result.score if result.score is not None else "none (the run never reached evaluation)")
    steps = f"{result.steps} ({result.actions} actions)"
    if result.seconds is not None:
        steps += f", {duration(result.seconds)} from the first action to the last"
    row("steps", steps)
    for error in result.errors:
        row("error", error)
    if result.jev is not None:
        jev = result.jev
        ended = jev.outcome or "no outcome"
        if jev.seconds is not None:
            ended += f" after {duration(jev.seconds)}"
        tree = ""
        if jev.tree is not None:
            tree = f", tree {jev.tree}"
            if jev.tree_fetches is not None:
                tree += f" ({jev.tree_fetches} fetches"
                tree += f", {jev.tree_failures} failed)" if jev.tree_failures else ")"
        row("jev", f"{ended}; perception {jev.perception}, browser {jev.browser}{tree}")
    label = "tokens"
    for model, used in sorted(result.usage.items()):
        reasoning = f" ({used.reasoning_tokens:,} reasoning)" if used.reasoning_tokens else ""
        row(
            label,
            f"{model}: {used.requests:,} request{'' if used.requests == 1 else 's'}, {used.input_tokens:,} in, "
            f"{used.cached_input_tokens:,} cached in, {used.output_tokens:,} out{reasoning}",
        )
        label = ""
    lines.append(f"  {'folder':<12} {result.folder}")
    return lines


def solved(result: TaskResult) -> bool | None:
    """A score of 1 or more is solved, below 1 failed, and a missing or non-numeric score is None."""
    if result.score is None:
        return None
    try:
        return float(result.score) >= 1
    except ValueError:
        return None


TOKENS = {"input_tokens": "in", "cached_input_tokens": "cached in", "output_tokens": "out", "reasoning_tokens": "reasoning"}


def _tokens(result: TaskResult, model: str, name: str) -> int | None:
    return getattr(result.usage.get(model, Usage()), name) if result.usage else None


def _mean(values: list, show: Callable[[float], str]) -> str:
    known = [value for value in values if value is not None]
    if not known:
        return "-"
    text = show(sum(known) / len(known))
    return text if len(known) == len(values) else f"{text} ({len(known)} of {len(values)})"


def summarize(results: list[TaskResult]) -> list[str]:
    """Means over the solved tasks and, apart, over the failed ones. An unscored task is only listed."""
    won = [r for r in results if solved(r) is True]
    lost = [r for r in results if solved(r) is False]
    unscored = [r for r in results if solved(r) is None]
    lines = [
        f"{results[0].model}  summary of {len(results)} task{'' if len(results) == 1 else 's'}: "
        f"{len(won)} solved, {len(lost)} failed, {len(unscored)} not scored"
    ]
    table = [("mean per task", f"solved ({len(won)})", f"failed ({len(lost)})")]

    def row(label: str, value: Callable[[TaskResult], float | None], show: Callable[[float], str]) -> None:
        groups = [[value(r) for r in group] for group in (won, lost)]
        known = [v for group in groups for v in group if v is not None]
        if known and (any(known) or not label.endswith(" reasoning")):
            table.append((label, *(_mean(group, show) for group in groups)))

    row("steps", lambda r: r.steps, "{:.1f}".format)
    row("first to last action", lambda r: r.seconds, duration)
    row("jev's run time", lambda r: r.jev.seconds if r.jev is not None else None, duration)
    for model in sorted({model for r in won + lost for model in r.usage}):
        for name, label in TOKENS.items():
            row(f"{model} {label}", partial(_tokens, model=model, name=name), "{:,.0f}".format)
    if won or lost:
        widths = [max(len(cells[i]) for cells in table) for i in range(3)]
        for label, *cells in table:
            lines.append(f"  {label:<{widths[0]}}" + "".join(f"   {c:>{w}}" for c, w in zip(cells, widths[1:], strict=True)))
    to_failure = [r.seconds for r in lost if r.seconds is not None]
    if to_failure:
        lines.append(f"  median time to failure: {duration(statistics.median(to_failure))}, from the first action to the last")
    label = "not scored"
    for r in unscored:
        lines.append(f"  {label:<12} {r.domain}/{r.task_id}")
        label = ""
    return lines


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) > 1:
        print("usage: python -m typesafe_computer_use.waa.results [results folder]", file=sys.stderr)
        return 2
    root = Path(args[0] if args else "results")
    results = read(root)
    if not results:
        print(f"no WindowsAgentArena results under {root}")
        return 0
    blocks = [describe(r) for r in results]
    blocks += [summarize(list(tasks)) for _, tasks in itertools.groupby(results, key=lambda r: r.model)]
    print("\n\n".join("\n".join(block) for block in blocks))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
