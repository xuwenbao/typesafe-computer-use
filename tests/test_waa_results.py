"""Reading a WindowsAgentArena result tree. The folders are made here; nothing is run."""

from __future__ import annotations

import json
from pathlib import Path

from typesafe_computer_use.waa.results import read, summarize

RUN = {
    "outcome": "done",
    "seconds": 40,
    "perception": {"pipeline": "screen-v1"},
    "usage": {"typesafe": {"requests": 2, "input_tokens": 100, "output_tokens": 5}},
    "waa": {
        "commit": "abc",
        "image_digest": "sha256:def",
        "browser": "Google Chrome",
        "tree": "jev-uia",
        "tree_fetches": 3,
        "tree_failures": 0,
    },
}


def task(root: Path, domain: str, task_id: str, score: str | None, stamps: tuple[str, str] | None = None) -> Path:
    folder = root / "pyautogui" / "screenshot" / "jev" / "0" / domain / task_id
    folder.mkdir(parents=True)
    if score is not None:
        (folder / "result.txt").write_text(score + "\n", encoding="utf-8")
    if stamps is not None:
        lines = [
            {"step_num": 1, "action": "pyautogui.click(1, 1)", "action_timestamp": stamps[0]},
            {"step_num": 2, "action": "DONE", "action_timestamp": stamps[1]},
        ]
        (folder / "traj.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    jev = folder / "jev"
    jev.mkdir()
    (jev / "run.json").write_text(json.dumps(RUN), encoding="utf-8")
    (folder / "usage.json").write_text(json.dumps({"usage": {"ghost": {"input_tokens": 9999}}}), encoding="utf-8")
    return folder


def test_scores_split_solved_from_failed_and_a_missing_score_is_only_listed(tmp_path: Path):
    task(tmp_path, "chrome", "one", "1", ("20261112@100000", "20261112@100030"))
    task(tmp_path, "chrome", "two", "0", ("20261112@110000", "20261112@110100"))
    task(tmp_path, "notepad", "three", None)
    # an earlier run, moved aside, is not read
    old = tmp_path / "archive" / "20261112-090000" / "pyautogui" / "screenshot" / "jev" / "0" / "chrome" / "one"
    old.mkdir(parents=True)
    (old / "result.txt").write_text("0\n", encoding="utf-8")

    found = read(tmp_path)
    by_id = {item.task_id: item for item in found}
    assert set(by_id) == {"one", "two", "three"}
    assert by_id["one"].seconds == 30
    assert by_id["one"].usage["typesafe"].input_tokens == 100
    assert "ghost" not in by_id["one"].usage
    assert by_id["one"].jev is not None and by_id["one"].jev.browser == "Google Chrome"
    assert by_id["three"].score is None

    text = "\n".join(summarize(found))
    assert "1 solved, 1 failed, 1 not scored" in text
    assert "notepad/three" in text
    assert "9999" not in text


def test_a_timestamp_with_microseconds_is_not_a_waa_stamp(tmp_path: Path):
    task(tmp_path, "chrome", "one", "1", ("20261112@100000123456", "20261112@100030123456"))
    (found,) = read(tmp_path)
    assert found.seconds is None
