"""건국대학교 공지 모니터링 에이전트 - 메인 실행 파일"""

import asyncio
import logging
import os
import sys
from pathlib import Path

import yaml

from feeds import fetch_all_feeds, filter_new_articles, load_state, save_state, mark_as_seen, enrich_articles_with_body
from matcher import match_articles
from notifier import notify_relevant, notify_no_new, notify_no_relevant

logger = logging.getLogger("monitor")


def setup_logging():
    """로그 설정 초기화"""
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def validate_env():
    """필수 환경변수 사전 검증 및 경고"""
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not gemini_key:
        logger.warning("GEMINI_API_KEY 미설정 → 키워드 폴백 모드로 동작합니다.")
    if not bot_token or not chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID 미설정 → 메시지 미리보기만 출력됩니다.")


def load_config() -> dict:
    config_path = Path(__file__).parent / "config.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


async def run():
    logger.info("건국대 공지 모니터링 시작")

    # 1. 설정 로드
    config = load_config()
    state_path = Path(__file__).parent / config["settings"]["state_file"]

    # 2. 상태 로드
    state = load_state(str(state_path))
    logger.info("기존 확인 공지: %d건", len(state.get("seen_ids", {})))

    # 3. RSS 피드 수집
    logger.info("RSS 피드 수집 중...")
    all_articles = fetch_all_feeds(config)
    logger.info("총 %d건 수집", len(all_articles))

    # 4. 새 공지 필터링
    new_articles = filter_new_articles(all_articles, state)
    logger.info("새 공지: %d건", len(new_articles))

    if not new_articles:
        logger.info("새로운 공지가 없습니다.")
        await notify_no_new()
        save_state(state, str(state_path))
        return

    # 5. 새 공지 본문 크롤링
    logger.info("새 공지 본문 수집 중...")
    await enrich_articles_with_body(new_articles)

    # 6. Gemini 관련도 분석
    logger.info("Gemini로 관련도 분석 중...")
    matched = match_articles(new_articles, config)
    logger.info("관련 공지: %d건", len(matched))

    # 7. 텔레그램 알림
    if matched:
        logger.info("텔레그램으로 관련 공지 전송 중...")
        await notify_relevant(matched, len(new_articles))
        logger.info("전송 완료")
    else:
        logger.info("관련 공지 없음 알림 전송 중...")
        await notify_no_relevant(len(new_articles))
        logger.info("전송 완료")

    # 8. 상태 업데이트
    mark_as_seen(all_articles, state)
    save_state(state, str(state_path))
    logger.info("저장 완료 (총 %d건 기록)", len(state["seen_ids"]))

    logger.info("완료")


def main():
    setup_logging()
    validate_env()
    asyncio.run(run())


if __name__ == "__main__":
    main()
