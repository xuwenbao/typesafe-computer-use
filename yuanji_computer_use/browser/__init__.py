"""Browser backend: DOM-driven computer use, no pixels, no OCR."""

from .cdp import CDPError, Chrome, Session
from .decide import BROWSER_ACTIONS, Decider, Decision
from .perceive import Dom, Element, Page
from .runner import GoalRunner, RunResult, Step

__all__ = [
    "BROWSER_ACTIONS",
    "CDPError",
    "Chrome",
    "Decider",
    "Decision",
    "Dom",
    "Element",
    "GoalRunner",
    "Page",
    "RunResult",
    "Session",
    "Step",
]
