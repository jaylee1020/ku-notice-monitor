"""설정 로딩 및 유효성 검증 모듈

config.yaml을 읽고 환경변수(PROFILE_JSON/KEYWORDS_JSON)로 개인정보를
오버라이드한 뒤, 필수 값의 구조를 검증한다.
"""

import json
import logging
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "config.yaml"


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


def load_config() -> dict:
    """config.yaml을 로드한 뒤 환경변수로 개인정보를 오버라이드한다."""
    load_dotenv(Path(__file__).parent / ".env.local")
    load_dotenv(Path(__file__).parent / ".env")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    config["profile"] = _load_json_env("PROFILE_JSON", config.get("profile", {}))
    config["keywords"] = _load_json_env("KEYWORDS_JSON", config.get("keywords", {}))

    enabled_feeds = [n for n, fc in config["feeds"].items() if fc.get("enabled", True)]
    logger.info(
        "설정 로드 완료: 활성 피드 %d개, OpenAI 모델: %s, 행동 알림 창: %d일",
        len(enabled_feeds),
        config["ai"]["model"],
        config.get("classification", {}).get("action_window_days", 21),
    )
    return config


def _validate_feeds(feeds: dict) -> None:
    for name, fc in feeds.items():
        if "id" not in fc:
            raise ValueError(f"피드 '{name}'에 필수 필드 'id'가 없습니다.")
        if not isinstance(fc["id"], int):
            raise ValueError(f"피드 '{name}'의 'id'는 정수여야 합니다: {fc['id']!r}")


def _validate_ai(ai: dict) -> None:
    if "model" not in ai:
        raise ValueError("ai 섹션에 필수 필드 'model'이 없습니다.")
    if not isinstance(ai["model"], str) or not ai["model"].strip():
        raise ValueError("ai.model은 비어있지 않은 문자열이어야 합니다.")
    effort = ai.get("reasoning_effort", "low")
    if effort not in {"none", "low", "medium", "high", "xhigh", "max"}:
        raise ValueError(f"ai.reasoning_effort가 올바르지 않습니다: {effort!r}")

    for field in ("image_detail", "file_detail"):
        value = ai.get(field, "low")
        if value not in {"low", "high", "auto"}:
            raise ValueError(f"ai.{field}은 low/high/auto 중 하나여야 합니다: {value!r}")

    concurrency = ai.get("max_concurrency", 4)
    if not isinstance(concurrency, int) or not 1 <= concurrency <= 20:
        raise ValueError(f"ai.max_concurrency는 1~20 사이 정수여야 합니다: {concurrency!r}")


def _validate_classification(classification: dict) -> None:
    action_window = classification.get("action_window_days", 21)
    if not isinstance(action_window, int) or not 0 <= action_window <= 90:
        raise ValueError(
            "classification.action_window_days는 0~90 사이 정수여야 합니다: "
            f"{action_window!r}"
        )


def _validate_settings(settings: dict) -> None:
    for field in ("state_file", "base_url", "rss_url_template"):
        if field not in settings:
            raise ValueError(f"settings 섹션에 필수 필드 '{field}'가 없습니다.")

    base_url = settings["base_url"]
    if not isinstance(base_url, str) or not base_url.startswith(("http://", "https://")):
        raise ValueError(f"settings.base_url은 http(s) URL이어야 합니다: {base_url!r}")

    rss_tpl = settings["rss_url_template"]
    if not isinstance(rss_tpl, str) or "{board_id}" not in rss_tpl:
        raise ValueError(f"settings.rss_url_template에 {{board_id}} 자리표시자가 필요합니다: {rss_tpl!r}")

    for field in ("ssl_verify", "seed_on_first_run"):
        value = settings.get(field, True)
        if not isinstance(value, bool):
            raise ValueError(f"settings.{field}는 bool이어야 합니다: {value!r}")


def _validate_notifications(notifications: dict) -> None:
    hour = notifications.get("digest_hour_kst", 21)
    if not isinstance(hour, int) or not 0 <= hour <= 23:
        raise ValueError(f"notifications.digest_hour_kst는 0~23 사이 정수여야 합니다: {hour!r}")
    notify_empty = notifications.get("notify_empty_runs", False)
    if not isinstance(notify_empty, bool):
        raise ValueError(f"notifications.notify_empty_runs는 bool이어야 합니다: {notify_empty!r}")


def _warn_runtime_environment(config: dict) -> None:
    """구조 오류는 아니지만 운영상 주의가 필요한 항목을 경고 로그로 남긴다."""
    year = config.get("profile", {}).get("year")
    if year not in (None, "", 0) and not (isinstance(year, int) and 1 <= year <= 10):
        logger.warning("profile.year가 비정상 범위입니다 (1~10 권장): %r", year)

    for env_var, message in (
        ("OPENAI_API_KEY", "OPENAI_API_KEY가 설정되지 않았습니다. 키워드 매칭으로 대체됩니다."),
        ("TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN이 설정되지 않았습니다. 텔레그램 알림이 비활성화됩니다."),
        ("TELEGRAM_CHAT_ID", "TELEGRAM_CHAT_ID가 설정되지 않았습니다. 텔레그램 알림이 비활성화됩니다."),
    ):
        if not os.environ.get(env_var):
            logger.warning(message)

    if not any(fc.get("enabled", True) for fc in config["feeds"].values()):
        logger.warning("활성화된 RSS 피드가 없습니다. config.yaml의 feeds 설정을 확인하세요.")


def validate_config(config: dict) -> None:
    """필수 설정 값의 구조를 검증한다. 구조 오류 시 ValueError, 권장사항은 경고 로그."""
    for section in (
        "profile",
        "keywords",
        "feeds",
        "ai",
        "classification",
        "notifications",
        "settings",
    ):
        if section not in config:
            raise ValueError(f"config.yaml에 필수 섹션 '{section}'이 없습니다.")

    _validate_feeds(config["feeds"])
    _validate_ai(config["ai"])
    _validate_classification(config["classification"])
    _validate_notifications(config["notifications"])
    _validate_settings(config["settings"])
    _warn_runtime_environment(config)
