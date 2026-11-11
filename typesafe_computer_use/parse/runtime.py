"""Where the perception's models run, one at a time.

`INFERENCE` is the one lock every model call takes: OCR, detection, and captioning. A read made
ahead of a step runs on a thread of its own, and two models at once would only fight over the
same cores. `unsupported()` says why this machine cannot run the models, before a run starts.
"""

from __future__ import annotations

import functools
import importlib.util
import os
import platform
import sys
import threading

INFERENCE = threading.Lock()
TORCH_THREADS = max(1, (os.cpu_count() or 2) // 2)  # leave cores for the capture and the accessibility walk


def unsupported() -> str | None:
    """Why the desktop perception cannot run here, or None when it can."""
    if sys.platform == "darwin" and platform.machine() == "x86_64":
        return (
            "the desktop perception needs PyTorch, which ships no build for an Intel Mac; "
            "use a Mac with Apple silicon, Windows, or Linux (the browser path, clicker-bench, still runs here)"
        )
    missing = [name for name in ("torch", "torchvision", "transformers", "rapidocr") if importlib.util.find_spec(name) is None]
    if missing:
        return f"the desktop perception needs {', '.join(missing)}, which this environment lacks; run `uv sync`"
    return None


@functools.cache
def torch_device(allow_mps: bool = True) -> str:
    """CUDA, then Apple's GPU when the model runs there, then the CPU. Sets the CPU thread count once."""
    import torch

    torch.set_num_threads(TORCH_THREADS)
    if torch.cuda.is_available():
        return "cuda"
    if allow_mps and torch.backends.mps.is_available():
        return "mps"
    return "cpu"
