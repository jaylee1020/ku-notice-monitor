"""설정 로딩 및 유효성 검증 모듈.

config.yaml을 읽고 환경변수(PROFILE_TEXT/PROFILE_JSON/KEYWORDS_JSON)로 개인정보를
덮어쓴 뒤 Pydantic 모델 하나로 구조와 범위를 검증한다.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
    field_validator,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config.yaml"


class _Section(BaseModel):
    # 오타 난 설정 키가 조용히 무시되지 않게 한다.
    model_config = ConfigDict(extra="forbid")


class ProfileConfig(_Section):
    model_config = ConfigDict(extra="allow")
    major: str = ""
    previous_major: str = ""
    year: int = 0
    campus: str = ""
    status: str = ""


class KeywordConfig(_Section):
    model_config = ConfigDict(extra="allow")
    high: list[str] = Field(default_factory=list)
    medium: list[str] = Field(default_factory=list)


class FeedConfig(_Section):
    id: StrictInt
    enabled: StrictBool = True
    rss_url: str | None = None


class AIConfig(_Section):
    model: str = Field(min_length=1)
    reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"] = "medium"
    max_concurrency: StrictInt = Field(default=4, ge=1, le=20)
    request_timeout_seconds: StrictInt = Field(default=45, ge=5, le=120)
    image_detail: Literal["low", "high", "auto"] = "low"
    file_detail: Literal["low", "high", "auto"] = "low"

    @field_validator("model")
    @classmethod
    def _model_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model은 비어 있을 수 없습니다")
        return value.strip()


class ClassificationConfig(_Section):
    action_window_days: StrictInt = Field(default=21, ge=0, le=90)
    suppress_speculative_opportunities: StrictBool = True


class NotificationConfig(_Section):
    digest_hour_kst: StrictInt = Field(default=21, ge=0, le=23)
    notify_empty_runs: StrictBool = False


class RuntimeConfig(_Section):
    state_file: str
    base_url: str
    rss_url_template: str
    allowed_download_hosts: list[str] = Field(min_length=1)
    ssl_verify: StrictBool = True
    seed_on_first_run: StrictBool = True
    max_new_articles_per_run: StrictInt = Field(default=60, ge=1, le=500)
    min_feed_success_ratio: float = Field(default=0.7, gt=0, le=1)
    detail_refresh_interval_hours: StrictInt = Field(default=6, ge=1, le=24)
    detail_refresh_days: StrictInt = Field(default=14, ge=1, le=90)
    detail_refresh_max_articles: StrictInt = Field(default=30, ge=1, le=100)
    # 모든 피드가 서버 무응답으로 실패하면 잠시 기다렸다 한 번 더 수집한다.
    source_outage_retry_seconds: StrictInt = Field(default=90, ge=0, le=600)
    # 학교 서버 장애가 이 횟수만큼 연속될 때만 텔레그램으로 알린다.
    source_outage_alert_after_runs: StrictInt = Field(default=2, ge=1, le=48)

    @field_validator("base_url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("http(s) URL이어야 합니다")
        return value

    @field_validator("rss_url_template")
    @classmethod
    def _has_board_placeholder(cls, value: str) -> str:
        if "{board_id}" not in value:
            raise ValueError("{board_id} 자리표시자가 필요합니다")
        return value

    @field_validator("allowed_download_hosts")
    @classmethod
    def _non_blank_hosts(cls, value: list[str]) -> list[str]:
        if not all(host.strip() for host in value):
            raise ValueError("빈 호스트 이름은 허용되지 않습니다")
        return value


class AppConfig(_Section):
    profile_text: str = Field(default="", max_length=12_000)
    profile: ProfileConfig
    keywords: KeywordConfig
    feeds: dict[str, FeedConfig]
    ai: AIConfig
    classification: ClassificationConfig
    notifications: NotificationConfig
    settings: RuntimeConfig


def _load_json_env(var_name: str, fallback: dict) -> dict:
    """JSON 환경변수를 안전하게 로드하고, 파싱 실패 시 fallback을 반환한다."""
    raw = os.environ.get(var_name, "")
    if not raw:
        return fallback

    try:
        value = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("%s 파싱 실패: %s. config.yaml 기본값을 사용합니다.", var_name, e)
        return fallback

    if not isinstance(value, dict):
        logger.warning("%s는 JSON 객체여야 합니다. config.yaml 기본값을 사용합니다.", var_name)
        return fallback

    return value


def _load_text_env(var_name: str, fallback: str = "") -> str:
    """자연어 설정을 내용 로깅 없이 환경변수에서 읽는다."""
    raw = os.environ.get(var_name)
    return raw.strip() if raw is not None else fallback


def _format_validation_error(error: ValidationError) -> str:
    lines = []
    for item in error.errors():
        location = ".".join(str(part) for part in item["loc"]) or "(root)"
        lines.append(f"- {location}: {item['msg']}")
    return "config.yaml 설정이 올바르지 않습니다:\n" + "\n".join(lines)


def validate_config(config: dict[str, Any]) -> AppConfig:
    """설정 구조를 검증해 타입화된 설정을 반환한다. 오류 시 ValueError."""
    try:
        return AppConfig.model_validate(config)
    except ValidationError as exc:
        raise ValueError(_format_validation_error(exc)) from None


def _warn_runtime_environment(config: AppConfig) -> None:
    """구조 오류는 아니지만 운영상 주의가 필요한 항목을 경고 로그로 남긴다."""
    year = config.profile.year
    if year and not 1 <= year <= 10:
        logger.warning("profile.year가 비정상 범위입니다 (1~10 권장): %r", year)

    for env_var, message in (
        ("OPENAI_API_KEY", "OPENAI_API_KEY가 설정되지 않았습니다. 키워드 매칭으로 대체됩니다."),
        ("TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN이 설정되지 않았습니다. 알림은 outbox에 보존됩니다."),
        ("TELEGRAM_CHAT_ID", "TELEGRAM_CHAT_ID가 설정되지 않았습니다. 알림은 outbox에 보존됩니다."),
    ):
        if not os.environ.get(env_var):
            logger.warning(message)

    if not any(feed.enabled for feed in config.feeds.values()):
        logger.warning("활성화된 RSS 피드가 없습니다. config.yaml의 feeds 설정을 확인하세요.")


def load_config() -> AppConfig:
    """config.yaml을 로드한 뒤 환경변수로 개인정보를 오버라이드한다."""
    load_dotenv(PROJECT_ROOT / ".env.local")
    load_dotenv(PROJECT_ROOT / ".env")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    raw["profile_text"] = _load_text_env("PROFILE_TEXT", str(raw.get("profile_text") or ""))
    raw["profile"] = _load_json_env("PROFILE_JSON", raw.get("profile") or {})
    raw["keywords"] = _load_json_env("KEYWORDS_JSON", raw.get("keywords") or {})

    config = validate_config(raw)
    _warn_runtime_environment(config)
    logger.info(
        "설정 로드 완료: 활성 피드 %d개, OpenAI 모델: %s, 행동 알림 창: %d일",
        sum(feed.enabled for feed in config.feeds.values()),
        config.ai.model,
        config.classification.action_window_days,
    )
    return config
