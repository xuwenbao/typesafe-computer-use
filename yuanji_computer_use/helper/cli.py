"""`clicker`: one program. An agent runs `clicker mcp`, and that process drives the screen.

`mcp` imports the front only. The front imports the perception on the first tool call, so the
MCP handshake stays light. `setup` imports only the guide that writes an agent's MCP config.
"""

from __future__ import annotations

import argparse
import sys


class Commands:
    """The clicker commands: mcp, status, and setup."""

    @staticmethod
    def serve_mcp() -> None:
        from .front import Front

        Front.serve()

    @staticmethod
    def show_status() -> None:
        from .front import Front

        sys.exit(Front.status())

    @staticmethod
    def run_setup(argv: list[str]) -> None:
        from .setup import Guide

        sys.exit(Guide.main(argv))

    @staticmethod
    def main(argv: list[str] | None = None) -> None:
        argv = list(sys.argv[1:] if argv is None else argv)
        if argv and argv[0] == "setup":
            Commands.run_setup(argv[1:])
            return
        parser = argparse.ArgumentParser(prog="clicker", description="clicker's MCP server. It drives this computer's screen.")
        sub = parser.add_subparsers(dest="command", required=True)
        sub.add_parser("mcp", help="serve the MCP tools on stdio and drive the screen")
        sub.add_parser("status", help="this program's version, and whether its models are installed")
        sub.add_parser("setup", help="show or write how an agent starts jev")
        args = parser.parse_args(argv)
        if args.command == "mcp":
            Commands.serve_mcp()
        elif args.command == "status":
            Commands.show_status()


def main(argv: list[str] | None = None) -> None:
    return Commands.main(argv)


if __name__ == "__main__":
    main()
