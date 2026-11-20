"""Point an agent at the installed `jev.exe`, when the person at the keyboard says to.

`jev setup` prints the change and writes nothing. `--write` is what stores it. A key is read
only from a terminal, into the user's `.env`, and is never printed back.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from getpass import getpass
from pathlib import Path

from . import home

HOSTS = ("cursor", "claude-code", "claude-desktop", "codex")
ENTRY_KEY = "TYPESAFE_API_KEY"


class SetupError(Exception):
    """A choice the command will not carry out."""


def program_exe() -> Path:
    """This program. A frozen build is `jev.exe`; a checkout is the interpreter running it."""
    return Path(sys.executable).resolve()


def skill_source(exe: Path) -> Path:
    return exe.parent / "skills" / "jev"


def _roots() -> tuple[Path, Path]:
    if sys.platform == "win32":
        folder = Path(os.environ.get("USERPROFILE") or Path.home())
        roaming = Path(os.environ.get("APPDATA") or folder / "AppData" / "Roaming")
        return folder, roaming
    folder = Path.home()
    if sys.platform == "darwin":
        return folder, folder / "Library" / "Application Support"
    return folder, folder / ".config"


def mcp_entry(exe: Path) -> dict:
    return {"command": str(exe), "args": ["mcp"]}


def _config_path(host: str, scope: str, project: Path, home: Path, appdata: Path) -> Path | None:
    """Where this host keeps MCP config. None means this command will not write that file."""
    if host == "cursor":
        base = home if scope == "user" else project
        return base / ".cursor" / "mcp.json"
    if host == "claude-desktop":
        # Claude Desktop has one user config. A project scope is not a file this command knows.
        if scope != "user":
            return None
        return appdata / "Claude" / "claude_desktop_config.json"
    if host == "codex" and scope == "user":
        return home / ".codex" / "config.toml"
    return None


def _skill_dir(host: str, scope: str, project: Path, home: Path) -> Path | None:
    """Where this host reads a skill directory. None means the copy is described and not done."""
    if host == "claude-desktop":
        return None
    if host == "cursor":
        base = home / ".cursor" if scope == "user" else project / ".cursor"
    elif host == "claude-code":
        base = home / ".claude" if scope == "user" else project / ".claude"
    elif host == "codex" and scope == "user":
        base = home / ".codex"
    else:
        return None
    return base / "skills" / "jev"


def _print_change(lines: list[str]) -> None:
    print("\n".join(lines))


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    if path.exists():
        shutil.copyfile(path, path.with_name(path.name + ".bak"))
    os.replace(temporary, path)


def _json_config(path: Path, exe: Path, write: bool, replace: bool) -> None:
    current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(current, dict):
        raise SetupError(f"{path} is not a JSON object")
    servers = current.get("mcpServers")
    if servers is None:
        servers = {}
        current["mcpServers"] = servers
    if not isinstance(servers, dict):
        raise SetupError(f"{path} has an mcpServers value that is not an object")
    entry = mcp_entry(exe)
    existing = servers.get("jev")
    if existing is not None and existing != entry and not replace:
        raise SetupError(f"{path} already configures jev differently; pass --replace")
    servers["jev"] = entry
    text = json.dumps(current, indent=2) + "\n"
    _print_change([f"config: {path}", text.rstrip()])
    if write and existing != entry:
        _write_text(path, text)
        print(f"wrote {path}")


def _codex_config(path: Path, exe: Path, write: bool) -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if "[mcp_servers.jev]" in text:
        _print_change([f"config: {path}", "codex already has [mcp_servers.jev]; left unchanged"])
        return
    block = f'\n[mcp_servers.jev]\ncommand = {json.dumps(str(exe))}\nargs = ["mcp"]\n'
    _print_change([f"config: {path}", block.rstrip()])
    if write:
        _write_text(path, text + block)
        print(f"wrote {path}")


def _claude_command(scope: str, exe: Path, project: Path, write: bool, runner, which) -> None:
    command = ["claude", "mcp", "add", "--scope", scope, "jev", "--", str(exe), "mcp"]
    shown = f'claude mcp add --scope {scope} jev -- "{exe}" mcp'
    found = which("claude") if which else None
    if write and found:
        _print_change([f"run: {shown}"])
        result = runner(command, cwd=str(project) if scope == "project" else None)
        if result != 0:
            raise SetupError(f"claude mcp add exited {result}")
        print("ran claude mcp add")
        return
    reason = "claude was not found on PATH" if write else "not written"
    _print_change([f"command: {shown}", reason])


def _same_tree(left: Path, right: Path) -> bool:
    def files(root: Path) -> dict[Path, bytes]:
        return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}

    return files(left) == files(right)


def _copy_skill(source: Path, dest: Path, write: bool, replace: bool) -> None:
    if not source.is_dir():
        raise SetupError(f"the jev skill is not installed beside the program at {source}")
    _print_change([f"skill: {source} -> {dest}"])
    if dest.exists() and not _same_tree(source, dest):
        if not replace:
            raise SetupError(f"{dest} already has a different jev skill; pass --replace")
    elif dest.exists():
        print("skill already matches")
        return
    if write:
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(source, dest)
        print(f"wrote {dest}")


def apply(
    host: str,
    scope: str,
    project: Path,
    exe: Path,
    write: bool,
    replace: bool,
    skill: bool,
    home: Path | None = None,
    appdata: Path | None = None,
    runner=None,
    which=None,
) -> None:
    """Print the change for one host. `write` stores it."""
    folder, roaming = _roots()
    home = home or folder
    appdata = appdata or roaming
    project = project.resolve()
    if host == "claude-code":
        _claude_command(scope, exe, project, write, runner or _run, which if which is not None else shutil.which)
    else:
        path = _config_path(host, scope, project, home, appdata)
        if path is None:
            _print_change([f"config: {host} {scope} is not a file this command writes"])
        elif host == "codex":
            _codex_config(path, exe, write)
        else:
            _json_config(path, exe, write, replace)
    if skill:
        dest = _skill_dir(host, scope, project, home)
        if dest is None:
            _print_change([f"skill: {host} has no skill directory this command writes"])
        else:
            _copy_skill(skill_source(exe), dest, write, replace)


def _run(command: list[str], cwd: str | None) -> int:
    completed = subprocess.run(command, cwd=cwd, check=False)
    return completed.returncode


def _write_key(value: str) -> None:
    path = home.env_file()
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    kept = [line for line in lines if not line.startswith(f"{ENTRY_KEY}=") and not line.startswith(f"export {ENTRY_KEY}=")]
    kept.append(f"{ENTRY_KEY}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    print(f"wrote {path}")


def read_key(is_tty, prompt=getpass) -> int:
    if not is_tty():
        raise SetupError("run jev setup --key in your own terminal")
    value = prompt(f"{ENTRY_KEY}: ").strip()
    if not value:
        raise SetupError("empty key")
    _write_key(value)
    return 0


def _detected(home: Path, appdata: Path, project: Path) -> str:
    lines = ["detected:"]
    for host in HOSTS:
        if host == "claude-code":
            lines.append("  claude-code: claude mcp add")
            continue
        path = _config_path(host, "user", project, home, appdata)
        if path is None:
            lines.append(f"  {host}: not written")
            continue
        lines.append(f"  {host}: {path} ({'found' if path.exists() else 'absent'})")
    return "\n".join(lines)


def _ask_setup(ask, exe: Path, home: Path, appdata: Path, runner, which) -> int:
    print(_detected(home, appdata, Path.cwd()))
    print("Hosts: " + ", ".join(HOSTS))
    host = ask("host [cursor]: ").strip() or "cursor"
    if host not in HOSTS:
        raise SetupError(f"unknown host {host!r}")
    scope = ask("scope [user]: ").strip() or "user"
    if scope not in ("user", "project"):
        raise SetupError(f"unknown scope {scope!r}")
    project = Path.cwd()
    if scope == "project":
        answered = ask(f"project directory [{project}]: ").strip()
        if answered:
            project = Path(answered)
    skill = ask("install the jev skill? [y/N]: ").strip().lower().startswith("y")
    apply(host, scope, project, exe, False, True, skill, home, appdata, runner, which)
    if ask("write these changes? [y/N]: ").strip().lower() != "y":
        return 0
    apply(host, scope, project, exe, True, True, skill, home, appdata, runner, which)
    return 0


def main(
    argv: list[str] | None = None,
    exe: Path | None = None,
    home: Path | None = None,
    appdata: Path | None = None,
    runner=None,
    which=None,
    ask=input,
    is_tty=None,
    prompt=getpass,
) -> int:
    parser = argparse.ArgumentParser(prog="jev setup", description="Show or write how an agent starts jev.")
    parser.add_argument("--host", choices=HOSTS)
    parser.add_argument("--scope", choices=("user", "project"), default="user")
    parser.add_argument("--project", type=Path)
    parser.add_argument("--skill", action="store_true")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--key", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.key:
            if args.host or args.skill or args.write or args.replace:
                raise SetupError("--key is run on its own")
            tty = sys.stdin.isatty if is_tty is None else is_tty
            return read_key(tty, prompt)
        chosen = exe or program_exe()
        folder, roaming = _roots()
        if args.host is None:
            tty = sys.stdin.isatty if is_tty is None else is_tty
            if not tty():
                raise SetupError("pass --host, or run jev setup in your own terminal")
            return _ask_setup(ask, chosen, home or folder, appdata or roaming, runner, which)
        project = args.project or Path.cwd()
        apply(
            args.host,
            args.scope,
            project,
            chosen,
            args.write,
            args.replace,
            args.skill,
            home,
            appdata,
            runner,
            which,
        )
        return 0
    except SetupError as exc:
        print(exc, file=sys.stderr)
        return 2
