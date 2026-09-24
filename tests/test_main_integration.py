"""수집→분류→outbox→상태 저장 종단간 실행 테스트."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from ku_notice_monitor.config import AppConfig
from ku_notice_monitor.feeds import FeedBatch, FeedStatus
from ku_notice_monitor.main import DeliveryConfigurationError, run
from ku_notice_monitor.matcher import MatchResult
from ku_notice_monitor.notifier import TelegramDeliveryError
from ku_notice_monitor.profile import ProfileResolutionError
from ku_notice_monitor.state import _initial_state, mark_as_seen


def _config() -> AppConfig:
    return AppConfig.model_validate(
        {
            "profile": {},
            "keywords": {},
            "feeds": {"학사": {"id": 234, "enabled": True}},
            "ai": {
                "model": "gpt-6-luna",
                "reasoning_effort": "medium",
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
            return_value={article.key},
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
            return_value={article.key},
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


def _run_with(tmp_path, batch, *, enrich, match=None, profile=None, send=None, config=None):
    """외부 경계만 가짜로 바꾸고 실제 파이프라인을 실행한다."""
    match = match or AsyncMock(return_value=MatchResult([], "none", set(), 0, {}))
    patches = [
        patch("ku_notice_monitor.main.PROJECT_ROOT", tmp_path),
        patch("ku_notice_monitor.main.load_config", return_value=config or _config()),
        patch(
            "ku_notice_monitor.main.fetch_all_feeds_detailed",
            new_callable=AsyncMock,
            return_value=batch,
        ),
        patch("ku_notice_monitor.main.enrich_articles_with_body", side_effect=enrich),
        patch("ku_notice_monitor.main.match_articles", match),
        patch("ku_notice_monitor.main._digest_is_due", return_value=False),
        patch("ku_notice_monitor.main.send_telegram_part", send or AsyncMock()),
    ]
    if profile is not None:
        patches.append(patch("ku_notice_monitor.main.resolve_profile_snapshot", profile))
    for item in patches:
        item.start()
    try:
        asyncio.run(run())
    finally:
        for item in reversed(patches):
            item.stop()
    return match


def test_crawl_failure_during_refresh_is_not_reported_as_update(tmp_path, make_article):
    rss_article = make_article(
        title="장학 안내",
        description="RSS 요약",
        link="https://www.konkuk.ac.kr/notice/3",
        is_pinned=True,
    )
    state = _initial_state()
    enriched = make_article(
        title="장학 안내",
        description="RSS 요약\n상세 본문",
        link="https://www.konkuk.ac.kr/notice/3",
    )
    mark_as_seen(
        [rss_article],
        state,
        fingerprints={rss_article.key: rss_article.fingerprint},
        enriched_fingerprints={rss_article.key: enriched.fingerprint},
    )
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    batch = FeedBatch([rss_article], [FeedStatus("학사", 234, True, 1, 0.1)])

    async def failed_crawl(articles, _config):
        return set()  # 상세 페이지를 읽지 못함 → 본문은 RSS 요약 그대로

    match = _run_with(tmp_path, batch, enrich=failed_crawl)

    match.assert_not_awaited()
    stored = json.loads(state_path.read_text(encoding="utf-8"))
    assert stored["enriched_fingerprints"][rss_article.key] == enriched.fingerprint
    assert stored["last_run_stats"]["updated_articles"] == 0
    assert stored["last_run_stats"]["enrichment_failed"] == 1


def test_profile_failure_classifies_with_rules_and_schedules_retry(tmp_path, make_article):
    article = make_article(
        title="수강신청 필수 확인",
        link="https://www.konkuk.ac.kr/notice/4",
    )
    _write_state(tmp_path / "state.json")
    batch = FeedBatch([article], [FeedStatus("학사", 234, True, 1, 0.1)])

    async def crawl(articles, _config):
        return {item.key for item in articles}

    async def fake_match(articles, _config, *, use_ai):
        assert use_ai is False
        return MatchResult([], "rules", {item.key for item in articles}, 1, {})

    match = AsyncMock(side_effect=fake_match)
    _run_with(
        tmp_path,
        batch,
        enrich=crawl,
        match=match,
        profile=AsyncMock(side_effect=ProfileResolutionError("openai down")),
    )

    match.assert_awaited_once()
    stored = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert article.key in stored["seen_ids"]
    assert article.key in stored["classification_retries"]
    assert "openai down" in stored["last_run_stats"]["profile_metrics"]["error"]


def test_telegram_configuration_error_fails_run_after_saving_state(
    tmp_path,
    make_article,
    make_classified,
):
    article = make_article(title="등록금 납부", link="https://www.konkuk.ac.kr/notice/5")
    notice = make_classified(article=article, delivery="immediate", source="openai")
    _write_state(tmp_path / "state.json")
    batch = FeedBatch([article], [FeedStatus("학사", 234, True, 1, 0.1)])

    async def crawl(articles, _config):
        return {item.key for item in articles}

    with pytest.raises(DeliveryConfigurationError):
        _run_with(
            tmp_path,
            batch,
            enrich=crawl,
            match=AsyncMock(return_value=MatchResult([notice], "openai", set(), 0, {})),
            profile=AsyncMock(return_value=_snapshot()),
            send=AsyncMock(side_effect=TelegramDeliveryError("Unauthorized", configuration=True)),
        )

    stored = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert article.key in stored["seen_ids"]
    assert len(stored["pending_deliveries"]) == 1
    assert stored["pending_deliveries"][0]["attempts"] == 0


def _snapshot():
    from ku_notice_monitor.profile_models import ProfileSnapshot

    return ProfileSnapshot(summary="테스트 프로필")


# --- 학교 서버 장애 ---


def _outage_batch() -> FeedBatch:
    return FeedBatch(
        [],
        [FeedStatus("학사", 234, False, 0, 45.0, "응답 시간 초과", server_unavailable=True)],
    )


def _run_with_batches(tmp_path, batches, config=None):
    with (
        patch("ku_notice_monitor.main.PROJECT_ROOT", tmp_path),
        patch("ku_notice_monitor.main.load_config", return_value=config or _config()),
        patch(
            "ku_notice_monitor.main.fetch_all_feeds_detailed",
            new_callable=AsyncMock,
            side_effect=batches,
        ) as fetch,
        patch("ku_notice_monitor.main.asyncio.sleep", new_callable=AsyncMock) as sleep,
        patch(
            "ku_notice_monitor.main.enrich_articles_with_body",
            new_callable=AsyncMock,
            return_value=set(),
        ),
        patch("ku_notice_monitor.main._digest_is_due", return_value=False),
        patch(
            "ku_notice_monitor.main.send_telegram_part",
            new_callable=AsyncMock,
        ) as send,
    ):
        asyncio.run(run())
    return fetch, sleep, send


def test_brief_source_outage_recovers_on_in_run_retry(tmp_path, make_article):
    """순간 장애는 잠시 기다린 뒤 다시 수집해 정상 처리한다."""
    state_path = tmp_path / "state.json"
    _write_state(state_path)
    article = make_article(link="https://www.konkuk.ac.kr/notice/1")
    healthy = FeedBatch([article], [FeedStatus("학사", 234, True, 1, 0.1)])

    with patch(
        "ku_notice_monitor.main.match_articles",
        new_callable=AsyncMock,
        return_value=MatchResult([], "openai", set(), 0, {}),
    ):
        fetch, sleep, send = _run_with_batches(tmp_path, [_outage_batch(), healthy])

    assert fetch.await_count == 2
    sleep.assert_awaited_once_with(90)
    send.assert_not_awaited()
    stored = json.loads(state_path.read_text(encoding="utf-8"))
    assert article.key in stored["seen_ids"]
    assert "source_outage" not in stored


def test_source_outage_is_quiet_then_alerts_once_and_announces_recovery(
    tmp_path,
    make_article,
):
    """장애는 실행 실패가 아니며, 연속될 때만 한 번 알리고 복구도 알린다."""
    state_path = tmp_path / "state.json"
    _write_state(state_path)

    # 1회차: 조용히 기록만 한다.
    _, _, send = _run_with_batches(tmp_path, [_outage_batch(), _outage_batch()])
    send.assert_not_awaited()
    stored = json.loads(state_path.read_text(encoding="utf-8"))
    assert stored["source_outage"]["consecutive_runs"] == 1
    assert stored["source_outage"]["alerted"] is False
    assert stored["seen_ids"] == {}

    # 2회차: 기준(2회)에 도달해 안내를 한 번 보낸다.
    _, _, send = _run_with_batches(tmp_path, [_outage_batch(), _outage_batch()])
    send.assert_awaited_once()
    alert = send.await_args.args[0]
    assert "학교 홈페이지에 접속할 수 없습니다" in alert
    assert "2회 연속" in alert
    assert "응답 시간 초과" in alert

    # 3회차: 이미 알렸으므로 다시 보내지 않는다.
    _, _, send = _run_with_batches(tmp_path, [_outage_batch(), _outage_batch()])
    send.assert_not_awaited()
    stored = json.loads(state_path.read_text(encoding="utf-8"))
    assert stored["source_outage"]["consecutive_runs"] == 3

    # 복구: 복구 안내를 보내고 장애 기록을 지운다.
    article = make_article(link="https://www.konkuk.ac.kr/notice/1")
    healthy = FeedBatch([article], [FeedStatus("학사", 234, True, 1, 0.1)])
    with patch(
        "ku_notice_monitor.main.match_articles",
        new_callable=AsyncMock,
        return_value=MatchResult([], "openai", set(), 0, {}),
    ):
        _, _, send = _run_with_batches(tmp_path, [healthy])
    send.assert_awaited_once()
    assert "복구되었습니다" in send.await_args.args[0]
    stored = json.loads(state_path.read_text(encoding="utf-8"))
    assert "source_outage" not in stored
    assert article.key in stored["seen_ids"]


def test_source_outage_on_first_run_fails_without_creating_state(tmp_path):
    """첫 실행에 상태를 만들면 다음 실행이 기존 공지를 신규로 오인하므로 실패로 끝낸다."""
    from ku_notice_monitor.main import SourceOutageError

    with pytest.raises(SourceOutageError):
        _run_with_batches(tmp_path, [_outage_batch(), _outage_batch()])
    assert not (tmp_path / "state.json").exists()


def test_main_marks_error_notified_so_workflow_does_not_repeat(tmp_path, monkeypatch):
    from ku_notice_monitor import main as main_module

    monkeypatch.setattr(main_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(main_module, "setup_logging", lambda: None)

    async def failing_run():
        raise main_module.FeedCollectionError("게시판 9개 중 3개만 읽음")

    notify = AsyncMock(return_value=True)
    monkeypatch.setattr(main_module, "run", failing_run)
    monkeypatch.setattr(main_module, "notify_error", notify)

    with pytest.raises(SystemExit):
        main_module.main()

    assert (tmp_path / main_module.ERROR_NOTIFIED_MARKER).exists()
    assert "건너뛰었습니다" in notify.await_args.kwargs["title"]
