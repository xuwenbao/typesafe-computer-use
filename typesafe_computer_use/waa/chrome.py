"""Rewrite a WAA task's Chrome launch so the debugging port actually listens.

Chrome 136 and later ignore --remote-debugging-port on the default profile, and a Chrome left
from the previous task accepts the new launch instead of starting one. The launch endpoint
returns as soon as the process is spawned, so a kill on its own can still be running when
Chrome starts and then kill that Chrome. A short sleep sits between the two.
"""

from __future__ import annotations

CHROME_USER_DATA = r"C:\Users\Docker\AppData\Local\jev-chrome-cdp"
_CDP_STEPS = frozenset({"chrome_open_tabs", "chrome_close_tabs"})
_CHROME = frozenset({"google-chrome", "chrome"})


def chrome_cdp_config(config: list) -> list:
    """The task config, with each CDP Chrome launch killed and restarted on a private profile.

    A launch that only opens Chrome, with no step that connects over CDP, is returned unchanged.
    A profile task launches Chrome the same way and has to keep the default profile.
    """
    if not isinstance(config, list):
        return config
    if not any(isinstance(step, dict) and step.get("type") in _CDP_STEPS for step in config):
        return config
    rewritten: list = []
    for step in config:
        command = _debug_command(step)
        if command is None:
            rewritten.append(step)
            continue
        rewritten.append({"type": "launch", "parameters": {"command": ["taskkill", "/F", "/IM", "chrome.exe"]}})
        rewritten.append({"type": "sleep", "parameters": {"seconds": 1}})
        rewritten.append({"type": "launch", "parameters": {"command": command}})
    return rewritten


def install_chrome_setup() -> None:
    """Open the task's tabs without closing Chrome's first one.

    Closing that tab deadlocks the debugging socket: the client and Chrome both wait, and the
    task never starts. The first tab stays. The pages the task named are still opened.
    """
    from desktop_env.controllers.setup import SetupController

    SetupController._chrome_open_tabs_setup = _open_chrome_tabs


def _open_chrome_tabs(self, urls_to_open: list[str]):
    """The upstream opener, minus the close of the first tab."""
    import logging
    import time

    from playwright.sync_api import sync_playwright

    logger = logging.getLogger("desktopenv.setup")
    remote_debugging_url = f"http://{self.vm_ip}:9222"
    logger.info("Connect to Chrome @: %s", remote_debugging_url)
    for attempt in range(15):
        if attempt > 0:
            time.sleep(5)
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.connect_over_cdp(remote_debugging_url)
            except Exception as error:
                if attempt < 14:
                    logger.error("Attempt %s: Failed to connect, retrying. Error: %s", attempt + 1, error)
                    continue
                logger.error("Failed to connect after multiple attempts: %s", error)
                raise
            logger.info("Opening %s...", urls_to_open)
            context = browser.contexts[0]
            for index, url in enumerate(urls_to_open):
                page = context.new_page()
                try:
                    page.goto(url, timeout=60000)
                except Exception:
                    logger.warning("Opening %s exceeds time limit", url)
                logger.info("Opened tab %s: %s", index + 1, url)
            return browser, context


def _debug_command(step: object) -> list[str] | None:
    """The Chrome command plus a private profile, or None when this step is not that launch."""
    if not isinstance(step, dict) or step.get("type") != "launch":
        return None
    command = (step.get("parameters") or {}).get("command")
    if not isinstance(command, list) or not command or command[0] not in _CHROME:
        return None
    if not any(isinstance(part, str) and part.startswith("--remote-debugging-port") for part in command):
        return None
    flags = [str(part) for part in command]
    if not any(part.startswith("--user-data-dir") for part in flags):
        flags.append(f"--user-data-dir={CHROME_USER_DATA}")
    return flags
