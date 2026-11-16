"""`jev`'s subcommands."""

import pytest

from typesafe_computer_use.helper import cli, front


def test_mcp_is_the_front(monkeypatch):
    called = []
    monkeypatch.setattr(front, "serve", lambda: called.append("mcp"))
    cli.main(["mcp"])
    assert called == ["mcp"]


@pytest.mark.parametrize("argv", [["helper"], ["stop"], ["install", "build.zip", "--sha256", "ab"], []])
def test_only_mcp_and_status_are_commands(argv):
    with pytest.raises(SystemExit) as caught:
        cli.main(argv)
    assert caught.value.code != 0


def test_status_names_this_program_and_its_models(capsys, monkeypatch, tmp_path):
    models = tmp_path / "models"
    models.mkdir()
    exe = tmp_path / "jev.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(front, "helper_version", lambda: "9.9.9")
    monkeypatch.setattr(front.sys, "executable", str(exe))
    monkeypatch.setenv("JEV_HOME", str(tmp_path / "home"))
    with pytest.raises(SystemExit) as caught:
        cli.main(["status"])
    assert caught.value.code == 0
    out = capsys.readouterr().out
    assert "version: 9.9.9" in out
    assert str(exe) in out
    assert "models: yes" in out


def test_status_without_models_says_so(capsys, monkeypatch, tmp_path):
    exe = tmp_path / "jev.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(front.sys, "executable", str(exe))
    monkeypatch.setenv("JEV_HOME", str(tmp_path / "home"))
    with pytest.raises(SystemExit) as caught:
        cli.main(["status"])
    assert caught.value.code == 0
    assert "models: no" in capsys.readouterr().out
