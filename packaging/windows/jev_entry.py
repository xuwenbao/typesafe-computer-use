"""PyInstaller's entry script for jev.exe.

`huggingface_hub` reads `HF_HUB_CACHE` and `HF_HUB_OFFLINE` when it is imported, so a frozen
program points them at the models beside this exe before `main` imports anything that loads the
hub. The first tool call is what imports it. A checkout never comes through this script.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from typesafe_computer_use.helper.cli import main


def use_bundled_models(exe: Path, environ: dict[str, str] | None = None) -> bool:
    """Point the hub cache at `models` beside `exe` and stay offline, when that folder is there.

    An `HF_HUB_CACHE` that is already set is left alone. Returns whether the folder was there.
    """
    env = os.environ if environ is None else environ
    models = Path(exe).resolve().parent / "models"
    if not models.is_dir():
        return False
    env.setdefault("HF_HUB_CACHE", str(models))
    env["HF_HUB_OFFLINE"] = "1"
    return True


if getattr(sys, "frozen", False):
    use_bundled_models(Path(sys.executable))


if __name__ == "__main__":
    main()
