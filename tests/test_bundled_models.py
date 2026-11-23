"""A frozen jev.exe reads the models placed beside it, and does not download them."""

import importlib.util
from pathlib import Path

_ENTRY = Path(__file__).resolve().parents[1] / "packaging" / "windows" / "jev_entry.py"
_spec = importlib.util.spec_from_file_location("jev_entry", _ENTRY)
assert _spec is not None and _spec.loader is not None
entry = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(entry)


def test_capture_proc_arguments_drop_the_dispatch_word():
    assert entry.capture_arguments(["jev.exe", "capture-proc", "shot.png", "chrome"]) == [
        "jev.exe",
        "shot.png",
        "chrome",
    ]
    assert entry.capture_arguments(["jev.exe", "status"]) is None


def test_models_beside_the_exe_become_the_offline_cache(tmp_path):
    exe = tmp_path / "jev.exe"
    exe.write_bytes(b"MZ")
    (tmp_path / "models").mkdir()
    env: dict[str, str] = {}
    assert entry.use_bundled_models(exe, env) is True
    assert env["HF_HUB_CACHE"] == str((tmp_path / "models").resolve())
    assert env["HF_HUB_OFFLINE"] == "1"


def test_no_models_folder_leaves_the_environment_alone(tmp_path):
    exe = tmp_path / "jev.exe"
    exe.write_bytes(b"MZ")
    env = {"HF_HUB_CACHE": "kept"}
    assert entry.use_bundled_models(exe, env) is False
    assert env == {"HF_HUB_CACHE": "kept"}


def test_an_existing_cache_setting_is_kept(tmp_path):
    exe = tmp_path / "jev.exe"
    exe.write_bytes(b"MZ")
    (tmp_path / "models").mkdir()
    env = {"HF_HUB_CACHE": "kept"}
    assert entry.use_bundled_models(exe, env) is True
    assert env["HF_HUB_CACHE"] == "kept"
    assert env["HF_HUB_OFFLINE"] == "1"
