"""Command-line entry points: `clicker` and `clicker-inspect`."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import config
from .actions import Context
from .llm.writer import Writer
from .parse.runtime import unsupported
from .perception.pipeline import capture, mark, perceive, start_warm_up
from .platforms.adapter import desktop
from .report import annotate, ax_count, icon_count, render_payload
from .runner import RunConfig, run
from .timing import format_timing

DOTENV = Path.cwd() / ".env"


def _require_perception() -> None:
    reason = unsupported()
    if reason:
        sys.exit(reason)


def _prepare() -> None:
    if not config.settings().typesafe_api_key:
        sys.exit("TYPESAFE_API_KEY is not set (export it or put it in .env)")


def child_argv(goal: str, passed: list[str]) -> list[str]:
    """The child's arguments: the goal from the pipe, then the flags the launch command added."""
    return [goal, *passed]


def ask_user(question: str) -> str:
    """Read the user's reply to a question the run has just printed. An empty reply declines to answer.

    The bell is for a user who is watching the browser, not this terminal.
    """
    try:
        return input("\a  > ")
    except EOFError:
        return ""


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] in (["mcp"], ["status"], ["setup"]):
        from .helper.cli import main as helper_main

        helper_main(args)
        return
    # 隔离会话只在 Windows 上导入。其它系统在导入之前就退出，避免把那条路径带进来。
    if args[:1] == ["session"]:
        if sys.platform != "win32":
            sys.exit("clicker session only runs on Windows")
        config.load_dotenv(DOTENV)
        from .platforms.windows_session import run_controller

        run_controller(args[1:])
        return
    # 子会话里的 jev 若仍在控制台会话，必须在截图和输入之前退出。提问走管道。
    session_ask = None
    session_done = None
    if os.environ.get("JEV_SESSION") == "1":
        from .platforms.windows_session import child_startup, end_child, refuse_if_console

        refuse_if_console()
        goal, session_ask, session_done = child_startup()
        extra = list(sys.argv[1:] if argv is None else argv)
        argv = child_argv(goal, extra)
    mark("main")
    config.load_dotenv(DOTENV)
    try:
        options = config.settings()
    except ValueError as e:
        sys.exit(str(e))
    parser = argparse.ArgumentParser(
        prog="clicker",
        description="Drive this computer toward a goal: screen OCR, a TypeSafe classifier, deterministic actions.",
    )
    parser.add_argument("goal", help="what you want done on this computer")
    parser.add_argument("--act", action="store_true", help="actually click and type (default: dry run, one step)")
    parser.add_argument("--steps", type=int, default=options.steps, help="max actions before stopping")
    parser.add_argument("--min-confidence", type=float, default=options.min_confidence, help="stop below this confidence")
    parser.add_argument("--delay", type=float, default=options.delay, help="seconds to wait after each action")
    parser.add_argument(
        "--handoffs",
        type=int,
        default=options.handoffs,
        help="times the writer may send a stopped run back to the classifier with a new focus (0: every stop is final)",
    )
    parser.add_argument("--out", type=Path, default=Path("runs") / time.strftime("%Y%m%d-%H%M%S"), help="run folder")
    parser.add_argument("--image", type=Path, help="replay a saved capture instead of the live screen (never acts)")
    parser.add_argument("--app", help="frontmost app to report during replay")
    parser.add_argument("--url", help="browser URL to report during replay")
    args = parser.parse_args(argv)

    _require_perception()
    _prepare()
    start_warm_up()
    if args.act and not desktop.accessibility_trusted():
        sys.exit("this terminal lacks Accessibility permission; grant it in System Settings > Privacy & Security")
    try:
        writer = Writer.open()
    except ValueError as e:
        sys.exit(str(e))
    if writer is None:
        print(
            "writer disabled: no OPENAI_API_KEY, or ANTHROPIC_API_KEY when CLICKER_WRITER_API=anthropic; type_text, writer-proposed URLs and the final answer need one"
        )
    else:
        print(f"writer: {Writer(writer).provider()}")

    cfg = RunConfig(
        goal=args.goal,
        out=args.out,
        act=args.act,
        steps=args.steps,
        min_confidence=args.min_confidence,
        delay=args.delay,
        handoffs=args.handoffs,
        driver_steps=options.driver_budget,
        image=args.image,
        app=args.app,
        url=args.url,
    )

    def ctx_factory(typesafe, history):
        return Context(
            goal=args.goal,
            browser=options.browser,
            email=options.email,
            typesafe=typesafe,
            writer=writer,
            history=history,
            ask=session_ask if session_ask is not None else (ask_user if sys.stdin.isatty() else None),
        )

    state = run(cfg, ctx_factory)
    if session_done is not None:
        end_child(session_done, state.outcome, str(args.out))
    if state.outcome.startswith("aborted"):
        sys.exit(130)


def inspect(argv: list[str] | None = None) -> None:
    config.load_dotenv(DOTENV)
    try:
        options = config.settings()
    except ValueError as e:
        sys.exit(str(e))
    parser = argparse.ArgumentParser(
        prog="clicker-inspect",
        description="Count down, capture the screen, and show exactly what the clicker would send to TypeSafe.",
    )
    parser.add_argument("goal", nargs="?", default="(no goal given)")
    parser.add_argument("--countdown", type=int, default=3)
    parser.add_argument("--no-open", action="store_true", help="write files without opening them")
    parser.add_argument("--out", type=Path, default=Path("inspections") / time.strftime("%Y%m%d-%H%M%S"))
    args = parser.parse_args(argv)
    _require_perception()
    args.out.mkdir(parents=True, exist_ok=True)

    for n in range(args.countdown, 0, -1):
        print(f"{n}...", end=" ", flush=True)
        time.sleep(1)
    print("capture")

    browser = options.browser
    timing: dict[str, float] = {}
    screen = capture(browser=browser, timing=timing)
    items = perceive(screen, options.max_options, args.goal, timing)
    annotated = args.out / "annotated.png"
    text = args.out / "state.txt"
    screen.image.save(args.out / "raw.png")
    annotate(screen, items, chosen="", out=annotated)
    text.write_text(render_payload(args.goal, screen, items, [], browser, options.email), encoding="utf-8")

    print(
        f"app={screen.app!r} url={screen.url!r} items={len(items)} ax={ax_count(items)} icons={icon_count(items)} "
        f"offscreen={len(screen.offscreen)} field={screen.field.role if screen.field else None}"
    )
    print(format_timing(timing))
    print(f"  {annotated}\n  {text}")
    if not args.no_open:
        desktop.open_path(annotated)
        desktop.open_path(text, as_text=True)
