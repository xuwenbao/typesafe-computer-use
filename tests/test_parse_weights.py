import ast
import hashlib
from pathlib import Path

import pytest
from huggingface_hub.errors import LocalEntryNotFoundError

from typesafe_computer_use.parse import weights


@pytest.fixture(autouse=True)
def fresh_verify():
    weights.verify.cache_clear()


def hub(tmp_path, contents: dict[str, bytes], calls: list | None = None):
    """A stand-in for `hf_hub_download` that serves files from `tmp_path`, one folder per commit."""

    def download(repo, filename, revision):
        if calls is not None:
            calls.append((repo, filename, revision))
        if filename not in contents:
            raise LocalEntryNotFoundError("not cached")
        local = tmp_path / revision / filename
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(contents[filename])
        return str(local)

    return download


def test_every_file_is_read_at_its_pinned_commit(tmp_path, monkeypatch):
    calls = []
    files = {f: b"x" for w in weights.ALL for f in w.files}
    monkeypatch.setattr(weights, "hf_hub_download", hub(tmp_path, files, calls))
    monkeypatch.setattr(weights, "verify", lambda *a: None)
    weights.fetch_all()
    assert {(repo, revision) for repo, _, revision in calls} == {(w.repo, w.revision) for w in weights.ALL}
    assert len(calls) == sum(len(w.files) for w in weights.ALL)


def test_the_detector_is_icon_detect_v3_and_never_the_agpl_one():
    assert weights.DETECTOR.files == ("icon_detect_v3/model.pt",)
    assert not any(f.startswith("icon_detect/") for w in weights.ALL for f in w.files)


def test_a_file_with_the_pinned_digest_is_accepted(tmp_path, monkeypatch):
    data = b"detector"
    pinned = weights.Weights("r", "c", ("m.pt",), {"m.pt": hashlib.sha256(data).hexdigest()})
    monkeypatch.setattr(weights, "hf_hub_download", hub(tmp_path, {"m.pt": data}))
    assert weights.path(pinned, "m.pt").read_bytes() == data


def test_a_file_with_another_digest_names_itself(tmp_path, monkeypatch):
    pinned = weights.Weights("r", "c", ("m.pt",), {"m.pt": "0" * 64})
    monkeypatch.setattr(weights, "hf_hub_download", hub(tmp_path, {"m.pt": b"tampered"}))
    with pytest.raises(weights.WeightsError, match=r"r/m\.pt at .* has SHA-256"):
        weights.path(pinned, "m.pt")


def test_a_file_missing_offline_says_how_to_fetch_it(tmp_path, monkeypatch):
    monkeypatch.setattr(weights, "hf_hub_download", hub(tmp_path, {}))
    with pytest.raises(weights.WeightsError, match="clicker-models fetch"):
        weights.path(weights.DETECTOR, "icon_detect_v3/model.pt")


def test_folder_is_the_root_of_the_snapshot_or_its_subfolder(tmp_path, monkeypatch):
    files = dict.fromkeys(weights.CAPTIONER.files, b"x")
    monkeypatch.setattr(weights, "hf_hub_download", hub(tmp_path, files))
    monkeypatch.setattr(weights, "verify", lambda *a: None)
    root = tmp_path / weights.CAPTIONER.revision
    assert weights.folder(weights.CAPTIONER) == root
    assert weights.folder(weights.CAPTIONER, "icon_caption") == root / "icon_caption"


def test_versions_names_each_repository_once():
    assert weights.versions()["microsoft/OmniParser-v2.0"] == weights.OMNIPARSER_REVISION


def test_fetch_reports_a_missing_file_and_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(weights, "hf_hub_download", hub(tmp_path, {}))
    assert weights.main(["fetch"]) == 1
    assert "clicker-models fetch" in capsys.readouterr().err


HEAVY = {"torch", "torchvision", "transformers", "rapidocr", "onnxruntime", "cv2"}


def test_no_parse_module_imports_a_model_library_when_it_loads():
    """An Intel Mac has none of these, and must still import jev and run its tests."""
    package = Path(weights.__file__).parent
    for source in package.glob("*.py"):
        for node in ast.iter_child_nodes(ast.parse(source.read_text(encoding="utf-8"))):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            assert not {n.split(".")[0] for n in names} & HEAVY, f"{source.name} imports {names} at module level"
