"""The prompt the installer leaves beside jev.exe is the one the docs show."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_docs_show_the_installed_prompt_word_for_word():
    prompt = (ROOT / "packaging" / "windows" / "agent-prompt.md").read_text(encoding="utf-8")
    doc = (ROOT / "docs" / "agent-start.md").read_text(encoding="utf-8")
    shown = doc.split("```text\n", 1)[1].split("\n```", 1)[0] + "\n"
    assert shown == prompt


def test_the_installer_ships_the_prompt():
    raw = (ROOT / "packaging" / "windows" / "jev.iss").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "ISCC reads a file without a BOM in the ANSI code page"
    script = raw.decode("utf-8-sig")
    assert "agent-prompt.md" in script
    assert "如何接入智能体.txt" in script
    assert "postinstall" in script
    assert "skipifsilent" in script
    build = (ROOT / "scripts" / "build-helper.ps1").read_text(encoding="utf-8")
    assert "PromptFile" in build
    assert "GuideFile" in build


def test_the_guide_head_is_only_the_steps():
    head = (ROOT / "packaging" / "windows" / "agent-guide-head.txt").read_text(encoding="utf-8")
    assert head.rstrip("\n").endswith("==================== 从下一行开始复制 ====================")
    assert "我已经在这台 Windows 电脑上装好了 jev" not in head
