"""Where the helper keeps its files, independent of the directory anyone started it from.

`JEV_HOME` wins; otherwise `%LOCALAPPDATA%\\jev` on Windows, `~/Library/Application Support/jev`
on macOS, and `~/.local/share/jev` elsewhere.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


class Home:
    """Where jev keeps its config, runs, and state."""

    @staticmethod
    def root() -> Path:
        override = os.environ.get("JEV_HOME", "").strip()
        if override:
            return Path(override)
        if sys.platform == "win32":
            local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
            return Path(local) / "jev"
        if sys.platform == "darwin":
            return Path.home() / "Library" / "Application Support" / "jev"
        return Path.home() / ".local" / "share" / "jev"

    @staticmethod
    def env_file() -> Path:
        return Home.root() / ".env"

    @staticmethod
    def runs_dir() -> Path:
        return Home.root() / "runs"

    @staticmethod
    def state_dir() -> Path:
        return Home.root() / "state"
