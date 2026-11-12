import importlib.util

import pytest

from typesafe_computer_use.parse import caption, detect, ocr, runtime


def test_an_intel_mac_is_unsupported_and_says_where_jev_runs(monkeypatch):
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setattr(runtime.platform, "machine", lambda: "x86_64")
    reason = runtime.unsupported()
    assert "Intel Mac" in reason and "Apple silicon" in reason


def test_a_missing_model_library_is_named(monkeypatch):
    monkeypatch.setattr(runtime.sys, "platform", "linux")
    monkeypatch.setattr(runtime.platform, "machine", lambda: "x86_64")
    real = importlib.util.find_spec
    monkeypatch.setattr(runtime.importlib.util, "find_spec", lambda name: None if name == "torch" else real(name))
    assert "needs torch" in runtime.unsupported()


def test_a_machine_with_everything_is_supported(monkeypatch):
    monkeypatch.setattr(runtime.sys, "platform", "linux")
    monkeypatch.setattr(runtime.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(runtime.importlib.util, "find_spec", lambda name: object())
    assert runtime.unsupported() is None


@pytest.mark.parametrize(
    "load, what",
    [
        (lambda: detect.load_model(), "the icon detector"),
        (lambda: caption.load_model(), "the icon captioner"),
        (lambda: ocr._engine(), "the OCR engine"),
    ],
)
def test_no_test_loads_a_real_model(load, what):
    with pytest.raises(RuntimeError, match=f"a test loaded {what}"):
        load()
