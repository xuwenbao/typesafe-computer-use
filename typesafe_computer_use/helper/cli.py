"""`jev`: one program. An agent runs `jev mcp`, and that process drives the screen.

`mcp` imports the front only. The front imports the perception on the first tool call, so the
MCP handshake stays light. `setup` imports only the guide that writes an agent's MCP config.
"""

from __future__ import annotations

import argparse
import sys


def serve_mcp() -> None:
    from .front import serve

    serve()


def show_status() -> None:
    from .front import status

    sys.exit(status())


def run_setup(argv: list[str]) -> None:
    from .setup import main as setup_main

    sys.exit(setup_main(argv))


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "setup":
        run_setup(argv[1:])
        return
    parser = argparse.ArgumentParser(prog="jev", description="jev's MCP server. It drives this computer's screen.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("mcp", help="serve the MCP tools on stdio and drive the screen")
    sub.add_parser("status", help="this program's version, and whether its models are installed")
    sub.add_parser("setup", help="show or write how an agent starts jev")
    args = parser.parse_args(argv)
    if args.command == "mcp":
        serve_mcp()
    elif args.command == "status":
        show_status()


if __name__ == "__main__":
    main()
