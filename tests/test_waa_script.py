"""`scripts/waa`, run as a copy in an empty repo. docker and git are stubs, so nothing is fetched or started."""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "waa"
POPEN = subprocess.Popen


@pytest.fixture
def processes(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", POPEN)


def stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def script(tmp_path: Path, processes):
    shutil.copytree(REPO / "waa_overlay", tmp_path / "waa_overlay", ignore=shutil.ignore_patterns("__pycache__", "upstream"))
    (tmp_path / "waa_overlay" / "requirements.txt").write_text("requests\n", encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    copy = tmp_path / "scripts" / "waa"
    shutil.copy2(SCRIPT, copy)
    copy.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "commands.log"
    stub(
        bin_dir,
        "git",
        textwrap.dedent(
            """\
            #!/bin/bash
            printf 'git' >> "$COMMAND_LOG"
            for arg in "$@"; do printf ' %q' "$arg" >> "$COMMAND_LOG"; done
            printf '\\n' >> "$COMMAND_LOG"
            exit 0
            """
        ),
    )
    stub(
        bin_dir,
        "docker",
        textwrap.dedent(
            """\
            #!/bin/bash
            printf 'docker' >> "$COMMAND_LOG"
            for arg in "$@"; do printf ' %q' "$arg" >> "$COMMAND_LOG"; done
            printf '\\n' >> "$COMMAND_LOG"
            exit 0
            """
        ),
    )
    stub(bin_dir, "uv", "#!/bin/bash\nexit 0\n")
    kvm = tmp_path / "kvm"
    kvm.write_text("", encoding="utf-8")
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "COMMAND_LOG": str(log),
        "WAA_KVM_DEVICE": str(kvm),
    }

    def run(*args: str, **extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(copy), *args], capture_output=True, text=True, env={**env, **extra}, timeout=30)

    run.log = log  # type: ignore[attr-defined]
    run.repo = tmp_path  # type: ignore[attr-defined]
    return run


def commands(script) -> str:
    log = script.log
    return log.read_text(encoding="utf-8") if log.is_file() else ""


def checkout(script, *tasks: str) -> None:
    root = script.repo / ".waa" / "WindowsAgentArena" / "src" / "win-arena-container"
    client = root / "client"
    client.mkdir(parents=True, exist_ok=True)
    for task in tasks:
        domain, task_id = task.split("/")
        path = client / "evaluation_examples_windows" / "examples" / domain / f"{task_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    (client / "run.py").write_text("# existing\n", encoding="utf-8")
    storage = script.repo / ".waa" / "storage"
    storage.mkdir(parents=True)
    (storage / "disk").write_text("", encoding="utf-8")


def test_no_command_prints_the_usage(script):
    result = script()
    assert result.returncode == 2
    assert "scripts/waa setup" in result.stdout
    assert "DOMAIN/ID" in result.stdout


def test_setup_prints_the_pinned_fetch_the_build_and_the_import_check(script):
    result = script("setup")
    assert result.returncode == 0, result.stderr
    text = result.stdout + commands(script)
    assert "eb220c4dcf64babefc1bfc287edda91ab04132ed" in text
    assert "sha256:5f503d0bdb1b09bdfffebdc7337c237af4d6bd08d046a18c97e1c244c0e1a028" in text
    assert "docker build" in text
    assert "src/win-arena-container/waa-requirements.txt" in text
    assert "desktop_env.evaluators" in text and "typesafe_computer_use.waa.agent" in text
    assert "docker run" in text


def test_prepare_image_without_an_iso_explains_where_to_get_one(script):
    checkout(script)
    result = script("prepare-image")
    assert result.returncode == 2
    assert "Windows 11 Enterprise Evaluation" in result.stderr
    assert "info.microsoft.com" in result.stderr
    assert "setup.iso" in result.stderr
    assert "docker" not in commands(script)


def test_prepare_image_prints_the_vm_container(script, tmp_path: Path):
    checkout(script)
    iso = tmp_path / "setup.iso"
    iso.write_text("iso", encoding="utf-8")
    result = script("prepare-image", "--iso", str(iso), WAA_RAM_SIZE="4G", WAA_CPU_CORES="2")
    assert result.returncode == 0, result.stderr
    text = result.stdout
    assert "--device=/dev/kvm" in text
    assert "--cap-add NET_ADMIN" in text
    assert "-p 8006:8006" in text and "-p 3390:3389" in text
    assert f"source={iso}" in text and "target=/custom.iso" in text
    assert "/storage" in text
    assert "RAM_SIZE=4G" in text and "CPU_CORES=2" in text
    assert "--prepare-image" in text and "--start-client" in text


@pytest.mark.parametrize("flag", ["--ocr", "--ocr=rapidocr", "--envs", "--envs=2", "--num-workers", "--num-workers=4"])
def test_run_refuses_the_removed_flags(script, flag):
    result = script("run", "chrome/abc", flag)
    assert result.returncode == 2
    assert "does not take" in result.stderr
    assert "docker" not in commands(script)


@pytest.mark.parametrize("task", ["chrome", "chrome/x/y", "../chrome/x", "/chrome/x"])
def test_a_task_is_one_domain_and_one_id(script, task):
    checkout(script, "chrome/abc")
    result = script("run", task, TYPESAFE_API_KEY="present")
    assert result.returncode == 2
    assert "a task is DOMAIN/ID" in result.stderr


def test_a_missing_task_file_is_refused(script):
    checkout(script, "chrome/abc")
    result = script("run", "chrome/missing", TYPESAFE_API_KEY="present")
    assert result.returncode == 2
    assert "no such task: chrome/missing" in result.stderr


def test_a_run_needs_the_key_kvm_and_a_golden_image(script, tmp_path: Path):
    checkout(script, "chrome/abc")
    missing = script("run", "chrome/abc")
    assert missing.returncode == 2 and "TYPESAFE_API_KEY" in missing.stderr

    no_kvm = script("run", "chrome/abc", TYPESAFE_API_KEY="present", WAA_KVM_DEVICE=str(tmp_path / "absent"))
    assert no_kvm.returncode == 2 and "/dev/kvm" in no_kvm.stderr

    storage = script.repo / ".waa" / "storage"
    for child in storage.iterdir():
        child.unlink()
    empty = script("run", "chrome/abc", TYPESAFE_API_KEY="present")
    assert empty.returncode == 2 and "prepare-image" in empty.stderr


def test_a_run_archives_the_earlier_result_and_does_not_print_the_key(script):
    checkout(script, "chrome/abc", "msedge/def")
    earlier = script.repo / "results" / "pyautogui" / "screenshot" / "jev" / "0" / "chrome" / "abc"
    earlier.mkdir(parents=True)
    (earlier / "result.txt").write_text("1\n", encoding="utf-8")
    (earlier / "jev").mkdir()
    kept = script.repo / "results" / "pyautogui" / "screenshot" / "jev" / "0" / "notepad" / "other"
    kept.mkdir(parents=True)
    (kept / "result.txt").write_text("0\n", encoding="utf-8")

    secret = "waa-secret-value-9f3c"
    result = script(
        "run", "chrome/abc", "msedge/def", TYPESAFE_API_KEY=secret, OPENAI_API_KEY=secret, CLICKER_WRITER_MODEL="luna"
    )
    assert result.returncode == 0, result.stderr
    text = result.stdout + result.stderr + commands(script)
    assert secret not in text
    assert "-e TYPESAFE_API_KEY" in result.stdout
    assert "-e OPENAI_API_KEY" in result.stdout
    assert "-e CLICKER_WRITER_MODEL" in result.stdout
    assert "RAM_SIZE=8G" in result.stdout and "CPU_CORES=8" in result.stdout
    assert "--device=/dev/kvm" in result.stdout
    assert not earlier.exists()
    archived = list((script.repo / "results" / "archive").glob("*/pyautogui/screenshot/jev/0/chrome/abc/result.txt"))
    assert archived
    assert (kept / "result.txt").is_file()
    listed = script.repo / ".waa" / "tasks"
    assert any(path.is_file() for path in listed.glob("*.json"))


def test_results_invokes_the_reader(script):
    result = script("results", "results")
    assert result.returncode == 0, result.stderr
    assert "typesafe_computer_use.waa.results" in result.stdout
