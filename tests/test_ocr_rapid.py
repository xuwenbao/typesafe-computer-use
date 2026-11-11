"""How `osworld.ocr` loads the RapidOCR backend with and without RapidOCR installed. The conversion
of RapidOCR's output is tested with `parse.ocr`, in test_parse_ocr.py.

No test loads the models. Where the extra is not installed, `ocr_rapid` is imported against a
stand-in RapidOCR that refuses to build an engine, and the import is undone afterward, so the rest
of the suite sees the machine as it is.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import types

import pytest

import typesafe_computer_use
from typesafe_computer_use.osworld import ocr

MODULE = "typesafe_computer_use.ocr_rapid"
HAS_EXTRA = importlib.util.find_spec("rapidocr") is not None
INSTALL = "install it with: uv sync --extra rapidocr (or pip install 'typesafe-computer-use[rapidocr]')"


class _NoEngine:
    def __init__(self, *args: object, **kwargs: object) -> None:
        raise RuntimeError("the rapidocr extra is not installed; tests must not build an engine")


@pytest.fixture(scope="module")
def rapid():
    """`ocr_rapid`, for its pure functions."""
    if HAS_EXTRA:
        yield importlib.import_module(MODULE)
        return
    stand_in = types.ModuleType("rapidocr")
    stand_in.RapidOCR = _NoEngine
    sys.modules["rapidocr"] = stand_in
    try:
        yield importlib.import_module(MODULE)
    finally:
        sys.modules.pop("rapidocr", None)
        sys.modules.pop(MODULE, None)
        if hasattr(typesafe_computer_use, "ocr_rapid"):
            delattr(typesafe_computer_use, "ocr_rapid")


# ----- loading the backend --------------------------------------------------------------------


def test_rapidocr_is_a_known_backend_and_an_unknown_name_still_fails():
    assert set(ocr.backends()) >= {"vision", "rapidocr"}
    with pytest.raises(ValueError, match=r"unknown OCR backend 'nope'; known backends: rapidocr, vision"):
        ocr.backend("nope")


def test_without_the_extra_rapidocr_is_unavailable_and_says_how_to_install_it(monkeypatch):
    monkeypatch.setitem(sys.modules, "rapidocr", None)  # an import of it raises ImportError
    monkeypatch.delitem(sys.modules, MODULE, raising=False)
    monkeypatch.delattr(typesafe_computer_use, "ocr_rapid", raising=False)
    with pytest.raises(ocr.Unavailable, match="the rapidocr backend cannot load") as raised:
        ocr.backend("rapidocr")
    assert INSTALL in str(raised.value)
    assert isinstance(raised.value.__cause__, ImportError)


def test_with_the_module_importable_rapidocr_is_its_recognizer(monkeypatch):
    fake = types.ModuleType(MODULE)
    fake.recognize_text = lambda image: [("fake", 1.0, (0.0, 0.0, 1.0, 1.0))]
    monkeypatch.setitem(sys.modules, MODULE, fake)
    monkeypatch.setattr(typesafe_computer_use, "ocr_rapid", fake, raising=False)
    assert ocr.backend("rapidocr") is fake.recognize_text


@pytest.mark.skipif(not HAS_EXTRA, reason="needs the rapidocr extra")
def test_with_the_extra_rapidocr_loads_without_loading_the_models():
    rapid = importlib.import_module(MODULE)
    assert ocr.backend("rapidocr") is rapid.recognize_text
    assert rapid._engine.cache_info().currsize == 0
