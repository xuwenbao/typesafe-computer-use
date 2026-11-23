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
    assert "agent-prompt.md" in raw.decode("utf-8-sig")
    assert "PromptFile" in (ROOT / "scripts" / "build-helper.ps1").read_text(encoding="utf-8")
