"""건국대학교 공지 모니터링 에이전트 - 메인 실행 파일"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import yaml

from feeds import check_ssl_health, enrich_articles_with_body, fetch_all_feeds
from matcher import match_articles
from notifier import notify_error, notify_no_new, notify_no_relevant, notify_relevant
from state import filter_new_articles, load_state, mark_as_seen, save_state


def setup_logging() -> None:
    """구조화된 로깅 초기화"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def _load_json_env(var_name: str, fallback: dict) -> dict:
    """JSON 환경변수를 안전하게 로드하고, 파싱 실패 시 fallback 반환"""
    logger = logging.getLogger(__name__)
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
    """config.yaml 로드 후 환경변수로 개인정보 오버라이드"""
    config_path = Path(__file__).parent / "config.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    config["profile"] = _load_json_env("PROFILE_JSON", config.get("profile", {}))
    config["keywords"] = _load_json_env("KEYWORDS_JSON", config.get("keywords", {}))

    return config


def validate_config(config: dict) -> None:
    """필수 설정 값 유효성 검사. 구조 오류 시 ValueError, 권장사항은 경고 로그."""
    logger = logging.getLogger(__name__)

    required_sections = ["profile", "keywords", "feeds", "gemini", "settings"]
    for section in required_sections:
        if section not in config:
            raise ValueError(f"config.yaml에 필수 섹션 '{section}'이 없습니다.")

    for feed_name, feed_config in config["feeds"].items():
        if "id" not in feed_config:
            raise ValueError(f"피드 '{feed_name}'에 필수 필드 'id'가 없습니다.")

    if "model" not in config.get("gemini", {}):
        raise ValueError("gemini 섹션에 필수 필드 'model'이 없습니다.")

    required_settings = ["state_file", "base_url", "rss_url_template"]
    for field in required_settings:
        if field not in config.get("settings", {}):
            raise ValueError(f"settings 섹션에 필수 필드 '{field}'가 없습니다.")

    if not os.environ.get("GEMINI_API_KEY"):
        logger.warning("GEMINI_API_KEY가 설정되지 않았습니다. 키워드 매칭으로 대체됩니다.")

    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        logger.warning("TELEGRAM_BOT_TOKEN이 설정되지 않았습니다. 텔레그램 알림이 비활성화됩니다.")

    if not os.environ.get("TELEGRAM_CHAT_ID"):
        logger.warning("TELEGRAM_CHAT_ID가 설정되지 않았습니다. 텔레그램 알림이 비활성화됩니다.")

    enabled_feeds = [name for name, fc in config["feeds"].items() if fc.get("enabled", True)]
    if not enabled_feeds:
        logger.warning("활성화된 RSS 피드가 없습니다. config.yaml의 feeds 설정을 확인하세요.")


def _log_run_summary(stats: dict) -> None:
    """실행 결과 요약을 로그로 출력"""
    logger = logging.getLogger(__name__)
    logger.info(
        "실행 요약: 피드 %d개, 수집 %d건, 신규 %d건, 매칭 %d건, 분석: %s",
        stats["feeds_collected"],
        stats["articles_found"],
        stats["new_articles"],
        stats["matched_articles"],
        stats["method"],
    )


def _finalize_state(state: dict, state_path: str, all_articles, stats: dict) -> None:
    """상태 저장 및 실행 통계 기록"""
    mark_as_seen(all_articles, state)
    state["last_run_stats"] = stats
    save_state(state, state_path)


async def run() -> None:
    logger = logging.getLogger(__name__)
    logger.info("=== 건국대 공지 모니터링 시작 ===")

    stats = {
        "timestamp": datetime.now().isoformat(),
        "feeds_collected": 0,
        "articles_found": 0,
        "new_articles": 0,
        "matched_articles": 0,
        "method": "none",
    }

    config = load_config()
    validate_config(config)

    state_path = str(Path(__file__).parent / config["settings"]["state_file"])
    state = load_state(state_path)
    logger.info("기존 확인 공지: %d건", len(state.get("seen_ids", {})))

    ssl_verify = config.get("settings", {}).get("ssl_verify", False)
    if not ssl_verify:
        await check_ssl_health(config)

    logger.info("RSS 피드 수집 중...")
    all_articles = await fetch_all_feeds(config)
    enabled_feeds = [n for n, fc in config["feeds"].items() if fc.get("enabled", True)]
    stats["feeds_collected"] = len(enabled_feeds)
    stats["articles_found"] = len(all_articles)
    logger.info("총 %d건 수집", len(all_articles))

    new_articles = filter_new_articles(all_articles, state)
    stats["new_articles"] = len(new_articles)
    logger.info("새 공지: %d건", len(new_articles))

    if not new_articles:
        logger.info("새로운 공지가 없습니다.")
        await notify_no_new()
        _finalize_state(state, state_path, all_articles, stats)
        _log_run_summary(stats)
        return

    logger.info("새 공지 본문 수집 중... (%d건)", len(new_articles))
    await enrich_articles_with_body(new_articles, config)

    logger.info("Gemini로 관련도 분석 중...")
    matched, method = await match_articles(new_articles, config)
    stats["matched_articles"] = len(matched)
    stats["method"] = method
    logger.info("관련 공지: %d건", len(matched))

    if matched:
        logger.info("텔레그램으로 관련 공지 전송 중...")
        await notify_relevant(matched, len(new_articles))
    else:
        logger.info("관련 공지 없음 알림 전송 중...")
        await notify_no_relevant(len(new_articles))
    logger.info("전송 완료")

    _finalize_state(state, state_path, all_articles, stats)
    logger.info("상태 저장 완료 (총 %d건 기록)", len(state["seen_ids"]))
    _log_run_summary(stats)
    logger.info("=== 완료 ===")


def main() -> None:
    setup_logging()
    logger = logging.getLogger(__name__)
    try:
        asyncio.run(run())
    except Exception as e:
        logger.exception("모니터링 실행 중 치명적 오류 발생: %s", e)
        asyncio.run(notify_error(str(e)))
        sys.exit(1)


if __name__ == "__main__":
    main()
