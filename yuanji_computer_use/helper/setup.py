"""Point an agent at the installed `clicker.exe`, when the person at the keyboard says to.

`clicker setup` prints the change and writes nothing. `--write` is what stores it. A key is read
only from a terminal, into the user's `.env`, and is never printed back.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import unicodedata
from getpass import getpass
from pathlib import Path

from . import home

HOSTS = ("cursor", "claude-code", "claude-desktop", "codex", "workbuddy")
LABELS = {
    "cursor": "Cursor",
    "claude-code": "Claude Code",
    "claude-desktop": "Claude Desktop",
    "codex": "Codex",
    "workbuddy": "WorkBuddy",
}
ENTRY_KEY = "TYPESAFE_API_KEY"
MODEL_KEY = "OPENAI_API_KEY"
WRITER_URL = "CLICKER_WRITER_BASE_URL"
WRITER_KEY = "CLICKER_WRITER_API_KEY"
WRITER_MODEL = "CLICKER_WRITER_MODEL"
ANSWER_MODEL = "CLICKER_ANSWER_MODEL"
COMPATIBLE = (WRITER_URL, WRITER_KEY, WRITER_MODEL, ANSWER_MODEL)
STEPS = ("选择助手", "选择生效范围", "安装使用说明", "确认写入", "保存密钥")


class SetupError(Exception):
    """A choice the command will not carry out."""


TYPESAFE_NOTE = "TYPESAFE_API_KEY：jev 每一步决定点哪里，用的是 TypeSafe 的分类器。没有它，run_goal 不能运行。"


class Guide:
    """Install jev for an agent: MCP config, the skill, and the API key."""

    @staticmethod
    def program_exe() -> Path:
        """What an agent starts. A frozen build is `clicker.exe`; a checkout is the `clicker` script in its environment,
        since the interpreter alone would run `python mcp`."""
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve()
        windows = sys.platform == "win32"
        script = Path(sys.prefix) / ("Scripts" if windows else "bin") / ("clicker.exe" if windows else "clicker")
        if not script.is_file():
            raise SetupError(f"找不到 clicker 命令：{script}。请从安装好的 clicker.exe 运行 clicker setup。")
        return script

    @staticmethod
    def skill_source(exe: Path) -> Path:
        beside = exe.parent / "skills" / "jev"
        if beside.is_dir() or getattr(sys, "frozen", False):
            return beside
        checkout = Path(__file__).resolve().parents[2] / "skills" / "jev"
        return checkout if checkout.is_dir() else beside

    @staticmethod
    def _roots() -> tuple[Path, Path]:
        if sys.platform == "win32":
            folder = Path(os.environ.get("USERPROFILE") or Path.home())
            roaming = Path(os.environ.get("APPDATA") or folder / "AppData" / "Roaming")
            return folder, roaming
        folder = Path.home()
        if sys.platform == "darwin":
            return folder, folder / "Library" / "Application Support"
        return folder, folder / ".config"

    @staticmethod
    def mcp_entry(exe: Path) -> dict:
        return {"command": str(exe), "args": ["mcp"]}

    @staticmethod
    def _config_path(host: str, scope: str, project: Path, home: Path, appdata: Path) -> Path | None:
        """Where this host keeps MCP config. None means this command will not write that file."""
        if host in ("cursor", "workbuddy"):
            base = home if scope == "user" else project
            folder = ".cursor" if host == "cursor" else ".workbuddy"
            return base / folder / "mcp.json"
        if host == "claude-desktop":
            # Claude Desktop has one user config. A project scope is not a file this command knows.
            if scope != "user":
                return None
            return appdata / "Claude" / "claude_desktop_config.json"
        if host == "codex" and scope == "user":
            return home / ".codex" / "config.toml"
        return None

    @staticmethod
    def _skill_dir(host: str, scope: str, project: Path, home: Path) -> Path | None:
        """Where this host reads a skill directory. None means the copy is described and not done."""
        if host == "claude-desktop":
            return None
        if host in ("cursor", "workbuddy"):
            folder = ".cursor" if host == "cursor" else ".workbuddy"
            base = home / folder if scope == "user" else project / folder
        elif host == "claude-code":
            base = home / ".claude" if scope == "user" else project / ".claude"
        elif host == "codex" and scope == "user":
            base = home / ".codex"
        else:
            return None
        return base / "skills" / "jev"

    @staticmethod
    def _print_change(lines: list[str]) -> None:
        print("\n".join(lines))

    @staticmethod
    def _write_text(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        if path.exists():
            shutil.copyfile(path, path.with_name(path.name + ".bak"))
        os.replace(temporary, path)

    @staticmethod
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
        entry = Guide.mcp_entry(exe)
        existing = servers.get("jev")
        if existing is not None and existing != entry and not replace:
            raise SetupError(f"{path} already configures jev differently; pass --replace")
        servers["jev"] = entry
        text = json.dumps(current, indent=2) + "\n"
        Guide._print_change([f"config: {path}", text.rstrip()])
        if write and existing != entry:
            Guide._write_text(path, text)
            print(f"wrote {path}")

    @staticmethod
    def _codex_config(path: Path, exe: Path, write: bool) -> None:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        if "[mcp_servers.jev]" in text:
            Guide._print_change([f"config: {path}", "codex already has [mcp_servers.jev]; left unchanged"])
            return
        block = f'\n[mcp_servers.jev]\ncommand = {json.dumps(str(exe))}\nargs = ["mcp"]\n'
        Guide._print_change([f"config: {path}", block.rstrip()])
        if write:
            Guide._write_text(path, text + block)
            print(f"wrote {path}")

    @staticmethod
    def _claude_command(scope: str, exe: Path, project: Path, write: bool, runner, which) -> None:
        command = ["claude", "mcp", "add", "--scope", scope, "jev", "--", str(exe), "mcp"]
        shown = f'claude mcp add --scope {scope} jev -- "{exe}" mcp'
        found = which("claude") if which else None
        if write and found:
            Guide._print_change([f"run: {shown}"])
            result = runner(command, cwd=str(project) if scope == "project" else None)
            if result != 0:
                raise SetupError(f"claude mcp add exited {result}")
            print("ran claude mcp add")
            return
        reason = "claude was not found on PATH" if write else "not written"
        Guide._print_change([f"command: {shown}", reason])

    @staticmethod
    def _same_tree(left: Path, right: Path) -> bool:
        def files(root: Path) -> dict[Path, bytes]:
            return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}

        return files(left) == files(right)

    @staticmethod
    def _copy_skill(source: Path, dest: Path, write: bool, replace: bool) -> None:
        if not source.is_dir():
            raise SetupError(f"the jev skill is not installed beside the program at {source}")
        Guide._print_change([f"skill: {source} -> {dest}"])
        if dest.exists() and not Guide._same_tree(source, dest):
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

    @staticmethod
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
        folder, roaming = Guide._roots()
        home = home or folder
        appdata = appdata or roaming
        project = project.resolve()
        if host == "claude-code":
            Guide._claude_command(scope, exe, project, write, runner or Guide._run, which if which is not None else shutil.which)
        else:
            path = Guide._config_path(host, scope, project, home, appdata)
            if path is None:
                Guide._print_change([f"config: {host} {scope} is not a file this command writes"])
            elif host == "codex":
                Guide._codex_config(path, exe, write)
            else:
                Guide._json_config(path, exe, write, replace)
        if skill:
            dest = Guide._skill_dir(host, scope, project, home)
            if dest is None:
                Guide._print_change([f"skill: {host} has no skill directory this command writes"])
            else:
                Guide._copy_skill(Guide.skill_source(exe), dest, write, replace)
        if write:
            print(f"next: {Guide._reload_note(host)}")

    @staticmethod
    def _run(command: list[str], cwd: str | None) -> int:
        completed = subprocess.run(command, cwd=cwd, check=False)
        return completed.returncode

    @staticmethod
    def _env_value(name: str) -> str | None:
        path = home.Home.env_file()
        if not path.is_file():
            return None
        prefixes = (f"{name}=", f"export {name}=")
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            for prefix in prefixes:
                if stripped.startswith(prefix):
                    return stripped[len(prefix) :]
        return None

    @staticmethod
    def _write_env(name: str, value: str) -> None:
        path = home.Home.env_file()
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        prefixes = (f"{name}=", f"export {name}=")
        kept = [line for line in lines if not line.strip().startswith(prefixes)]
        kept.append(f"{name}={value}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(kept) + "\n", encoding="utf-8")

    @staticmethod
    def _ask_secret(name: str, label: str, prompt) -> str:
        """Ask for one key without echoing it. Returns saved, kept, or skipped; the value is never printed."""
        if Guide._env_value(name):
            print(f"已保存过 {name}。直接回车保留，粘贴新值替换。")
            had = True
        else:
            print(f"粘贴你的 {label}（保存到 .env，输入时不显示），直接回车跳过。")
            had = False
        value = prompt(f"{name}: ").strip()
        if value:
            Guide._write_env(name, value)
            print("已保存。")
            return "saved"
        print("已保留之前的值。" if had else "已跳过。")
        return "kept" if had else "skipped"

    @staticmethod
    def read_key(is_tty, prompt=getpass) -> int:
        if not is_tty():
            raise SetupError("请在你自己的终端里运行 clicker setup --key，密钥不要经过智能体的对话。")
        print(TYPESAFE_NOTE)
        print(f"它会保存到 {home.Home.env_file()}，文件里的其他设置保持不变。")
        Guide._ask_secret(ENTRY_KEY, "TypeSafe API key", prompt)
        return 0

    @staticmethod
    def _finder(which):
        return which if which is not None else shutil.which

    @staticmethod
    def _heading(number: int) -> None:
        print(f"\n第 {number}/{len(STEPS)} 步  {STEPS[number - 1]}")

    @staticmethod
    def _codex_has_jev(path: Path) -> bool:
        return path.is_file() and "[mcp_servers.jev]" in path.read_text(encoding="utf-8")

    @staticmethod
    def _detected(host: str, home_dir: Path, appdata: Path, project: Path, which) -> bool:
        if host == "claude-code":
            return bool(Guide._finder(which)("claude"))
        path = Guide._config_path(host, "user", project, home_dir, appdata)
        return path is not None and path.is_file()

    @staticmethod
    def _host_hint(host: str, home_dir: Path, appdata: Path, project: Path, which) -> str:
        if host == "claude-code":
            if Guide._finder(which)("claude"):
                return "找到了 claude 命令：会替你运行一条命令，把 jev 加进去"
            return "没有找到 claude 命令：只会显示一条命令，让你自己运行"
        path = Guide._config_path(host, "user", project, home_dir, appdata)
        if host == "codex" and path is not None and Guide._codex_has_jev(path):
            return "已经接好了：不会再改"
        if path is not None and path.is_file():
            return "已经在用：会在原配置里加上 jev，其他内容不动"
        return "还没有它的配置：会新建一份"

    @staticmethod
    def _width(text: str) -> int:
        """Columns a terminal gives the text: Chinese characters take two."""
        return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)

    @staticmethod
    def _choose(ask, question: str, options: list[tuple[str, str]], default: int = 1) -> int:
        """Number the options, and ask until one is picked. Returns its index, from 0."""
        print(question)
        width = max(Guide._width(label) for label, _ in options)
        for number, (label, hint) in enumerate(options, 1):
            print(f"  {number}. {label}{' ' * (width - Guide._width(label))}  {hint}")
        while True:
            answer = ask(f"请输入编号 [{default}]: ").strip()
            if not answer:
                return default - 1
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return int(answer) - 1
            folded = answer.lower()
            for index, (label, _) in enumerate(options):
                if folded == label.lower():
                    return index
            print(f"请输入 1 到 {len(options)} 之间的编号。")

    @staticmethod
    def _yes(ask, question: str, default: bool) -> bool:
        answer = ask(f"{question} {'[Y/n]' if default else '[y/N]'}: ").strip().lower()
        if not answer:
            return default
        return answer.startswith("y")

    @staticmethod
    def _choose_host(ask, home_dir: Path, appdata: Path, project: Path, which) -> str:
        Guide._heading(1)
        hosts = sorted(HOSTS, key=lambda host: not Guide._detected(host, home_dir, appdata, project, which))
        options = [(LABELS[host], Guide._host_hint(host, home_dir, appdata, project, which)) for host in hosts]
        return hosts[Guide._choose(ask, "你想在哪个编程助手里使用 jev?", options)]

    @staticmethod
    def _choose_scope(ask, host: str, project: Path) -> tuple[str, Path]:
        Guide._heading(2)
        if host == "claude-desktop":
            print("Claude Desktop 只支持所有项目，已为你选好。")
            return "user", project
        options = [
            ("所有项目（推荐）", "以后在这个助手里打开任何项目都能用"),
            ("只有一个项目", "只在你指定的文件夹里能用"),
        ]
        while True:
            if Guide._choose(ask, "jev 要对哪些项目可用?", options) == 0:
                return "user", project
            if host == "codex":
                print("这个向导不写 Codex 的项目配置。请选「所有项目」。")
                continue
            while True:
                answered = ask(f"项目文件夹 [{project}]: ").strip()
                folder = Path(answered) if answered else project
                if folder.is_dir():
                    return "project", folder
                print(f"找不到这个文件夹：{folder}。请重新输入。")

    @staticmethod
    def _choose_skill(ask, host: str, scope: str, project: Path, exe: Path, home_dir: Path) -> bool:
        Guide._heading(3)
        print("使用说明（技能）是一份给助手看的说明书，告诉它什么时候该用 jev、遇到 busy 或 forbidden 怎么办。")
        dest = Guide._skill_dir(host, scope, project, home_dir)
        if dest is None:
            print(f"{LABELS[host]} 不支持安装使用说明，已跳过。")
            return False
        source = Guide.skill_source(exe)
        if not source.is_dir():
            print("这个安装里没有找到使用说明，已跳过。")
            return False
        if not Guide._yes(ask, f"要把这份说明装进 {LABELS[host]} 吗?", True):
            return False
        if dest.exists() and not Guide._same_tree(source, dest):
            return Guide._yes(ask, f"{LABELS[host]} 里已经有一份不同的 jev 使用说明。用新版替换?", False)
        return True

    @staticmethod
    def _start_command(exe: Path) -> str:
        return f'"{exe}" mcp'

    @staticmethod
    def _plan_lines(host: str, scope: str, project: Path, exe: Path, home_dir: Path, appdata: Path, skill: bool, which):
        lines = []
        if host == "claude-code":
            command = f'claude mcp add --scope {scope} jev -- "{exe}" mcp'
            if Guide._finder(which)("claude"):
                lines.append(f"运行这条命令，把 jev 加进 Claude Code：{command}")
            else:
                lines.append(f"本机没有 claude 命令，所以只显示这条命令，请你自己运行：{command}")
        else:
            path = Guide._config_path(host, scope, project, home_dir, appdata)
            if Guide._already_set(host, path, exe):
                lines.append(f"{path} 里已经接好 jev，不会改动")
            else:
                if path.is_file():
                    lines.append(f"修改 {path}（原文件会先备份为 {path.name}.bak）")
                else:
                    lines.append(f"新建 {path}")
                if host == "codex":
                    lines.append("在文件末尾加上一段 [mcp_servers.jev]，其他内容保持原样")
                else:
                    lines.append("只加一项名为 jev 的条目，其他内容保持原样")
                    existing = Guide._json_jev(path)
                    if existing is not None and existing != Guide.mcp_entry(exe):
                        lines.append("原来那项 jev 指向别的程序，会被替换")
        lines.append(f"助手启动 jev 的命令：{Guide._start_command(exe)}")
        if skill:
            lines.append(f"使用说明复制到 {Guide._skill_dir(host, scope, project, home_dir)}")
        return lines

    @staticmethod
    def _already_set(host: str, path: Path, exe: Path) -> bool:
        if host == "codex":
            return Guide._codex_has_jev(path)
        return Guide._json_jev(path) == Guide.mcp_entry(exe)

    @staticmethod
    def _json_jev(path: Path):
        if not path.is_file():
            return None
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return None
        servers = current.get("mcpServers") if isinstance(current, dict) else None
        return servers.get("jev") if isinstance(servers, dict) else None

    @staticmethod
    def _reload_note(host: str) -> str:
        if host == "cursor":
            return "重启 Cursor，或在它的 MCP 设置里刷新"
        if host == "claude-code":
            return "新开一个 Claude Code 会话"
        if host == "claude-desktop":
            return "退出并重新打开 Claude Desktop"
        if host == "codex":
            return "新开一个 Codex 会话"
        return "在 WorkBuddy 的「MCP 服务管理」→「我的 MCP」里找到 jev，点「信任」，再新开一个会话。没点信任之前，WorkBuddy 不会启动 jev"

    @staticmethod
    def _write_host(host, scope, project, exe, home_dir, appdata, skill, runner, which, done, todo) -> bool:
        """Write the chosen host, then say in Chinese what happened. Returns whether the host can now start jev."""
        path = None if host == "claude-code" else Guide._config_path(host, scope, project, home_dir, appdata)
        existed = path is not None and path.is_file()
        untouched = path is not None and Guide._already_set(host, path, exe)
        with contextlib.redirect_stdout(io.StringIO()):
            Guide.apply(host, scope, project, exe, True, True, skill, home_dir, appdata, runner, which)
        if host == "claude-code":
            command = f'claude mcp add --scope {scope} jev -- "{exe}" mcp'
            if Guide._finder(which)("claude"):
                done.append(("已运行", command))
            else:
                todo.append(f"在终端运行这条命令，把 jev 加进 Claude Code：{command}")
                return False
        elif untouched:
            done.append(("未改动", f"{path}（已经接好 jev）"))
        elif existed:
            done.append(("已写入", f"{path}（原文件备份为 {path.name}.bak）"))
        else:
            done.append(("已新建", str(path)))
        if skill:
            done.append(("已安装", f"使用说明 → {Guide._skill_dir(host, scope, project, home_dir)}"))
        return True

    @staticmethod
    def _drop_env(names: tuple[str, ...]) -> list[str]:
        """Remove these settings from the user's `.env`. Returns the names that were there."""
        found = [name for name in names if Guide._env_value(name)]
        if not found:
            return []
        path = home.Home.env_file()
        lines = path.read_text(encoding="utf-8").splitlines()
        prefixes = tuple(p for name in names for p in (f"{name}=", f"export {name}="))
        kept = [line for line in lines if not line.strip().startswith(prefixes)]
        path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        return found

    @staticmethod
    def _ask_plain(ask, name: str, label: str, default: str | None = None) -> str | None:
        """A setting that is not secret, so the answer is shown. Enter keeps the value in brackets."""
        current = Guide._env_value(name) or default
        answer = ask(f"{label} [{current}]: " if current else f"{label}: ").strip()
        return answer or current

    @staticmethod
    def _choose_writer(ask, done, todo) -> list[tuple[str, str, str]]:
        """Pick the model service the writer calls. Returns the secrets still to ask for."""
        print("写作模型：jev 遇到需要「想一想」的步骤时调用它，例如输入文字、决定打开哪个网址、")
        print("最后回答你的问题、接手卡住的任务。没有它，这些都会被跳过。")
        options = [
            ("OpenAI 官方", "填 OPENAI_API_KEY"),
            ("OpenAI 兼容服务", "DeepSeek、通义千问、自建服务等：填地址、模型和密钥"),
            ("先跳过", "之后重新运行 clicker setup 补上"),
        ]
        choice = Guide._choose(ask, "写作模型用哪家服务?", options, 2 if Guide._env_value(WRITER_URL) else 1)
        later = "之后重新运行 clicker setup 补上"
        if choice == 2:
            todo.append(f"还没有配置写作模型：{later}")
            return []
        if choice == 0:
            dropped = Guide._drop_env(COMPATIBLE)
            if dropped:
                done.append(("已移除", f"兼容服务的设置：{'、'.join(dropped)}"))
            return [(MODEL_KEY, "OpenAI API key", later)]
        print()
        base = Guide._ask_plain(ask, WRITER_URL, "服务地址（例如 https://api.deepseek.com）")
        model = Guide._ask_plain(ask, WRITER_MODEL, "写作模型名（例如 deepseek-chat）")
        if not base or not model:
            todo.append(f"兼容服务缺地址或模型，没有保存：{later}")
            return []
        answer = Guide._ask_plain(ask, ANSWER_MODEL, "回答问题用的模型", model) or model
        for name, value in (("CLICKER_WRITER_API", "openai"), (WRITER_URL, base), (WRITER_MODEL, model), (ANSWER_MODEL, answer)):
            Guide._write_env(name, value)
        done.append(("已保存", f"写作模型 {model}，回答模型 {answer}，地址 {base}"))
        return [(WRITER_KEY, f"API key（{base}）", later)]

    @staticmethod
    def _save_keys(ask, prompt, done, todo) -> None:
        Guide._heading(5)
        print(f"jev 的密钥和模型设置都保存到 {home.Home.env_file()}，文件里的其他设置保持不变。")
        print()
        secrets = Guide._choose_writer(ask, done, todo)
        saved = []
        for name, label, later in (*secrets, (ENTRY_KEY, "TypeSafe API key", "之后在终端运行 clicker setup --key")):
            print()
            if name == ENTRY_KEY:
                print(TYPESAFE_NOTE)
            outcome = Guide._ask_secret(name, label, prompt)
            if outcome == "saved":
                saved.append(name)
            elif outcome == "kept":
                done.append(("已保留", f"{name}（之前保存过）"))
            else:
                todo.append(f"还没有保存 {name}：{later}")
        if saved:
            done.append(("已保存", f"{'、'.join(saved)} → {home.Home.env_file()}"))

    @staticmethod
    def _ask_setup(ask, exe: Path, home_dir: Path, appdata: Path, runner, which, prompt) -> int:
        lines = [
            "欢迎使用 jev 安装向导",
            "",
            "jev 能让你的编程助手（例如 Cursor）操作这台 Windows 电脑：点击、输入、切换窗口。",
            "这个向导只做准备工作：告诉助手怎么启动 jev，并保存 jev 要用的密钥。",
            "它不会操作屏幕，也不会启动 jev。",
            "",
            " → ".join(STEPS),
            "直接回车 = 选方括号里的默认项。按 Ctrl+C 随时退出，确认写入之前不会写任何文件。",
        ]
        if sys.platform != "win32":
            lines += ["", "提示：你现在不在 Windows 上。可以照常写配置，但 jev 只能在 Windows 上操作屏幕。"]
        print("\n".join(lines))
        done: list[tuple[str, str]] = []
        todo: list[str] = []
        try:
            host = Guide._choose_host(ask, home_dir, appdata, Path.cwd(), which)
            scope, project = Guide._choose_scope(ask, host, Path.cwd())
            skill = Guide._choose_skill(ask, host, scope, project, exe, home_dir)
            Guide._heading(4)
            print("下面这些改动还没有写到磁盘：")
            for line in Guide._plan_lines(host, scope, project, exe, home_dir, appdata, skill, which):
                print(f"  - {line}")
            if Guide._yes(ask, "确认写入?", False):
                if Guide._write_host(host, scope, project, exe, home_dir, appdata, skill, runner, which, done, todo):
                    todo.insert(0, Guide._reload_note(host))
                print("已写入。")
            else:
                print("没有写入任何文件。")
                todo.append("想接上助手时，重新运行 clicker setup")
            Guide._save_keys(ask, prompt, done, todo)
        except (KeyboardInterrupt, EOFError):
            print("\n已退出。之后的步骤没有执行。")
            return 130
        print("\n完成")
        for tag, text in done:
            print(f"  {tag}  {text}")
        if not done:
            print("  这次没有改动任何文件")
        print("\n接下来")
        todo.append("对助手说一句：用 jev 在记事本里写一行字")
        for number, line in enumerate(todo, 1):
            print(f"  {number}. {line}")
        return 0

    @staticmethod
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
        parser = argparse.ArgumentParser(prog="clicker setup", description="Show or write how an agent starts jev.")
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
                return Guide.read_key(tty, prompt)
            chosen = exe or Guide.program_exe()
            folder, roaming = Guide._roots()
            if args.host is None:
                tty = sys.stdin.isatty if is_tty is None else is_tty
                if not tty():
                    raise SetupError("请在你自己的终端里运行 clicker setup，或加上 --host。")
                return Guide._ask_setup(ask, chosen, home or folder, appdata or roaming, runner, which, prompt)
            project = args.project or Path.cwd()
            Guide.apply(
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
