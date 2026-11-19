"""Tunables, the site catalog, and environment loading.

`settings()` is the only way to read configuration, and it returns one `Settings` for the process.
`load_dotenv` and `reset_settings()` drop that copy so the next call reads the environment again.
`.env` is copied into the environment by `load_dotenv` first: the Anthropic and TypeSafe SDKs read
their own variables from there, and pydantic-settings does not write them back.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Anthropic 官方 API 的主机名。用来判断写手是不是在打官方接口。
ANTHROPIC_HOST = "api.anthropic.com"
# OpenAI 官方 Chat Completions 的根地址。写手不设端点时用它。密钥用 OPENAI_API_KEY。
OPENAI_HOST = "api.openai.com"
OPENAI_BASE_URL = "https://api.openai.com/v1"
# 写手端点支持的协议。anthropic 是 Messages API。openai 是 Chat Completions。
WRITER_APIS = ("anthropic", "openai")

# 分类器能直接点名打开的网站。键是站点名。值是首页。目录里没有的站交给写手拼 URL。
SITES: dict[str, str] = {
    "github": "https://github.com/",  # GitHub
    "gmail": "https://mail.google.com/",  # Gmail
    "google_calendar": "https://calendar.google.com/",  # Google 日历
    "launchdarkly": "https://app.launchdarkly.com/",  # LaunchDarkly
    "linear": "https://linear.app/",  # Linear
    "notion": "https://www.notion.so/",  # Notion
    "slack": "https://app.slack.com/",  # Slack
    "typesafe_console": "https://console.typesafe.ai/",  # TypeSafe 控制台
}


class Settings(BaseSettings):
    """Every tunable jev reads, from `CLICKER_*` unless a field names another variable."""

    model_config = SettingsConfigDict(
        env_prefix="CLICKER_",
        env_ignore_empty=True,
        str_strip_whitespace=True,
        extra="ignore",
    )

    # 下面这些用 CLICKER_ 前缀。命令行有同名参数时以命令行为准。
    min_ocr_confidence: float = Field(0.3, ge=0, le=1)  # OCR 一行字的最低置信度。更低的丢掉
    max_options: int = Field(255, ge=1, le=255)  # 交给分类器的选项上限。TypeSafe Choice 最多 255 个
    abort_corner_px: int = Field(4, ge=0)  # 鼠标进入左上角这么大的方块就中止运行。单位是像素
    min_confidence: float = Field(0.4, ge=0, le=1)  # 分类器置信度低于这个就停。--min-confidence 优先
    steps: int = Field(100, ge=1)  # 一轮最多走多少步。--steps 优先
    delay: float = Field(2.0, ge=0)  # 每一步做完后等待的秒数。--delay 优先
    handoffs: int = Field(10, ge=0)  # 写手把停住的运行交回分类器的次数。0 表示每次停下都是终点。--handoffs 优先

    browser: str = "Google Chrome"  # 用来干活的浏览器
    email: str | None = None  # 有值才提供 type_email。把这个地址填进聚焦的文本框
    session_username: str | None = None  # 子会话登录名。不设时用 USERNAME，再没有就用 Administrator
    session_password: str | None = None  # 子会话登录密码。有值才写入凭据管理器；不设就靠子会话自己登录

    writer_api: Literal["anthropic", "openai"] = "openai"  # 写手端点的协议。默认 OpenAI 的 Chat Completions
    writer_base_url: str | None = None  # 写手端点。OpenAI 不设就用官方。Anthropic 不设就交给 SDK。完整请求地址和根地址都可以
    writer_api_key: str | None = None  # 只发给上面这个端点的密钥。官方 OpenAI 不设时用 OPENAI_API_KEY。自建端点不校验时可以不设
    writer_model: str = "gpt-6.1-terra"  # 写自由文本和提议网址的模型。默认 OpenAI 的轻量模型
    writer_vision: bool = True  # 回答模型是否看截图。纯文本模型设为 false
    writer_timeout: float = Field(120.0, gt=0)  # 一次写手请求的超时秒数。客户端会再试一次
    writer_reasoning: str | None = None  # OpenAI 协议下短调用要的推理强度。不设就交给模型
    answer_model: str = "gpt-6.1-sol"  # 分类器停下后读屏幕的模型。默认 OpenAI 的更强模型
    answer_reasoning: str | None = None  # 回答模型的推理强度。不设就交给模型

    driver: bool = True  # 分类器彻底做不完时默认交给更强模型接管。设为 false 就停在分类器
    driver_steps: int = Field(20, ge=0)  # 一次接管最多执行多少个动作，被拒绝的也算。不按模型调用次数计，也不占用 --steps
    driver_model: str | None = None  # 接管用的模型。不设就用回答模型

    # 下面这些不带 CLICKER_ 前缀。按原名读取。
    typesafe_api_key: str | None = Field(None, validation_alias="TYPESAFE_API_KEY")  # 分类器的密钥
    # SDK 自己读的端点。和密钥成对走。这里只用来判断是不是官方接口。
    anthropic_base_url: str | None = Field(None, validation_alias="ANTHROPIC_BASE_URL")
    waa_save_tree: bool = Field(False, validation_alias="JEV_WAA_SAVE_TREE")  # 为真时把每次观察的无障碍树存进运行目录
    waa_commit: str | None = Field(None, validation_alias="WAA_COMMIT")  # 这次 WAA 运行钉住的提交。写入 run.json
    waa_image_digest: str | None = Field(None, validation_alias="WAA_IMAGE_DIGEST")  # 基础镜像摘要。写入 run.json
    openai_api_key: str | None = Field(None, validation_alias="OPENAI_API_KEY")  # 官方 OpenAI 端点的密钥。别的端点不读它

    @field_validator("writer_api", mode="before")
    @classmethod
    def _lower_writer_api(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @model_validator(mode="after")
    def _trim_writer_base_url(self) -> Settings:
        """Drop the path the SDK appends, so a full request URL and the root both work.

        The OpenAI API keeps its `/v1`. Anthropic's `/v1`, `/messages`, and `/v1/messages` come off.
        `ANTHROPIC_BASE_URL` is not touched: the SDK reads it with the Anthropic credentials, so a
        key and the endpoint it was meant for always travel as a pair.
        """
        raw = self.writer_base_url
        if not raw:
            if self.writer_api == "openai":
                self.writer_base_url = OPENAI_BASE_URL
            return self
        base = raw.rstrip("/")
        suffixes = ("/chat/completions",) if self.writer_api == "openai" else ("/v1/messages", "/messages", "/v1")
        for suffix in suffixes:
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        self.writer_base_url = base.rstrip("/") or None
        return self

    @property
    def driver_budget(self) -> int:
        """Actions the driver may take once the classifier has stopped for good. Zero while it is off."""
        return self.driver_steps if self.driver else 0

    @property
    def resolved_driver_model(self) -> str:
        """The model the driver asks for each action. The answer model unless another is named."""
        return self.driver_model or self.answer_model

    @property
    def custom_writer_endpoint(self) -> bool:
        """Whether the writer talks to anything but Anthropic's own API, by either variable."""
        if self.writer_api == "openai":
            return True
        url = self.writer_base_url or self.anthropic_base_url
        return bool(url) and urlparse(url).hostname != ANTHROPIC_HOST


_settings: Settings | None = None


def settings() -> Settings:
    """The one configuration for this process, built from the environment on the first call.

    Invalid values raise `ValueError` naming the environment variable, and are not kept: the next
    call tries again.
    """
    global _settings
    if _settings is None:
        try:
            _settings = Settings()
        except ValidationError as error:
            raise ValueError(_settings_error(error)) from None
    return _settings


def reset_settings() -> None:
    """Drop the cached configuration. The next `settings()` reads the environment again."""
    global _settings
    _settings = None


def _env_name(field: str) -> str:
    info = Settings.model_fields.get(field)
    alias = getattr(info, "validation_alias", None) if info is not None else None
    if isinstance(alias, str):
        return alias
    return f"CLICKER_{field.upper()}"


def _explain(item: dict) -> str:
    field = str(item["loc"][0]) if item["loc"] else "settings"
    env = _env_name(field)
    raw = item.get("input")
    kind = item["type"]
    if field == "writer_api":
        return f"{env} must be one of {', '.join(WRITER_APIS)}, not {raw!r}"
    if kind == "bool_parsing":
        return f"{env} must be true or false, not {raw!r}"
    if field == "writer_timeout" and kind == "greater_than":
        return f"{env} must be greater than zero, not {raw!r}"
    if field == "writer_timeout" and kind in {"float_parsing", "int_parsing"}:
        return f"{env} must be a number of seconds, not {raw!r}"
    if field == "driver_steps" and kind in {"int_parsing", "float_parsing"}:
        return f"{env} must be a whole number, not {raw!r}"
    return f"{env} {item['msg']}"


def _settings_error(error: ValidationError) -> str:
    return "; ".join(_explain(item) for item in error.errors())


def load_dotenv(path: Path) -> None:
    """Set KEY=VALUE lines from a .env file into the environment unless already set.

    Drops the cached configuration either way, so a `settings()` taken before this call is not the
    one a later call returns.
    """
    if path.is_file():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))
    reset_settings()
