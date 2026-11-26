import json
import os
from dataclasses import replace
from types import SimpleNamespace

import httpx
import openai
import pytest

from yuanji_computer_use.agents.driver.conversation import INSTRUCTIONS, DriverConversation
from yuanji_computer_use.config import load_dotenv, reset_settings, settings
from yuanji_computer_use.llm.calls import Calls, MeteredWriter
from yuanji_computer_use.llm.writer import ANSWER_SYSTEM, Writer, WriterError
from yuanji_computer_use.models import AxNode, Field


def test_dotenv_sets_only_missing_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("CLICKER_TEST_PRESENT", "keep")
    monkeypatch.delenv("CLICKER_TEST_NEW", raising=False)
    (tmp_path / ".env").write_text('# comment\nCLICKER_TEST_PRESENT=override\nCLICKER_TEST_NEW="quoted value"\nbroken line\n')
    load_dotenv(tmp_path / ".env")
    assert os.environ["CLICKER_TEST_PRESENT"] == "keep"
    assert os.environ["CLICKER_TEST_NEW"] == "quoted value"


def test_dotenv_missing_file_is_fine(tmp_path):
    load_dotenv(tmp_path / "nope.env")


def test_settings_is_one_object_until_reset(clean_env):
    first = settings()
    assert settings() is first


def test_an_environment_change_waits_for_reset(clean_env):
    assert settings().writer_timeout == 120.0
    os.environ["CLICKER_WRITER_TIMEOUT"] = "45"
    try:
        assert settings().writer_timeout == 120.0
        reset_settings()
        assert settings().writer_timeout == 45.0
    finally:
        os.environ.pop("CLICKER_WRITER_TIMEOUT", None)
        reset_settings()


def test_a_bad_value_is_not_cached(clean_env):
    os.environ["CLICKER_WRITER_TIMEOUT"] = "0"
    try:
        with pytest.raises(ValueError, match="greater than zero"):
            settings()
        os.environ["CLICKER_WRITER_TIMEOUT"] = "30"
        assert settings().writer_timeout == 30.0
    finally:
        os.environ.pop("CLICKER_WRITER_TIMEOUT", None)
        reset_settings()


def test_dotenv_refreshes_a_settings_already_read(tmp_path, clean_env):
    assert settings().browser == "Google Chrome"
    (tmp_path / ".env").write_text("CLICKER_BROWSER=Safari\n")
    load_dotenv(tmp_path / ".env")
    assert settings().browser == "Safari"


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
    assert Writer.open().compose_url("open example", []) == "https://example.com"
    (request,) = endpoint.seen
    assert request["path"] == "/v1/messages"
    assert request["headers"]["x-api-key"] == "not-needed"


def test_the_endpoint_key_goes_to_the_endpoint_and_no_anthropic_credential_does(clean_env, endpoint):
    clean_env.setenv("CLICKER_WRITER_API", "anthropic")
    clean_env.setenv("CLICKER_WRITER_BASE_URL", endpoint.url)
    clean_env.setenv("CLICKER_WRITER_API_KEY", "proxy-key")
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-ant-real")
    clean_env.setenv("ANTHROPIC_AUTH_TOKEN", "oauth-real")
    Writer.open().compose_url("open example", [])
    headers = endpoint.seen[0]["headers"]
    assert headers["x-api-key"] == "proxy-key"
    assert headers["authorization"] == "Bearer proxy-key"
    assert not any("sk-ant-real" in v or "oauth-real" in v for v in headers.values())


def test_a_bearer_token_still_goes_out_as_a_bearer_token(clean_env, endpoint):
    clean_env.setenv("CLICKER_WRITER_API", "anthropic")
    clean_env.setenv("ANTHROPIC_BASE_URL", endpoint.url)
    clean_env.setenv("ANTHROPIC_AUTH_TOKEN", "oauth-real")
    Writer.open().compose_url("open example", [])
    headers = endpoint.seen[0]["headers"]
    assert headers["authorization"] == "Bearer oauth-real"
    assert "x-api-key" not in headers


def test_without_credentials_or_an_endpoint_there_is_no_writer(clean_env):
    clean_env.setenv("CLICKER_WRITER_API", "anthropic")
    assert Writer.open() is None


def test_a_custom_endpoint_is_told_the_schema_in_the_prompt(clean_env, endpoint):
    clean_env.setenv("CLICKER_WRITER_API", "anthropic")
    clean_env.setenv("CLICKER_WRITER_BASE_URL", endpoint.url)
    endpoint.state["reply"] = 'Here you go:\n```json\n{"ok": true, "url": "https://example.com", "reason": "x"}\n```'
    # Metered, as the runner hands it out.
    assert Writer(MeteredWriter(Writer.open(), Calls())).compose_url("open example", []) == "https://example.com"
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
    writer = Writer.open()
    sent = {}

    def create(**kwargs):
        sent.update(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text='{"ok": false, "url": "", "reason": ""}')])

    monkeypatch.setattr(writer.messages, "create", create)
    Writer(writer).compose_url("open example", [])
    assert "schema" not in sent["system"]
    assert "thinking" not in sent
    assert sent["output_config"]["format"]["schema"]["required"] == ["ok", "url", "reason"]


def test_the_startup_line_leaves_credentials_out_of_the_url(clean_env):
    clean_env.setenv("CLICKER_WRITER_API", "anthropic")
    clean_env.setenv("CLICKER_WRITER_BASE_URL", "https://user:secret@proxy.example.com/v1/messages")
    line = Writer.open().provider()
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
    assert Writer.parse_json(reply) == expected


def test_a_reply_without_any_json_says_so():
    with pytest.raises(WriterError, match="without usable JSON"):
        Writer.parse_json("I am not able to help with that.")


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

    monkeypatch.setattr("yuanji_computer_use.llm.writer.anthropic.Anthropic", FakeAnthropic)
    monkeypatch.setattr("yuanji_computer_use.llm.openai_writer.openai.OpenAI", FakeOpenAI)

    clean_env.setenv("CLICKER_WRITER_API", "anthropic")
    clean_env.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert Writer.open() is not None
    clean_env.setenv("CLICKER_WRITER_BASE_URL", "http://localhost:9")
    assert Writer.open() is not None
    clean_env.setenv("CLICKER_WRITER_API", "openai")
    assert Writer.open() is not None
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
        Writer(writer)._structured("system", {"goal": "x"}, {"reason": {"type": "string"}}, 20)


def test_the_screen_reads_ask_for_enough_tokens_to_finish(screen, clean_env):
    clean_env.setenv("CLICKER_WRITER_VISION", "false")
    sent: list[int] = []
    answer = {"achieved": False, "answer": "not yet", "focus": "", "question": ""}

    def create(**kwargs):
        sent.append(kwargs["max_tokens"])
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(answer))])

    writer = SimpleNamespace(messages=SimpleNamespace(create=create))
    Writer(writer).compose_answer("save the number", screen, [], [], "stuck")
    assert sent == [2048]


def test_the_takeover_prompt_closes_menus_and_opens_apps_with_start():
    assert "press escape to close it, then press the shortcut" in INSTRUCTIONS
    assert "When the app you need is not on the screen, open it from the Run dialog: press run" in INSTRUCTIONS
    assert "press escape, then use run" in INSTRUCTIONS
    assert "On a Mac, press start and type its name into Spotlight" in INSTRUCTIONS
    assert "Do not give_up because one way failed" in INSTRUCTIONS
    assert "A spinner, a number box" in INSTRUCTIONS
    assert "do not press escape until the selection has been used" in INSTRUCTIONS
    assert "cut them and paste them" in INSTRUCTIONS
    assert "%USERPROFILE%" in INSTRUCTIONS
    assert "drag: drag from item number" in INSTRUCTIONS
    assert "Keep the reason to three sentences." in INSTRUCTIONS
    assert "Do not click Previous or Next once per month." in INSTRUCTIONS
    assert "type into it directly" in INSTRUCTIONS
    assert "whether or not it came into view" in INSTRUCTIONS
    assert "do not tell it to repeat a click" in ANSWER_SYSTEM
    assert "click the heading that shows the month and year" in ANSWER_SYSTEM
    assert "offscreen_controls lists controls on this page" in ANSWER_SYSTEM
    assert "a blank area of the capture cannot" in ANSWER_SYSTEM
    assert "only when on_screen.file_rows lists it" in ANSWER_SYSTEM
    assert "not a row of this folder" in ANSWER_SYSTEM
    assert "on_screen" in INSTRUCTIONS
    assert "a blank area of the capture cannot" in INSTRUCTIONS
    assert "only when on_screen.file_rows lists it" in INSTRUCTIONS
    assert "not a row of this folder" in INSTRUCTIONS
    assert "set target to that item's text" in INSTRUCTIONS
    assert "highlighted_menu_item" in INSTRUCTIONS
    assert "Do not press up or down first." in INSTRUCTIONS
    assert "highlighted_menu_item" in ANSWER_SYSTEM
    assert "set focus to press Return" in ANSWER_SYSTEM
    assert "actions_since" in ANSWER_SYSTEM
    assert "missing" in ANSWER_SYSTEM
    assert "driver_said_done" in ANSWER_SYSTEM
    assert "actions_since" in INSTRUCTIONS
    assert "Return on a file row opens" in ANSWER_SYSTEM
    assert "Set route to classifier" in ANSWER_SYSTEM
    assert "Set route to driver" in ANSWER_SYSTEM
    assert "Set route to end" in ANSWER_SYSTEM
    assert "do not set route to end" in ANSWER_SYSTEM
    assert "cannot right-click, double-click" not in ANSWER_SYSTEM
    assert "give_up and say in the reason what is missing" in INSTRUCTIONS
    assert "give the one next action" in INSTRUCTIONS
    assert "batch" not in INSTRUCTIONS
    assert "'(note: ...)' was taken" in INSTRUCTIONS
    assert "When change_course is present" in INSTRUCTIONS
    assert "do_not_repeat" in INSTRUCTIONS
    assert "your_notes" not in INSTRUCTIONS
    assert "Leave notes empty" not in INSTRUCTIONS
    assert "read_text and count it" in INSTRUCTIONS


def test_a_highlighted_menu_item_is_named_and_a_plain_focus_is_not(screen, clean_env):
    clean_env.setenv("CLICKER_WRITER_VISION", "false")
    seen: list[dict] = []

    def create(**kwargs):
        seen.append(json.loads(kwargs["messages"][0]["content"][-1]["text"]))
        payload = {"achieved": False, "answer": "not yet", "focus": "", "question": "", "route": "driver"}
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(payload))])

    writer = SimpleNamespace(messages=SimpleNamespace(create=create))
    menu = replace(screen, field=Field("AXMenuBarItem", "文件夹", "", "", 0, 0, 20, 20))
    drive = DriverConversation.screen_packet("make a folder", menu, [], [], "stuck", [], [], None, 3)
    Writer(writer).compose_answer("make a folder", menu, [], [], "stuck")
    assert [drive["highlighted_menu_item"], seen[0]["highlighted_menu_item"]] == ["文件夹", "文件夹"]

    seen.clear()
    drive = DriverConversation.screen_packet("make a folder", screen, [], [], "stuck", [], [], None, 3)
    Writer(writer).compose_answer("make a folder", screen, [], [], "stuck")
    assert "highlighted_menu_item" not in drive
    assert all("highlighted_menu_item" not in packet for packet in seen)


def test_the_writer_is_told_about_controls_that_are_not_in_view(screen, clean_env):
    clean_env.setenv("CLICKER_WRITER_VISION", "false")
    node = AxNode("AXTextField", "Record directory", 0, 0, 10, 10, True)
    showing = replace(screen, offscreen=[node])
    seen: list[dict] = []

    def create(**kwargs):
        seen.append(json.loads(kwargs["messages"][0]["content"][0]["text"]))
        payload = {"achieved": False, "answer": "not yet", "focus": "", "question": ""}
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(payload))])

    writer = SimpleNamespace(messages=SimpleNamespace(create=create))
    drive = DriverConversation.screen_packet("set the folder", showing, [], [], "stuck", [], [], None, 3)
    Writer(writer).compose_answer("set the folder", showing, [], [], "stuck")
    assert drive["offscreen_controls"] == [{"i": 0, "control": "field 'Record directory'"}]
    assert seen[0]["offscreen_controls"] == [{"i": 0, "control": "field 'Record directory'"}]


def test_valid_url():
    assert Writer.valid_url("https://www.cnn.com")
    assert Writer.valid_url("https://news.ycombinator.com/newest")
    assert not Writer.valid_url("http://www.cnn.com")
    assert not Writer.valid_url("https://localhost")
    assert not Writer.valid_url("https://www.cnn.com/a b")
    assert not Writer.valid_url("")


def test_a_tunable_comes_from_its_variable(clean_env):
    assert settings().steps == 100
    assert settings().delay == 2.0
    assert settings().min_confidence == 0.3
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
