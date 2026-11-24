"""The prompt the installer leaves beside clicker.exe is the one the docs show."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_docs_show_the_installed_prompt_word_for_word():
    prompt = (ROOT / "packaging" / "windows" / "agent-prompt.md").read_text(encoding="utf-8")
    doc = (ROOT / "docs" / "agent-start.md").read_text(encoding="utf-8")
    shown = doc.split("```text\n", 1)[1].split("\n```", 1)[0] + "\n"
    assert shown == prompt


def test_the_installer_ships_the_prompt():
    raw = (ROOT / "packaging" / "windows" / "clicker.iss").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "ISCC reads a file without a BOM in the ANSI code page"
    script = raw.decode("utf-8-sig")
    assert "agent-prompt.md" in script
    assert "如何接入智能体.txt" in script
    assert "postinstall" in script
    assert "skipifsilent" in script
    build = (ROOT / "scripts" / "build-helper.ps1").read_text(encoding="utf-8")
    assert "PromptFile" in build
    assert "GuideFile" in build
    assert "InstallSkillDir" in build


def test_every_document_the_prompt_names_is_installed():
    prompt = (ROOT / "packaging" / "windows" / "agent-prompt.md").read_text(encoding="utf-8")
    script = (ROOT / "packaging" / "windows" / "clicker.iss").read_text(encoding="utf-8-sig")
    assert "http" not in prompt, "a link to a branch that does not have the file yet returns 404"
    assert r'DestDir: "{app}\skills\jev-install"' in script
    for part in ("skills\\jev\\references\\tools.md", "skills\\jev-install\\references\\hosts.md"):
        assert part in prompt
        assert (ROOT / Path(part.replace("\\", "/"))).is_file()
    assert (ROOT / "skills" / "jev-install" / "references" / "troubleshooting.md").is_file()


def test_the_guide_head_is_only_the_steps():
    head = (ROOT / "packaging" / "windows" / "agent-guide-head.txt").read_text(encoding="utf-8")
    assert head.rstrip("\n").endswith("==================== 从下一行开始复制 ====================")
    assert "我已经在这台 Windows 电脑上装好了 jev" not in head
