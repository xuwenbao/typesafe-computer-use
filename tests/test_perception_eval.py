import json

import pytest
from PIL import Image

from typesafe_computer_use import perception_eval as pe
from typesafe_computer_use.models import Item
from typesafe_computer_use.platform_adapter import desktop


def item(text, x1, y1, x2, y2, index=0):
    return Item(index, text, 0.9, x1, y1, x2, y2)


def write_sample(folder, name, labels, size=(400, 300)):
    Image.new("RGB", size, "white").save(folder / f"{name}.png")
    (folder / f"{name}.labels.json").write_text(json.dumps(labels, ensure_ascii=False), encoding="utf-8")


# ------------------------------------------------------------------ labels


def test_a_valid_text_target_is_accepted():
    targets, region, tags = pe.parse_labels({"targets": [{"box": [10, 20, 80, 40], "kind": "text", "text": "保存"}]}, "a")
    assert targets == [pe.Target((10.0, 20.0, 80.0, 40.0), "text", "保存")]
    assert region is None and tags == []


def test_a_text_target_without_text_names_the_file_and_the_target():
    with pytest.raises(pe.LabelError, match=r"shot\.labels\.json: target 1: a text target needs its text"):
        pe.parse_labels(
            {"targets": [{"box": [0, 0, 5, 5], "kind": "icon"}, {"box": [0, 0, 5, 5], "kind": "text"}]},
            "shot.labels.json",
        )


@pytest.mark.parametrize(
    "raw, message",
    [
        ({"box": [0, 0, 5], "kind": "icon"}, "four numbers"),
        ({"box": [5, 0, 5, 5], "kind": "icon"}, "x2 > x1"),
        ({"box": [0, 0, 5, 5], "kind": "button"}, "kind must be"),
    ],
)
def test_malformed_targets_are_rejected(raw, message):
    with pytest.raises(pe.LabelError, match=message):
        pe.parse_labels({"targets": [raw]}, "f")


def test_load_set_pairs_each_labels_file_with_its_capture(tmp_path):
    write_sample(tmp_path, "b", {"tags": ["zh"], "region": [0, 0, 200, 100], "targets": []})
    write_sample(tmp_path, "a", {"targets": [{"box": [1, 1, 9, 9], "kind": "icon"}]})
    Image.new("RGB", (10, 10)).save(tmp_path / "unlabelled.png")
    samples = pe.load_set(tmp_path)
    assert [s.name for s in samples] == ["a", "b"]
    assert samples[1].region == (0.0, 0.0, 200.0, 100.0) and samples[1].tags == ["zh"]


def test_labels_without_a_capture_are_an_error(tmp_path):
    (tmp_path / "x.labels.json").write_text('{"targets": []}', encoding="utf-8")
    with pytest.raises(pe.LabelError, match=r"no capture beside it named x\.png"):
        pe.load_set(tmp_path)


def test_missing_coverage_lists_the_kinds_no_capture_is_tagged_with():
    samples = [pe.Sample("a", None, [], tags=["zh", "linux"]), pe.Sample("b", None, [], tags=["windows"])]
    assert pe.missing_coverage(samples) == ["retina-small-text", "icon-toolbar"]


# ------------------------------------------------------------------ metrics


def test_an_item_centered_inside_the_box_is_a_hit():
    target = pe.Target((100.0, 100.0, 132.0, 132.0), "icon")
    assert pe.score(target, [item("gear", 105, 110, 125, 126)]).hit  # center (115, 118)
    assert not pe.score(target, [item("gear", 130, 100, 160, 130)]).hit  # center (145, 115)


def test_one_wrong_character_in_two_is_half_an_error():
    assert pe.cer("设置", "没置") == 0.5


def test_the_error_is_measured_against_the_best_run_of_a_longer_block():
    assert pe.cer("保存", "取消 保存 关闭") == 0.0
    assert pe.cer("Save", "Cancel Sove Close") == 0.25


def test_a_text_target_takes_the_hit_that_reads_it_best():
    target = pe.Target((0.0, 0.0, 100.0, 40.0), "text", "Settings")
    scored = pe.score(target, [item("Setings", 0, 0, 100, 40), item("Settings", 10, 10, 90, 30)])
    assert scored.cer == 0.0 and scored.item_text == "Settings"


def test_a_miss_has_no_error():
    scored = pe.score(pe.Target((0.0, 0.0, 10.0, 10.0), "text", "OK"), [])
    assert (scored.hit, scored.cer) == (False, None)


def test_summary_counts_recall_per_kind_and_the_mean_error_of_hits():
    images = [
        {
            "seconds": {"ocr": 0.2},
            "targets": [
                {"kind": "text", "hit": True, "cer": 0.0},
                {"kind": "text", "hit": True, "cer": 0.5},
                {"kind": "text", "hit": False, "cer": None},
                {"kind": "icon", "hit": False, "cer": None},
            ],
        },
        {"seconds": {"ocr": 0.4}, "targets": [{"kind": "icon", "hit": True, "cer": None}]},
    ]
    summary = pe.summarize(images)
    assert summary["text"] == {"targets": 3, "hits": 2, "recall": 0.6667}
    assert summary["icon"] == {"targets": 2, "hits": 1, "recall": 0.5}
    assert summary["cer"] == 0.25
    assert summary["seconds"] == {"ocr": 0.3}


# ------------------------------------------------------------------ running and comparing


def test_a_region_is_read_as_a_crop_and_scored_in_capture_pixels(tmp_path):
    write_sample(
        tmp_path,
        "s",
        {"region": [100, 50, 300, 250], "targets": [{"box": [110, 60, 160, 80], "kind": "text", "text": "OK"}]},
    )
    seen = []

    def read(image):
        seen.append(image.size)
        return [item("OK", 10, 10, 60, 30)], {"ocr": 0.1}

    report = pe.evaluate(pe.load_set(tmp_path), read, {"pipeline": "fake"})
    assert seen == [(200, 200)]
    assert report["pipeline"] == "fake"
    assert report["images"][0]["targets"][0]["hit"] is True
    assert report["summary"]["text"]["recall"] == 1.0


def test_the_current_pipeline_reads_the_file_through_ocr_alone(monkeypatch):
    monkeypatch.setattr(desktop, "recognize_text", lambda image: [("Open", 0.9, (10.0, 10.0, 50.0, 30.0))])
    items, seconds = pe.current_pipeline(Image.new("RGB", (200, 100)))
    assert [it.text for it in items] == ["Open"]
    assert "ocr_region_pct" not in seconds and "total" in seconds


def test_compare_reports_the_differences_and_each_target_that_changed():
    def report(pipeline, hits, recall, cer):
        targets = [{"kind": "text", "text": t, "hit": h} for t, h in zip(["A", "B"], hits, strict=True)]
        summary = {"text": {"recall": recall}, "icon": {"recall": None}, "cer": cer, "seconds": {"total": 0.5}}
        return {"pipeline": pipeline, "summary": summary, "images": [{"name": "s", "targets": targets}]}

    out = pe.compare(report("old", [True, False], 0.5, 0.1), report("new", [False, True], 0.5, 0.05))
    assert out["summary"]["text"]["recall"] == 0.0 and out["summary"]["cer"] == -0.05
    assert out["lost"] == [{"image": "s", "target": 0, "kind": "text", "text": "A"}]
    assert out["gained"] == [{"image": "s", "target": 1, "kind": "text", "text": "B"}]


def test_check_reports_coverage_without_reading_images(tmp_path, capsys):
    write_sample(tmp_path, "s", {"tags": ["zh"], "targets": []})
    assert pe.main(["check", str(tmp_path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"captures": 1, "missing_coverage": ["retina-small-text", "icon-toolbar", "linux", "windows"]}


def test_a_bad_labels_file_exits_with_its_name_and_no_report(tmp_path, capsys):
    write_sample(tmp_path, "s", {"targets": [{"box": [0, 0, 5, 5], "kind": "text"}]})
    assert pe.main(["run", str(tmp_path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "s.labels.json: target 0" in captured.err
