"""`jev setup` prints a change, and writes only when asked. Nothing here touches a real home directory."""

import json

from typesafe_computer_use.helper import setup


def _main(tmp_path, argv, **kwargs):
    exe = kwargs.pop("exe", tmp_path / "app" / "jev.exe")
    return setup.main(argv, exe=exe, home=tmp_path / "home", appdata=tmp_path / "roaming", **kwargs)


def test_a_pipe_must_name_the_host(tmp_path):
    assert _main(tmp_path, [], is_tty=lambda: False) == 2
    assert list(tmp_path.rglob("*")) == []


def test_a_terminal_writes_only_after_yes(tmp_path, capsys):
    answers = iter(["cursor", "", "n", "n"])
    assert _main(tmp_path, [], ask=lambda _prompt: next(answers), is_tty=lambda: True) == 0
    assert "detected:" in capsys.readouterr().out
    assert not (tmp_path / "home" / ".cursor" / "mcp.json").exists()
    answers = iter(["cursor", "", "n", "y"])
    assert _main(tmp_path, [], ask=lambda _prompt: next(answers), is_tty=lambda: True) == 0
    saved = json.loads((tmp_path / "home" / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    assert saved["mcpServers"]["jev"]["args"] == ["mcp"]


def test_preview_writes_nothing(tmp_path, capsys):
    code = _main(tmp_path, ["--host", "cursor"])
    assert code == 0
    assert list(tmp_path.rglob("*")) == []
    assert "mcp.json" in capsys.readouterr().out


def test_json_merge_keeps_other_entries_and_writes_a_backup(tmp_path):
    path = tmp_path / "home" / ".cursor" / "mcp.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"theme": "dark", "mcpServers": {"other": {"command": "x"}}}), encoding="utf-8")
    exe = tmp_path / "app" / "jev.exe"
    assert _main(tmp_path, ["--host", "cursor", "--write"], exe=exe) == 0
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["theme"] == "dark"
    assert saved["mcpServers"]["other"] == {"command": "x"}
    assert saved["mcpServers"]["jev"] == {"command": str(exe), "args": ["mcp"]}
    assert "other" in path.with_name("mcp.json.bak").read_text(encoding="utf-8")


def test_a_different_jev_entry_needs_replace(tmp_path):
    path = tmp_path / "home" / ".cursor" / "mcp.json"
    path.parent.mkdir(parents=True)
    original = json.dumps({"mcpServers": {"jev": {"command": "old", "args": ["mcp"]}}})
    path.write_text(original, encoding="utf-8")
    assert _main(tmp_path, ["--host", "cursor", "--write"]) == 2
    assert path.read_text(encoding="utf-8") == original
    exe = tmp_path / "app" / "jev.exe"
    assert _main(tmp_path, ["--host", "cursor", "--write", "--replace"], exe=exe) == 0
    assert json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["jev"]["command"] == str(exe)


def test_codex_keeps_an_existing_section(tmp_path, capsys):
    path = tmp_path / "home" / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    original = '[mcp_servers.jev]\ncommand = "old"\n'
    path.write_text(original, encoding="utf-8")
    assert _main(tmp_path, ["--host", "codex", "--write", "--replace"]) == 0
    assert path.read_text(encoding="utf-8") == original
    assert "left unchanged" in capsys.readouterr().out


def test_claude_code_prints_the_command_and_a_stand_in_runs_it(tmp_path):
    exe = tmp_path / "app" / "jev.exe"
    ran = []

    def runner(command, cwd):
        ran.append((command, cwd))
        return 0

    assert _main(tmp_path, ["--host", "claude-code"], exe=exe, runner=runner, which=lambda _name: "claude") == 0
    assert ran == []
    project = tmp_path / "project"
    project.mkdir()
    assert (
        _main(
            tmp_path,
            ["--host", "claude-code", "--scope", "project", "--project", str(project), "--write"],
            exe=exe,
            runner=runner,
            which=lambda _name: None,
        )
        == 0
    )
    assert ran == []
    assert (
        _main(
            tmp_path,
            ["--host", "claude-code", "--scope", "project", "--project", str(project), "--write"],
            exe=exe,
            runner=runner,
            which=lambda _name: "claude",
        )
        == 0
    )
    assert ran == [(["claude", "mcp", "add", "--scope", "project", "jev", "--", str(exe), "mcp"], str(project.resolve()))]


def test_key_is_refused_when_stdin_is_not_a_terminal(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_HOME", str(tmp_path / "jev"))
    assert setup.main(["--key"], is_tty=lambda: False, prompt=lambda _label: "secret") == 2
    assert not (tmp_path / "jev" / ".env").exists()


def test_key_keeps_the_other_variables(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_HOME", str(tmp_path / "jev"))
    env = tmp_path / "jev" / ".env"
    env.parent.mkdir()
    env.write_text("OTHER=1\nTYPESAFE_API_KEY=old\n", encoding="utf-8")
    assert setup.main(["--key"], is_tty=lambda: True, prompt=lambda _label: "new-key") == 0
    text = env.read_text(encoding="utf-8")
    assert "OTHER=1" in text
    assert "TYPESAFE_API_KEY=new-key" in text
    assert "old" not in text


def test_skill_copies_the_whole_directory(tmp_path):
    exe = tmp_path / "app" / "jev.exe"
    source = exe.parent / "skills" / "jev"
    (source / "references").mkdir(parents=True)
    (source / "SKILL.md").write_text("skill", encoding="utf-8")
    (source / "references" / "tools.md").write_text("tools", encoding="utf-8")
    assert _main(tmp_path, ["--host", "cursor", "--skill", "--write"], exe=exe) == 0
    dest = tmp_path / "home" / ".cursor" / "skills" / "jev"
    assert (dest / "SKILL.md").read_text(encoding="utf-8") == "skill"
    assert (dest / "references" / "tools.md").read_text(encoding="utf-8") == "tools"
    (dest / "SKILL.md").write_text("edited", encoding="utf-8")
    assert _main(tmp_path, ["--host", "cursor", "--skill", "--write"], exe=exe) == 2
    assert (dest / "SKILL.md").read_text(encoding="utf-8") == "edited"
    assert _main(tmp_path, ["--host", "cursor", "--skill", "--write", "--replace"], exe=exe) == 0
    assert (dest / "SKILL.md").read_text(encoding="utf-8") == "skill"
