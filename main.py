"""건국대학교 공지 모니터링 에이전트 - 메인 실행 파일

워크플로우 조율만 담당한다. 설정 로딩/검증은 config.py, 개별 단계는
feeds/matcher/notifier/state 모듈이 책임진다.
"""

import asyncio
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

from config import load_config, validate_config
from feeds import check_ssl_health, enrich_articles_with_body, fetch_all_feeds
from matcher import match_articles
from models import Article
from notifier import (
    notify_error,
    notify_first_run,
    notify_no_new,
    notify_no_relevant,
    notify_relevant,
)
from state import filter_new_articles, load_state, mark_as_seen, save_state

logger = logging.getLogger(__name__)


def setup_logging() -> None:
    """구조화된 로깅 초기화"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def _log_run_summary(stats: dict) -> None:
    """실행 결과 요약을 사람이 읽는 형태와 구조화된 JSON 형태로 모두 출력한다."""
    logger.info(
        "실행 요약: 피드 %d개, 수집 %d건, 신규 %d건, 매칭 %d건, 분석: %s",
        stats["feeds_collected"],
        stats["articles_found"],
        stats["new_articles"],
        stats["matched_articles"],
        stats["method"],
    )
    # GitHub Actions 로그에서 grep/jq로 추출하기 좋도록 단일 라인 JSON 출력
    logger.info("run_summary_json=%s", json.dumps({"event": "run_summary", **stats}, ensure_ascii=False))


def _finalize_state(state: dict, state_path: str, all_articles: list[Article], stats: dict) -> None:
    """확인한 공지를 상태에 기록하고 실행 통계와 함께 저장한다."""
    mark_as_seen(all_articles, state)
    state["last_run_stats"] = stats
    save_state(state, state_path)


async def run() -> None:
    logger.info("=== 건국대 공지 모니터링 시작 ===")

    stats = {
        "timestamp": datetime.now().isoformat(),
        "feeds_collected": 0,
        "articles_found": 0,
        "new_articles": 0,
        "matched_articles": 0,
        "method": "none",
        "timing": {},
    }

    config = load_config()
    validate_config(config)

    state_path = str(Path(__file__).parent / config["settings"]["state_file"])
    first_run = not Path(state_path).exists()
    state = load_state(state_path)
    logger.info("기존 확인 공지: %d건", len(state.get("seen_ids", {})))

    if not config["settings"].get("ssl_verify", True):
        await check_ssl_health(config)

    logger.info("RSS 피드 수집 중...")
    t0 = time.monotonic()
    all_articles = await fetch_all_feeds(config)
    stats["timing"]["fetch_feeds"] = round(time.monotonic() - t0, 2)
    stats["feeds_collected"] = sum(1 for fc in config["feeds"].values() if fc.get("enabled", True))
    stats["articles_found"] = len(all_articles)
    logger.info("총 %d건 수집", len(all_articles))

    # 최초 실행(상태 파일 없음) 시, 기존 공지를 전부 알림 없이 '확인함' 처리한다.
    # 과거 공지 수백 건에 대한 본문 크롤링/분석/대량 알림 폭발을 방지한다.
    if first_run and config["settings"].get("seed_on_first_run", True) and all_articles:
        logger.info("최초 실행 감지: 기존 공지 %d건을 알림 없이 확인 처리합니다.", len(all_articles))
        stats["method"] = "seed"
        _finalize_state(state, state_path, all_articles, stats)
        await notify_first_run(len(all_articles))
        _log_run_summary(stats)
        logger.info("=== 최초 실행 시드 완료 ===")
        return

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
    t0 = time.monotonic()
    await enrich_articles_with_body(new_articles, config)
    stats["timing"]["enrich_articles"] = round(time.monotonic() - t0, 2)

    logger.info("Gemini로 관련도 분석 중...")
    t0 = time.monotonic()
    matched, method = await match_articles(new_articles, config)
    stats["timing"]["analyze"] = round(time.monotonic() - t0, 2)
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
    try:
        asyncio.run(run())
    except Exception as e:
        logger.exception("모니터링 실행 중 치명적 오류 발생: %s", e)
        asyncio.run(notify_error(str(e)))
        sys.exit(1)


if __name__ == "__main__":
    main()
