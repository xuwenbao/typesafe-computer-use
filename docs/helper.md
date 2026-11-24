# jev as MCP tools (Windows)

Other agents (Claude Code, Cursor, Codex, any MCP client) can hand jev a goal, or drive the screen
through it step by step, over [MCP](https://modelcontextprotocol.io). The agent starts one
process, and that process does the work:

```
agent ──stdio MCP──▶ clicker mcp ──▶ screen, mouse, keyboard
                     (this process: the tools, the perception,
                      the loop, the gate)
```

Two agents can each start `clicker mcp`. A named mutex, `Local\jev-desk-<your SID>`, lets only one of
them move the mouse at a time. The one that does not get it is told `busy` and sends no input.
Windows releases the mutex if the holder crashes.

The MCP handshake stays light: it imports the front and FastMCP, and nothing of PyTorch. The first
tool call loads the perception and the desktop adapter, reads `%LOCALAPPDATA%\jev\.env`, and sends
later prints to `state\jev.log`, so they never mix with the protocol on stdout.

jev is a tool. It does not ask the user a question, and it does not send a request back to the
agent. A refused app comes back as an error.

Windows only for now. Like the rest of the Windows adapter, it is untested on a real machine.

## Install

`scripts\build-helper.ps1` builds `clicker-setup-<version>.exe` (Inno Setup, per user, no admin) and
writes `clicker-setup-<version>.sha256` beside it. The installer includes `clicker.exe` and the perception's
model files. An agent can install it from those local files: [paste this prompt](agent-install.md).
Run the installer once. It installs to `%LOCALAPPDATA%\Programs\clicker\`: `clicker.exe`, `_internal\`,
`models\`, `skills\jev\`, `agent-prompt.md`, `如何接入智能体.txt`, and `version.txt`. A manual
install leaves "打开接入说明" checked on the last page; finishing opens that text file in Notepad,
with three steps and then the prompt to paste into the agent ([the same prompt](agent-start.md)).
A silent install does not open Notepad. The finish page does not show the MCP config. The
installer still writes it to `mcp-config.json` in that folder. To wire the config by hand, paste it
into the agent. On Windows, write the absolute path: an agent packaged
as MSIX does not inherit `PATH`.

```json
{
  "mcpServers": {
    "jev": {
      "command": "C:\\Users\\<you>\\AppData\\Local\\Programs\\jev\\clicker.exe",
      "args": ["mcp"]
    }
  }
}
```

The first call reads the bundled models and does not download them. `clicker status` prints this
program's version, the path of the executable, the home directory, whether `models\` sits
beside the executable, and whether `.env` holds `TYPESAFE_API_KEY` and a writer model. It prints
names only, never a value.

WorkBuddy does not start a server from its `mcp.json` until the user trusts it: in WorkBuddy's
MCP manager, under 我的 MCP, find jev and click 信任. `clicker setup --host workbuddy --write` says so
on its `next:` line.

Run the installer again to upgrade. It stays in the same folder, so the MCP config does not
change. The installer closes any `clicker.exe` that still has the files open, which interrupts a call
in progress; the agent starts `clicker mcp` again afterwards. Uninstall from Apps & features does the
same. There is no `clicker stop`, and a process does not exit on its own after sitting idle: it lives
as long as the agent keeps the MCP session.

From a checkout, without a frozen build:

```json
{
  "mcpServers": {
    "jev": {
      "command": "uv",
      "args": ["run", "--directory", "C:\\path\\to\\jev", "jev", "mcp"]
    }
  }
}
```

`clicker mcp` reads its keys from `%LOCALAPPDATA%\jev\.env` (the same variables as the repo's `.env`;
`TYPESAFE_API_KEY` is required for `run_goal`) and writes its log to
`%LOCALAPPDATA%\jev\state\jev.log` and every run to `%LOCALAPPDATA%\jev\runs\`. `JEV_HOME` moves
all of it. A checkout has no bundled models: fetch them once with `uv run clicker-models fetch`,
or the first look downloads them and waits. The installed `clicker.exe` reads the `models` folder
beside it and does not download.

Each agent loads its own copy of the models. Restarting the MCP server loads them again.

## Tools

Every tool acts on the real machine; there is no dry run.

| tool | what it does |
|---|---|
| `run_goal` | hand jev a goal in plain English; its own loop drives to the end and returns the answer, every action, the run folder, and the last screen |
| `inspect_screen` | read the screen without acting: elements with numbers, roles, boxes in screen points, a `state_id`, and the marked screenshot |
| `get_app_state` | bring an app forward when given, then read as `inspect_screen` |
| `click`, `type_text`, `set_value`, `scroll` | act on an element of the latest state, or on a point |
| `press_key`, `activate`, `frontmost_app` | one key, bring an app forward, the app in front |

An element number holds only for the `state_id` it came with; any input makes that state stale,
and an element action on a stale state fails as `stale_state` without input. One call runs at a
time. A second call in the same process, or a call in another `clicker mcp` while this one holds the
desktop, fails as `busy` and is not queued. Failures come back as tool errors that start with
their kind: `forbidden`, `busy`, `stale_state`, `invalid_arguments`, `missed`, `aborted`,
`not_found`, `unsupported`, `not_configured`, `not_supported`.

## What is refused

Every input goes through one gate in this process, `run_goal`'s as much as the primitives'.
Nothing is asked:

- Never driven: terminals and shells, UAC, the lock and sign-in screens, Settings, Task Manager,
  jev itself, and the agent that asked (this process's parent processes). The call returns
  `forbidden`.
- Click only: IDEs and code editors may be clicked and scrolled, never typed into.
- Everything else is driven as soon as it is called.

A refusal inside `run_goal` ends the run before that action, and the returned `outcome` starts
with `aborted`. The writer is not given a way to ask the user; when it cannot continue it stops
and the result says why.

To stop a call mid-action, slam the mouse into the top-left corner, as with `clicker`. Cancelling
the call in the agent stops it at the next input. Closing the agent ends the process, and the
call with it.
