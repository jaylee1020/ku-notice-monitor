"""수집→분류→outbox→상태 저장 종단간 실행 테스트."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

from ku_notice_monitor.config import AppConfig
from ku_notice_monitor.feeds import FeedBatch, FeedStatus
from ku_notice_monitor.main import run
from ku_notice_monitor.matcher import MatchResult
from ku_notice_monitor.state import _initial_state


def _config() -> AppConfig:
    return AppConfig.model_validate(
        {
            "profile": {},
            "keywords": {},
            "feeds": {"학사": {"id": 234, "enabled": True}},
            "ai": {
                "model": "gpt-5.6-luna",
                "reasoning_effort": "low",
                "max_concurrency": 4,
                "request_timeout_seconds": 45,
                "image_detail": "low",
                "file_detail": "low",
            },
            "classification": {"action_window_days": 21},
            "notifications": {
                "digest_hour_kst": 21,
                "notify_empty_runs": False,
            },
            "settings": {
                "state_file": "state.json",
                "base_url": "https://www.konkuk.ac.kr",
                "rss_url_template": "https://www.konkuk.ac.kr/{board_id}",
                "allowed_download_hosts": ["konkuk.ac.kr"],
                "ssl_verify": True,
                "seed_on_first_run": True,
                "max_new_articles_per_run": 60,
                "min_feed_success_ratio": 0.7,
                "detail_refresh_interval_hours": 6,
                "detail_refresh_days": 14,
                "detail_refresh_max_articles": 30,
            },
        }
    )


def _write_state(path):
    path.write_text(
        json.dumps(_initial_state(), ensure_ascii=False),
        encoding="utf-8",
    )


def test_run_persists_and_delivers_urgent_notice(
    tmp_path,
    make_article,
    make_classified,
):
    article = make_article(
        title="수강신청 필수 확인",
        description="재학생 필수",
        link="https://www.konkuk.ac.kr/notice/1",
    )
    notice = make_classified(
        article=article,
        delivery="immediate",
        category="academic",
        source="openai",
    )
    state_path = tmp_path / "state.json"
    _write_state(state_path)
    batch = FeedBatch(
        [article],
        [FeedStatus("학사", 234, True, 1, 0.1)],
    )
    result = MatchResult([notice], "openai", set(), 0, {"total_tokens": 42})

    with (
        patch("ku_notice_monitor.main.PROJECT_ROOT", tmp_path),
        patch("ku_notice_monitor.main.load_config", return_value=_config()),
        patch(
            "ku_notice_monitor.main.fetch_all_feeds_detailed",
            new_callable=AsyncMock,
            return_value=batch,
        ),
        patch(
            "ku_notice_monitor.main.enrich_articles_with_body",
            new_callable=AsyncMock,
        ),
        patch(
            "ku_notice_monitor.main.match_articles",
            new_callable=AsyncMock,
            return_value=result,
        ),
        patch("ku_notice_monitor.main._digest_is_due", return_value=False),
        patch(
            "ku_notice_monitor.main.send_telegram_part",
            new_callable=AsyncMock,
        ) as send,
    ):
        asyncio.run(run())

    stored = json.loads(state_path.read_text(encoding="utf-8"))
    assert article.key in stored["seen_ids"]
    assert stored["pending_deliveries"] == []
    assert stored["delivery_history"]
    assert stored["last_run_stats"]["analysis_metrics"]["total_tokens"] == 42
    send.assert_awaited_once()


def test_run_schedules_retry_after_openai_failure(
    tmp_path,
    make_article,
    make_classified,
):
    article = make_article(
        title="졸업 요건 확인",
        description="적용 대상 확인 필요",
        link="https://www.konkuk.ac.kr/notice/2",
    )
    fallback = make_classified(
        article=article,
        delivery="review",
        category="academic",
        source="rules",
    )
    state_path = tmp_path / "state.json"
    _write_state(state_path)
    batch = FeedBatch(
        [article],
        [FeedStatus("학사", 234, True, 1, 0.1)],
    )
    result = MatchResult(
        [fallback],
        "rules",
        {article.key},
        0,
        {"rule_fallback_count": 1},
    )

    with (
        patch("ku_notice_monitor.main.PROJECT_ROOT", tmp_path),
        patch("ku_notice_monitor.main.load_config", return_value=_config()),
        patch(
            "ku_notice_monitor.main.fetch_all_feeds_detailed",
            new_callable=AsyncMock,
            return_value=batch,
        ),
        patch(
            "ku_notice_monitor.main.enrich_articles_with_body",
            new_callable=AsyncMock,
        ),
        patch(
            "ku_notice_monitor.main.match_articles",
            new_callable=AsyncMock,
            return_value=result,
        ),
        patch("ku_notice_monitor.main._digest_is_due", return_value=False),
        patch(
            "ku_notice_monitor.main.send_telegram_part",
            new_callable=AsyncMock,
        ),
    ):
        asyncio.run(run())

    stored = json.loads(state_path.read_text(encoding="utf-8"))
    assert article.key in stored["classification_retries"]
    assert stored["classification_retries"][article.key]["attempts"] == 1
