import pytest

from typesafe_computer_use import cli

REASON = "the desktop perception needs PyTorch, which ships no build for an Intel Mac"


@pytest.fixture
def unsupported_machine(monkeypatch):
    monkeypatch.setattr(cli, "unsupported", lambda: REASON)

    def too_far(*args, **kwargs):
        raise AssertionError("the run went past the platform check")

    for name in ("_prepare", "capture", "run"):
        monkeypatch.setattr(cli, name, too_far)
    monkeypatch.setattr(cli.time, "sleep", too_far)


def test_clicker_stops_before_anything_else_on_an_unsupported_machine(unsupported_machine):
    with pytest.raises(SystemExit) as stop:
        cli.main(["open the settings"])
    assert stop.value.code == REASON


def test_a_replay_stops_too_since_it_reads_the_capture_with_the_same_models(unsupported_machine, tmp_path):
    with pytest.raises(SystemExit) as stop:
        cli.main(["open the settings", "--image", str(tmp_path / "raw.png")])
    assert stop.value.code == REASON


def test_inspect_stops_before_its_countdown(unsupported_machine, tmp_path):
    with pytest.raises(SystemExit) as stop:
        cli.inspect(["--out", str(tmp_path / "inspection")])
    assert stop.value.code == REASON
    assert not (tmp_path / "inspection").exists()


def test_a_supported_machine_goes_on_to_the_api_key_check(monkeypatch):
    monkeypatch.setattr(cli, "unsupported", lambda: None)
    monkeypatch.setattr(cli.config, "load_dotenv", lambda path: None)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(SystemExit) as stop:
        cli.main(["open the settings"])
    assert "TYPESAFE_API_KEY" in str(stop.value.code)
