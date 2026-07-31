"""건국대학교 공지 모니터링 파이프라인."""

import asyncio
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from config import load_config, validate_config
from feeds import check_ssl_health, enrich_articles_with_body, fetch_all_feeds
from matcher import match_articles
from models import Article
from notifier import (
    notify_digest,
    notify_error,
    notify_first_run,
    notify_no_new,
    notify_no_relevant,
    notify_urgent,
)
from state import (
    clear_pending_digest,
    enqueue_digest,
    filter_new_articles,
    get_pending_digest,
    load_state,
    mark_as_seen,
    save_state,
)

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def _log_run_summary(stats: dict) -> None:
    logger.info(
        "실행 요약: 피드 %d개, 수집 %d건, 신규/수정 %d건, 즉시 %d건, 검토 %d건, 요약대기 %d건, 분석=%s",
        stats["feeds_collected"],
        stats["articles_found"],
        stats["new_articles"],
        stats["immediate_articles"],
        stats["review_articles"],
        stats["digest_queued"],
        stats["method"],
    )
    logger.info("run_summary_json=%s", json.dumps({"event": "run_summary", **stats}, ensure_ascii=False))


def _finalize_state(
    state: dict,
    state_path: str,
    all_articles: list[Article],
    stats: dict,
    source_fingerprints: dict[str, str],
) -> None:
    mark_as_seen(all_articles, state, fingerprints=source_fingerprints)
    state["last_run_stats"] = stats
    save_state(state, state_path)


def _digest_is_due(config: dict, now: datetime | None = None) -> bool:
    current = now or datetime.now(_KST)
    return current.hour == config["notifications"].get("digest_hour_kst", 21)


async def _flush_digest_if_due(state: dict, config: dict) -> int:
    if not _digest_is_due(config):
        return 0
    pending = get_pending_digest(state)
    if not pending:
        return 0
    await notify_digest(pending)
    clear_pending_digest(state)
    return len(pending)


async def run() -> None:
    logger.info("=== 건국대 공지 모니터링 시작 ===")
    stats = {
        "timestamp": datetime.now(_KST).isoformat(),
        "feeds_collected": 0,
        "articles_found": 0,
        "new_articles": 0,
        "updated_articles": 0,
        "matched_articles": 0,
        "immediate_articles": 0,
        "review_articles": 0,
        "digest_queued": 0,
        "digest_sent": 0,
        "method": "none",
        "timing": {},
    }

    config = load_config()
    validate_config(config)
    state_path = str(Path(__file__).parent / config["settings"]["state_file"])
    first_run = not Path(state_path).exists()
    state = load_state(state_path)

    if not config["settings"].get("ssl_verify", True):
        await check_ssl_health(config)

    started = time.monotonic()
    all_articles = await fetch_all_feeds(config)
    stats["timing"]["fetch_feeds"] = round(time.monotonic() - started, 2)
    stats["feeds_collected"] = sum(
        1 for feed_config in config["feeds"].values() if feed_config.get("enabled", True)
    )
    stats["articles_found"] = len(all_articles)
    logger.info("총 %d건 수집", len(all_articles))
    source_fingerprints = {article.key: article.fingerprint for article in all_articles}

    if first_run and config["settings"].get("seed_on_first_run", True) and all_articles:
        stats["method"] = "seed"
        _finalize_state(state, state_path, all_articles, stats, source_fingerprints)
        await notify_first_run(len(all_articles))
        _log_run_summary(stats)
        return

    new_articles = filter_new_articles(all_articles, state)
    stats["new_articles"] = len(new_articles)
    stats["updated_articles"] = sum(article.is_update for article in new_articles)

    if new_articles:
        started = time.monotonic()
        await enrich_articles_with_body(new_articles, config)
        stats["timing"]["enrich_articles"] = round(time.monotonic() - started, 2)

        started = time.monotonic()
        matched, method = await match_articles(new_articles, config)
        stats["timing"]["analyze"] = round(time.monotonic() - started, 2)
        stats["method"] = method
        stats["matched_articles"] = len(matched)

        urgent = [
            item for item in matched if item.delivery in {"immediate", "review"}
        ]
        digest = [item for item in matched if item.delivery == "digest"]
        stats["immediate_articles"] = sum(
            item.delivery == "immediate" for item in urgent
        )
        stats["review_articles"] = sum(item.delivery == "review" for item in urgent)
        stats["digest_queued"] = len(digest)

        if urgent:
            await notify_urgent(urgent, len(new_articles))
        if digest:
            enqueue_digest(digest, state)
        if not matched and config["notifications"].get("notify_empty_runs", False):
            await notify_no_relevant(len(new_articles))
    elif config["notifications"].get("notify_empty_runs", False):
        await notify_no_new()

    stats["digest_sent"] = await _flush_digest_if_due(state, config)
    _finalize_state(state, state_path, all_articles, stats, source_fingerprints)
    _log_run_summary(stats)
    logger.info("=== 완료 ===")


def main() -> None:
    setup_logging()
    try:
        asyncio.run(run())
    except Exception as exc:
        logger.exception("모니터링 실행 중 치명적 오류 발생: %s", exc)
        asyncio.run(notify_error(str(exc)))
        sys.exit(1)


if __name__ == "__main__":
    main()
