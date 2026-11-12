# Agent rules

Read [VISION.md](VISION.md) first: what jev is for, in a dozen lines. Weigh every design choice
against it.

This tool drives a real computer. When you work on it, you are almost always on the
maintainer's own Mac, often while they are using it.

## Never take over the machine without explicit approval

The rule is about this machine's screen, input, and apps, not about running commands. Ask first,
every time, before anything that would use or take them over:

- running the project on this machine: `clicker`, `clicker-inspect`, or any script that imports
  the platform adapter and calls it
- anything that moves the mouse, presses keys, clicks, scrolls, or types
- AppleScript or `osascript`, `open`, or any command that launches, activates, or quits an app
  or opens a URL or file
- launching Chrome or any browser, and anything that talks to a browser over CDP
- screen capture of any kind
- any Docker container other than the sandbox, and requests to local model servers (Ollama,
  LM Studio) or any other local service

Propose the exact command and wait. Approval covers that command once, not the kind of command
from then on. If you run subagents, give them this rule word for word.

Everything else needs no approval: `git`, `uv`, ruff, the offline tests, `scripts/sandbox` (its
own computer, which never touches this machine's screen, input, or apps), and ordinary CLI
commands such as `gh` or read-only `gcloud` queries. Commands that create or delete cloud
resources, or spend money, still get a yes first.

## WindowsAgentArena

`scripts/waa setup` fetches WindowsAgentArena and builds the client image. The import check it
runs is inside that container, so it needs approval, once, for that command. `scripts/waa results`
reads result files and needs none. `scripts/waa prepare-image` and `scripts/waa run` each start
the container, and each command needs approval of its own. A WSL2 machine with KVM is a machine
this runs on.

## Tests stay off the machine

`tests/conftest.py` makes every call that would reach the machine refuse during tests: input
events, AppleScript, screen capture, `open`, and accessibility actions. The pointer reads as
mid-screen. Do not weaken or bypass that guard. A test that needs one of those calls patches it
itself. When you add a new call that reaches the machine, add it to the guard in the same change.

## Everything else

See `CONTRIBUTING.md` for how the project works and what a pull request needs.
