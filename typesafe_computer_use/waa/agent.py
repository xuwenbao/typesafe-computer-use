"""jev as a WAA agent: `reset()` and `predict(instruction, obs)`, with jev's loop unchanged inside.

WAA's runner calls `predict` once per step and runs the actions it returns. `JevAgent` runs
`runner.run` on a worker thread, inside `platform_adapter.using(WAADesktop(...))`, and the adapter
turns the loop's reads and inputs into WAA steps. `predict` returns the four values WAA asks for;
the log is an empty dict and the computer-update is always empty, since jev does not use them.

The worker never blocks forever. Once it has handed out `max_steps` action lists, the next wait
raises `Abort("WAA step limit")`, and `reset()` makes a stale worker's wait raise `Abort("reset")`.
`max_steps` counts lists handed to the VM. A decision that hands nothing, a refusal or a stop for
review, spends one of the loop's own steps, which run to the usual limit. Either way the loop
records the outcome. When it ends, `predict` answers `DONE`, never `FAIL`.
"""

from __future__ import annotations

import json
import os
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import requests

from .. import config, runner
from ..actions import Context
from ..decide import Decision
from ..models import Abort
from ..parse.runtime import unsupported
from ..platform_adapter import using
from ..runner import RunConfig
from ..writer import Writer, make_writer
from . import tree
from .desktop import WAADesktop

RUN_FOLDER = "jev"
STEP_SECONDS = 600.0
RESET_JOIN_SECONDS = 5.0
DONE = "DONE"
COMMIT = "WAA_COMMIT"
DIGEST = "WAA_IMAGE_DIGEST"


def browser_for(domain: str) -> str:
    """The browser a task domain opens. Edge tasks use Edge; every other domain uses Chrome."""
    return "Microsoft Edge" if domain == "msedge" else "Google Chrome"


class _FromEnv:
    def __repr__(self) -> str:
        return "FROM_ENV"


FROM_ENV = _FromEnv()


def run_on(controller) -> tree.Run:
    """Post a script to the VM's `/execute` as `python -c`, with no pyautogui prefix.

    The prefix would land in front of the script, and the script's `__future__` import has to be
    its first line. The return is the process's exit code and its stdout.
    """

    def run(script: str, timeout: float) -> tuple[int, str]:
        response = requests.post(
            controller.http_server + "/execute",
            json={"command": ["python", "-c", script], "shell": False},
            timeout=timeout,
        )
        try:
            body = response.json()
        except ValueError:
            return response.status_code, response.text
        code = body.get("returncode", 0 if response.ok else response.status_code)
        return int(code), body.get("output") or body.get("message") or body.get("error") or ""

    return run


class JevAgent:
    """jev behind WAA's agent interface.

    The screen is read by the desktop perception. A machine that cannot run its models fails here,
    before any VM starts. `browser` is the one this task opens. `controller` returns WAA's
    controller of the VM, and with it jev fetches each observation's tree itself.
    """

    def __init__(
        self,
        max_steps: int = 15,
        out_root=None,
        *,
        browser: str = "Google Chrome",
        writer: Writer | _FromEnv | None = FROM_ENV,
        step_seconds: float = STEP_SECONDS,
        controller: Callable[[], object] | None = None,
    ) -> None:
        if max_steps < 1:
            raise ValueError(f"max_steps must be at least 1, not {max_steps}")
        reason = unsupported()
        if reason is not None:
            raise RuntimeError(reason)
        self.max_steps = max_steps
        self.out_root = Path(out_root) if out_root is not None else Path("jev-runs")
        self.browser = browser
        if isinstance(writer, _FromEnv):
            writer = make_writer()
            config.writer_vision()
        self.writer = writer
        self.email = config.email()
        self.step_seconds = step_seconds
        self.controller = controller
        self._run: _Run | None = None
        self._stale: threading.Thread | None = None

    def reset(self, runtime_logger=None, vm_ip=None) -> None:
        """Stop the previous task's worker, if any, and start the next task clean."""
        if self._run is not None:
            thread = self._run.stop(RESET_JOIN_SECONDS)
            self._stale = thread if thread is not None and thread.is_alive() else None
            self._run = None

    def predict(self, instruction: str, obs: dict) -> tuple[str, list[str], dict, None]:
        """One WAA step. The log is an empty dict and the computer update is empty."""
        if self._run is None:
            self._run = _Run(self, instruction, self._stale)
            self._stale = None
        response, actions = self._run.predict(obs)
        return response, actions, {}, None

    @property
    def worker(self) -> threading.Thread | None:
        """The current task's worker thread, once `predict` started it."""
        return self._run.thread if self._run is not None else None


@dataclass(frozen=True)
class _Step:
    response: str
    actions: list[str]
    last: bool = False


@dataclass(frozen=True)
class _Failed:
    error: BaseException


_STOP = object()


class _Run:
    """One task: the worker running jev's loop, and the two queues between it and `predict`."""

    def __init__(self, agent: JevAgent, instruction: str, after: threading.Thread | None) -> None:
        self.agent = agent
        self.cfg = RunConfig(
            goal=instruction,
            out=Path(agent.out_root) / RUN_FOLDER,
            act=True,
            steps=config.DEFAULT_STEPS,
            delay=0,
            read_ahead=True,
        )
        self.after = after
        self.to_worker: queue.Queue = queue.Queue()
        self.to_predict: queue.Queue = queue.Queue()
        self.thread: threading.Thread | None = None
        self.handed = 0
        self.final: list[str] = []
        self.decision = ""
        self.ended: str | None = None
        self.stopped = False
        self.adapter: WAADesktop | None = None

    def predict(self, obs: dict) -> tuple[str, list[str]]:
        if self.ended is not None:
            return self.ended, [DONE]
        if self.thread is None:
            self.thread = threading.Thread(target=self._work, args=(obs,), name="jev-waa", daemon=True)
            self.thread.start()
        else:
            self.to_worker.put(obs)
        try:
            message = self.to_predict.get(timeout=self.agent.step_seconds)
        except queue.Empty:
            self.ended = f"jev took longer than {self.agent.step_seconds:.0f}s over one step"
            self.stop(0)
            raise TimeoutError(self.ended) from None
        if isinstance(message, _Failed):
            self.ended = f"jev failed: {message.error!r}"
            raise message.error
        if message.last:
            self.ended = message.response
        return message.response, message.actions

    def stop(self, join_seconds: float) -> threading.Thread | None:
        """End the run. A worker waiting for an observation raises `Abort("reset")`."""
        self.stopped = True
        self.to_worker.put(_STOP)
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(join_seconds)
        return self.thread

    def _work(self, obs: dict) -> None:
        message: _Step | _Failed
        try:
            if self.after is not None:
                self.after.join()
            start = None
            if self.agent.controller is not None:
                controller = self.agent.controller

                def start() -> tree.Pending:
                    return tree.Pending(run_on(controller()))

            adapter = self.adapter = WAADesktop(obs, self._next_obs, start_tree=start, save_tree=self.cfg.out)
            with using(adapter):
                state = runner.run(self.cfg, self._context)
            answer = f"; answer: {state.answer.text}" if state.answer is not None else ""
            ended = f"jev ended: {state.outcome}{answer}"
            final = [*self.final, *adapter.actions]
            response = f"{self.decision}; {ended}" if final else ended
            message = _Step(response, [*final, DONE], last=True)
        except BaseException as error:
            message = _Failed(error)
        try:
            self._record()
        except Exception as error:
            message = message if isinstance(message, _Failed) else _Failed(error)
        self.to_predict.put(message)

    def _context(self, typesafe, history: list[str]) -> Context:
        return Context(
            goal=self.cfg.goal,
            browser=self.agent.browser,
            email=self.agent.email,
            typesafe=_Watching(typesafe, self._decided),
            writer=self.agent.writer,
            history=history,
            ask=None,
        )

    def _decided(self, line: str) -> None:
        self.decision = line

    def _next_obs(self, actions: list[str]) -> dict:
        """Hand one step's actions to `predict` and wait for the observation after them."""
        if self.stopped:
            raise Abort("reset")
        self.handed += 1
        if self.handed >= self.agent.max_steps:
            self.final = actions
            raise Abort("WAA step limit")
        self.to_predict.put(_Step(self.decision, actions))
        obs = self.to_worker.get()
        if obs is _STOP:
            raise Abort("reset")
        return obs

    def _record(self) -> None:
        """Add what the run ran on to jev's run.json."""
        path = self.cfg.out / "run.json"
        if not path.is_file():
            return
        summary = json.loads(path.read_text(encoding="utf-8"))
        fetches = list(self.adapter.fetches) if self.adapter is not None else []
        summary["waa"] = {
            "commit": os.environ.get(COMMIT),
            "image_digest": os.environ.get(DIGEST),
            "browser": self.agent.browser,
            "tree": "jev-uia",
            **tree.summary(fetches),
        }
        path.write_text(json.dumps(summary, indent=2), encoding="utf-8")


class _Watching:
    """The classifier, passed through, telling the run each decision it makes."""

    def __init__(self, client, decided: Callable[[str], None]) -> None:
        self._client = client
        self._decided = decided

    def system_one(self, **request):
        reply = self._client.system_one(**request)
        answers = getattr(reply, "answers", None) or {}
        if "kind" in answers:
            decision = Decision(
                kind=answers["kind"], item=answers.get("item"), site=answers.get("site"), offscreen=answers.get("offscreen")
            )
            self._decided(describe(decision, request.get("state") or {}))
        return reply


def describe(decision: Decision, state: dict) -> str:
    """A decision as one line: the action, what it acts on, and its confidence."""
    kind = decision.kind.choice
    target = ""
    if decision.clicking:
        items = {str(it.get("i")): it.get("text") for it in state.get("screen_items_in_reading_order", [])}
        target = f" {items.get(decision.item.choice, decision.item.choice)!r}"
    elif decision.pressing_offscreen:
        controls = {str(c.get("k")): c.get("label") for c in state.get("offscreen_controls", [])}
        target = f" {controls.get(decision.offscreen.choice, decision.offscreen.choice)!r}"
    elif kind == "use_browser" and decision.site is not None:
        target = f" {decision.site.choice}"
    return f"{kind}{target} ({decision.confidence:.2f})"
