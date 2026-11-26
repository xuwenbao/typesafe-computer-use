"""Which apps the helper may drive, and how: the gate every input goes through.

An app is named by its executable stem (`notepad`), or for a known browser by the product name the
Windows adapter reports (`Google Chrome` is `chrome`). Two rules, in order, and no questions:

- Never: terminals, shells, the helper itself, the agent that asked (its process and its parents),
  the lock and sign-in screens, UAC, and Settings. Nothing the caller says lifts this.
- Click only: IDEs and code editors may be clicked and scrolled, never typed into.

Everything else is driven without asking. The helper is a tool: it does not put a choice to the
user, and it does not ask the agent to ask one.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..models import Abort

# The same names as windows.BROWSER_EXES; tests/test_helper_approvals.py holds the two together.
BROWSER_EXES = {"Google Chrome": "chrome", "Microsoft Edge": "msedge", "Firefox": "firefox", "Brave Browser": "brave"}

NEVER = frozenset(
    {
        # terminals and shells: a typed line there runs as the user
        "cmd", "powershell", "pwsh", "windowsterminal", "wt", "openconsole", "conhost", "wsl", "wslhost",
        "bash", "mintty", "alacritty", "wezterm-gui", "hyper", "tabby", "putty",
        # the system's own guards and controls
        "consent", "lockapp", "logonui", "credentialuibroker", "systemsettings", "taskmgr", "regedit", "mmc",
        # this program, including the names of the earlier split builds
        "clicker", "jev", "jev-helper", "clicker-mcp",
    }
)  # fmt: skip

CLICK_ONLY = frozenset(
    {
        "code", "cursor", "windsurf", "zed", "devenv", "sublime_text",
        "idea64", "pycharm64", "rider64", "webstorm64", "clion64", "goland64",
    }
)  # fmt: skip

# Ancestors of the front that are the desktop itself rather than the agent that started it.
SYSTEM_ANCESTORS = frozenset({"explorer", "svchost", "services", "wininit", "winlogon", "userinit", "system", "smss", "csrss"})


class Denied(Abort):
    """An input the gate refused. An `Abort`, so a run that meets one stops before acting."""


class Gate:
    """One front's hard rules for the apps it drives. `check` returns, or raises `Denied`."""

    @staticmethod
    def app_key(app: str) -> str:
        """The one name an app goes by here: `Notepad.EXE`, `notepad` and `Google Chrome`'s `chrome` alike."""
        name = app.strip()
        exe = next((exe for product, exe in BROWSER_EXES.items() if product.lower() == name.lower()), None)
        return exe if exe is not None else name.lower().removesuffix(".exe")

    @staticmethod
    def host_apps(ancestors: Iterable[str]) -> frozenset[str]:
        """The agent that started the front: every ancestor process but the desktop's own."""
        return frozenset(key for key in map(Gate.app_key, ancestors) if key and key not in SYSTEM_ANCESTORS)

    def __init__(self, host: frozenset[str] = frozenset()) -> None:
        self.host = host

    def check(self, app: str, typing: bool = False) -> None:
        key = Gate.app_key(app)
        if not key:
            raise Denied("no app is in front, so there is nothing to drive")
        if key in NEVER or key in self.host:
            raise Denied(f"{app} is never driven by jev")
        if typing and key in CLICK_ONLY:
            raise Denied(f"{app} may only be clicked and scrolled, not typed into")
