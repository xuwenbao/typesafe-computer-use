import json
import os
from dataclasses import replace
from types import SimpleNamespace

import httpx
import openai
import pytest

from typesafe_computer_use.calls import Calls, MeteredWriter
from typesafe_computer_use.config import load_dotenv, settings
from typesafe_computer_use.models import AxNode
from typesafe_computer_use.writer import (
    ANSWER_SYSTEM,
    DRIVE_SYSTEM,
    WriterError,
    _structured,
    compose_answer,
    compose_drive_step,
    compose_url,
    make_writer,
    parse_json,
    provider,
    valid_url,
)


def test_dotenv_sets_only_missing_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("CLICKER_TEST_PRESENT", "keep")
    monkeypatch.delenv("CLICKER_TEST_NEW", raising=False)
    (tmp_path / ".env").write_text('# comment\nCLICKER_TEST_PRESENT=override\nCLICKER_TEST_NEW="quoted value"\nbroken line\n')
    load_dotenv(tmp_path / ".env")
    assert os.environ["CLICKER_TEST_PRESENT"] == "keep"
    assert os.environ["CLICKER_TEST_NEW"] == "quoted value"


def test_dotenv_missing_file_is_fine(tmp_path):
    load_dotenv(tmp_path / "nope.env")


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("http://localhost:8081", "http://localhost:8081"),
        ("http://localhost:8081/", "http://localhost:8081"),
        ("http://localhost:8081/v1", "http://localhost:8081"),
        ("http://localhost:8081/v1/messages", "http://localhost:8081"),
        ("https://proxy.example.com/v1/messages/", "https://proxy.example.com"),
    ],
)
def test_the_writer_endpoint_accepts_any_of_its_spellings(given, expected, clean_env):
    clean_env.setenv("CLICKER_WRITER_API", "anthropic")
    clean_env.setenv("CLICKER_WRITER_BASE_URL", given)
    assert settings().writer_base_url == expected


def test_the_anthropic_base_url_is_left_to_the_sdk(clean_env):
    clean_env.setenv("CLICKER_WRITER_API", "anthropic")
    clean_env.setenv("ANTHROPIC_BASE_URL", "http://localhost:8081")
    assert settings().writer_base_url is None


def test_an_endpoint_of_its_own_needs_no_key(clean_env, endpoint):
    clean_env.setenv("CLICKER_WRITER_API", "anthropic")
    clean_env.setenv("CLICKER_WRITER_BASE_URL", f"{endpoint.url}/v1/messages")
    assert compose_url(make_writer(), "open example", []) == "https://example.com"
    (request,) = endpoint.seen
    assert request["path"] == "/v1/messages"
    assert request["headers"]["x-api-key"] == "not-needed"


def test_the_endpoint_key_goes_to_the_endpoint_and_no_anthropic_credential_does(clean_env, endpoint):
    clean_env.setenv("CLICKER_WRITER_API", "anthropic")
    clean_env.setenv("CLICKER_WRITER_BASE_URL", endpoint.url)
    clean_env.setenv("CLICKER_WRITER_API_KEY", "proxy-key")
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-ant-real")
    clean_env.setenv("ANTHROPIC_AUTH_TOKEN", "oauth-real")
    compose_url(make_writer(), "open example", [])
    headers = endpoint.seen[0]["headers"]
    assert headers["x-api-key"] == "proxy-key"
    assert headers["authorization"] == "Bearer proxy-key"
    assert not any("sk-ant-real" in v or "oauth-real" in v for v in headers.values())


def test_a_bearer_token_still_goes_out_as_a_bearer_token(clean_env, endpoint):
    clean_env.setenv("CLICKER_WRITER_API", "anthropic")
    clean_env.setenv("ANTHROPIC_BASE_URL", endpoint.url)
    clean_env.setenv("ANTHROPIC_AUTH_TOKEN", "oauth-real")
    compose_url(make_writer(), "open example", [])
    headers = endpoint.seen[0]["headers"]
    assert headers["authorization"] == "Bearer oauth-real"
    assert "x-api-key" not in headers


def test_without_credentials_or_an_endpoint_there_is_no_writer(clean_env):
    clean_env.setenv("CLICKER_WRITER_API", "anthropic")
    assert make_writer() is None


def test_a_custom_endpoint_is_told_the_schema_in_the_prompt(clean_env, endpoint):
    clean_env.setenv("CLICKER_WRITER_API", "anthropic")
    clean_env.setenv("CLICKER_WRITER_BASE_URL", endpoint.url)
    endpoint.state["reply"] = 'Here you go:\n```json\n{"ok": true, "url": "https://example.com", "reason": "x"}\n```'
    # Metered, as the runner hands it out.
    assert compose_url(MeteredWriter(make_writer(), Calls()), "open example", []) == "https://example.com"
    assert '"required": ["ok", "url", "reason"]' in endpoint.seen[0]["body"]["system"]
    # Thinking on by default would spend the whole 200-token budget and leave no text.
    assert endpoint.seen[0]["body"]["thinking"] == {"type": "disabled"}


@pytest.mark.parametrize(
    ("env", "custom"),
    [
        ({"CLICKER_WRITER_API": "anthropic"}, False),
        ({"CLICKER_WRITER_API": "anthropic", "ANTHROPIC_BASE_URL": "https://api.anthropic.com"}, False),
        ({"CLICKER_WRITER_API": "anthropic", "ANTHROPIC_BASE_URL": "http://localhost:1234"}, True),
        ({"CLICKER_WRITER_API": "anthropic", "CLICKER_WRITER_BASE_URL": "http://localhost:1234/v1/messages"}, True),
    ],
)
def test_the_endpoint_is_custom_whichever_variable_names_it(clean_env, env, custom):
    for name, value in env.items():
        clean_env.setenv(name, value)
    assert settings().custom_writer_endpoint is custom


def test_anthropic_itself_gets_the_schema_only_through_output_config(clean_env, monkeypatch):
    clean_env.setenv("CLICKER_WRITER_API", "anthropic")
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    writer = make_writer()
    sent = {}

    def create(**kwargs):
        sent.update(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text='{"ok": false, "url": "", "reason": ""}')])

    monkeypatch.setattr(writer.messages, "create", create)
    compose_url(writer, "open example", [])
    assert "schema" not in sent["system"]
    assert "thinking" not in sent
    assert sent["output_config"]["format"]["schema"]["required"] == ["ok", "url", "reason"]


def test_the_startup_line_leaves_credentials_out_of_the_url(clean_env):
    clean_env.setenv("CLICKER_WRITER_API", "anthropic")
    clean_env.setenv("CLICKER_WRITER_BASE_URL", "https://user:secret@proxy.example.com/v1/messages")
    line = provider(make_writer())
    assert line.startswith("https://proxy.example.com (anthropic API)  models:")
    assert "secret" not in line


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ('{"fill": true}', {"fill": True}),
        ('```json\n{"fill": false}\n```', {"fill": False}),
        ('Sure thing:\n{"fill": true, "text": "hi"}\nHope that helps.', {"fill": True, "text": "hi"}),
    ],
)
def test_the_writer_reply_is_read_whether_or_not_it_came_plain(reply, expected):
    assert parse_json(reply) == expected


def test_a_reply_without_any_json_says_so():
    with pytest.raises(WriterError, match="without usable JSON"):
        parse_json("I am not able to help with that.")


def test_writer_clients_time_out_and_retry_once(clean_env, monkeypatch):
    seen: list[dict] = []

    class FakeAnthropic:
        def __init__(self, **kwargs):
            seen.append(kwargs)
            self.api_key = kwargs.get("api_key", "sk-ant-test")
            self.auth_token = kwargs.get("auth_token")
            self.base_url = kwargs.get("base_url") or "https://api.anthropic.com"

    class FakeOpenAI:
        def __init__(self, **kwargs):
            seen.append(kwargs)
            self.base_url = kwargs["base_url"]

    monkeypatch.setattr("typesafe_computer_use.writer.anthropic.Anthropic", FakeAnthropic)
    monkeypatch.setattr("typesafe_computer_use.openai_writer.openai.OpenAI", FakeOpenAI)

    clean_env.setenv("CLICKER_WRITER_API", "anthropic")
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert make_writer() is not None
    clean_env.setenv("CLICKER_WRITER_BASE_URL", "http://localhost:9")
    assert make_writer() is not None
    clean_env.setenv("CLICKER_WRITER_API", "openai")
    assert make_writer() is not None
    assert seen == [
        {"timeout": 120.0, "max_retries": 1},
        {
            "base_url": "http://localhost:9",
            "api_key": "not-needed",
            "auth_token": "not-needed",
            "timeout": 120.0,
            "max_retries": 1,
        },
        {"base_url": "http://localhost:9", "api_key": "not-needed", "timeout": 120.0, "max_retries": 1},
    ]


def test_the_writer_speaks_openai_unless_told_otherwise(clean_env):
    clean_env.setenv("OPENAI_BASE_URL", "http://localhost:9/v1")  # never read: it may point anywhere
    cfg = settings()
    assert cfg.writer_api == "openai"
    assert cfg.writer_base_url == "https://api.openai.com/v1"
    assert cfg.writer_model == "gpt-6.1-terra"
    assert cfg.answer_model == "gpt-6.1-sol"


def test_the_writer_timeout_is_configurable(clean_env):
    assert settings().writer_timeout == 120.0
    clean_env.setenv("CLICKER_WRITER_TIMEOUT", "45")
    assert settings().writer_timeout == 45.0
    clean_env.setenv("CLICKER_WRITER_TIMEOUT", "0")
    with pytest.raises(ValueError, match="greater than zero"):
        settings()
    clean_env.setenv("CLICKER_WRITER_TIMEOUT", "soon")
    with pytest.raises(ValueError, match="number of seconds"):
        settings()


def test_a_timed_out_request_is_a_writer_error(clean_env):
    def create(**kwargs):
        raise openai.APITimeoutError(httpx.Request("POST", "http://localhost/v1/messages"))

    writer = SimpleNamespace(messages=SimpleNamespace(create=create))
    with pytest.raises(WriterError, match="the request failed"):
        _structured(writer, "system", {"goal": "x"}, {"reason": {"type": "string"}}, 20)


def test_the_screen_reads_ask_for_enough_tokens_to_finish(screen, clean_env):
    clean_env.setenv("CLICKER_WRITER_VISION", "false")
    sent: list[int] = []
    drive = {
        "reason": "The screen is idle.",
        "action": "wait",
        "item": "",
        "text": "",
        "key": "",
        "modifier": "",
        "achieved": False,
    }
    answer = {"achieved": False, "answer": "not yet", "focus": "", "question": ""}

    def create(**kwargs):
        sent.append(kwargs["max_tokens"])
        payload = drive if len(sent) == 1 else answer
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(payload))])

    writer = SimpleNamespace(messages=SimpleNamespace(create=create))
    compose_drive_step(writer, "save the number", screen, [], [], "stuck", [], [], None, 3, None)
    compose_answer(writer, "save the number", screen, [], [], "stuck")
    assert sent == [2048, 2048]


def test_the_takeover_prompt_closes_menus_and_opens_apps_with_start():
    assert "press escape to close it, then press the shortcut" in DRIVE_SYSTEM
    assert "press start, type its name into the search box" in DRIVE_SYSTEM
    assert "Do not give_up because one way failed" in DRIVE_SYSTEM
    assert "A spinner, a number box" in DRIVE_SYSTEM
    assert "do not press escape until the selection has been used" in DRIVE_SYSTEM
    assert "cut them and paste them" in DRIVE_SYSTEM
    assert "%USERPROFILE%" in DRIVE_SYSTEM
    assert "drag: drag from item number" in DRIVE_SYSTEM
    assert "Keep the reason to three sentences." in DRIVE_SYSTEM
    assert "Do not click Previous or Next once per month." in DRIVE_SYSTEM
    assert "type into it directly" in DRIVE_SYSTEM
    assert "whether or not it came into view" in DRIVE_SYSTEM
    assert "do not tell it to repeat a click" in ANSWER_SYSTEM
    assert "click the heading that shows the month and year" in ANSWER_SYSTEM
    assert "offscreen_controls lists controls on this page" in ANSWER_SYSTEM


def test_the_writer_is_told_about_controls_that_are_not_in_view(screen, clean_env):
    clean_env.setenv("CLICKER_WRITER_VISION", "false")
    node = AxNode("AXTextField", "Record directory", 0, 0, 10, 10, True)
    showing = replace(screen, offscreen=[node])
    seen: list[dict] = []

    def create(**kwargs):
        seen.append(json.loads(kwargs["messages"][0]["content"][0]["text"]))
        payload = (
            {"reason": "next", "action": "wait", "item": "", "text": "", "key": "", "modifier": "", "achieved": False}
            if len(seen) == 1
            else {"achieved": False, "answer": "not yet", "focus": "", "question": ""}
        )
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(payload))])

    writer = SimpleNamespace(messages=SimpleNamespace(create=create))
    compose_drive_step(writer, "set the folder", showing, [], [], "stuck", [], [], None, 3, None)
    compose_answer(writer, "set the folder", showing, [], [], "stuck")
    assert seen[0]["offscreen_controls"] == [{"i": 0, "control": "field 'Record directory'"}]
    assert seen[1]["offscreen_controls"] == [{"i": 0, "control": "field 'Record directory'"}]


def test_valid_url():
    assert valid_url("https://www.cnn.com")
    assert valid_url("https://news.ycombinator.com/newest")
    assert not valid_url("http://www.cnn.com")
    assert not valid_url("https://localhost")
    assert not valid_url("https://www.cnn.com/a b")
    assert not valid_url("")


def test_a_tunable_comes_from_its_variable(clean_env):
    assert settings().steps == 100
    assert settings().delay == 2.0
    assert settings().min_confidence == 0.4
    assert settings().handoffs == 10
    assert settings().max_options == 255
    assert settings().min_ocr_confidence == 0.3
    assert settings().abort_corner_px == 4
    clean_env.setenv("CLICKER_STEPS", "12")
    clean_env.setenv("CLICKER_DELAY", "0.5")
    clean_env.setenv("CLICKER_MIN_CONFIDENCE", "0.6")
    clean_env.setenv("CLICKER_HANDOFFS", "2")
    clean_env.setenv("CLICKER_MAX_OPTIONS", "40")
    clean_env.setenv("CLICKER_MIN_OCR_CONFIDENCE", "0.5")
    clean_env.setenv("CLICKER_ABORT_CORNER_PX", "8")
    cfg = settings()
    assert (cfg.steps, cfg.delay, cfg.min_confidence, cfg.handoffs) == (12, 0.5, 0.6, 2)
    assert (cfg.max_options, cfg.min_ocr_confidence, cfg.abort_corner_px) == (40, 0.5, 8)


def test_a_bad_tunable_names_its_variable(clean_env):
    clean_env.setenv("CLICKER_MAX_OPTIONS", "lots")
    with pytest.raises(ValueError, match="CLICKER_MAX_OPTIONS"):
        settings()
    clean_env.setenv("CLICKER_MAX_OPTIONS", "40")
    clean_env.setenv("CLICKER_WRITER_VISION", "maybe")
    with pytest.raises(ValueError, match="CLICKER_WRITER_VISION"):
        settings()


def test_variables_that_are_not_prefixed_keep_their_names(clean_env):
    clean_env.setenv("TYPESAFE_API_KEY", "ts-key")
    clean_env.setenv("JEV_WAA_SAVE_TREE", "1")
    clean_env.setenv("WAA_COMMIT", "abc")
    clean_env.setenv("WAA_IMAGE_DIGEST", "sha256:def")
    cfg = settings()
    assert cfg.typesafe_api_key == "ts-key"
    assert cfg.waa_save_tree is True
    assert cfg.waa_commit == "abc"
    assert cfg.waa_image_digest == "sha256:def"
    assert cfg.anthropic_base_url is None
    clean_env.setenv("OPENAI_API_KEY", "sk-openai")
    assert settings().openai_api_key == "sk-openai"
