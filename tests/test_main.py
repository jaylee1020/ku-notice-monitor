"""스마트 알림 스케줄 테스트."""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from ku_notice_monitor.feeds import FeedBatch, FeedStatus
from ku_notice_monitor.main import (
    FeedCollectionError,
    _digest_is_due,
    _flush_digest_if_due,
    _flush_pending_deliveries,
    _validate_feed_health,
)
from ku_notice_monitor.state import enqueue_digest


def _config(hour=21):
    return {"notifications": {"digest_hour_kst": hour}}


def test_digest_is_due_at_configured_kst_hour():
    now = datetime(2026, 8, 1, 21, 15, tzinfo=ZoneInfo("Asia/Seoul"))
    assert _digest_is_due(_config(), now) is True


def test_digest_is_not_due_at_other_hour():
    now = datetime(2026, 8, 1, 20, 59, tzinfo=ZoneInfo("Asia/Seoul"))
    assert _digest_is_due(_config(), now) is False


def test_digest_catches_up_after_configured_hour():
    now = datetime(2026, 8, 1, 23, 15, tzinfo=ZoneInfo("Asia/Seoul"))
    assert _digest_is_due(_config(), now, {"last_digest_sent_date": None}) is True
    assert _digest_is_due(
        _config(), now, {"last_digest_sent_date": "2026-08-01"}
    ) is False


def test_flush_digest_moves_to_outbox_and_clears(make_classified):
    state = {"pending_digest": []}
    enqueue_digest([make_classified()], state)
    now = datetime(2026, 8, 1, 21, 15, tzinfo=ZoneInfo("Asia/Seoul"))
    count = asyncio.run(_flush_digest_if_due(state, _config(), now=now))
    assert count == 1
    assert state["pending_digest"] == []
    assert state["last_digest_enqueued_date"] == "2026-08-01"
    assert len(state["pending_deliveries"]) == 1


def test_outbox_success_removes_delivery(tmp_path):
    state = {
        "seen_ids": {},
        "article_fingerprints": {},
        "pending_digest": [],
        "pending_deliveries": [
            {
                "id": "delivery-1",
                "kind": "urgent",
                "text": "hello",
                "attempts": 0,
                "next_attempt_at": None,
                "metadata": {"group_id": "urgent-1"},
            }
        ],
    }
    path = str(tmp_path / "state.json")
    with patch("ku_notice_monitor.main.send_telegram_part", new_callable=AsyncMock) as send:
        result = asyncio.run(_flush_pending_deliveries(state, path))
    send.assert_awaited_once_with("hello")
    assert result["sent_parts"] == 1
    assert state["pending_deliveries"] == []


def test_outbox_failure_is_retained(tmp_path):
    state = {
        "seen_ids": {},
        "article_fingerprints": {},
        "pending_digest": [],
        "pending_deliveries": [
            {
                "id": "delivery-1",
                "kind": "urgent",
                "text": "hello",
                "attempts": 0,
                "next_attempt_at": None,
                "metadata": {"group_id": "urgent-1"},
            }
        ],
    }
    path = str(tmp_path / "state.json")
    with patch(
        "ku_notice_monitor.main.send_telegram_part",
        new_callable=AsyncMock,
        side_effect=RuntimeError("telegram down"),
    ):
        result = asyncio.run(_flush_pending_deliveries(state, path))
    assert result["failed_parts"] == 1
    assert len(state["pending_deliveries"]) == 1
    assert state["pending_deliveries"][0]["attempts"] == 1


def test_feed_health_rejects_all_failed():
    batch = FeedBatch(
        [],
        [
            FeedStatus("학사", 234, False, 0, 0.1, "timeout"),
            FeedStatus("장학", 235, False, 0, 0.1, "timeout"),
        ],
    )
    with pytest.raises(FeedCollectionError):
        _validate_feed_health(batch, {"settings": {"min_feed_success_ratio": 0.7}})


def test_feed_health_accepts_healthy_partial_collection(make_article):
    batch = FeedBatch(
        [make_article()],
        [
            FeedStatus("학사", 234, True, 1, 0.1),
            FeedStatus("장학", 235, True, 0, 0.1),
            FeedStatus("취업", 236, False, 0, 0.1, "timeout"),
        ],
    )
    ratio = _validate_feed_health(
        batch,
        {"settings": {"min_feed_success_ratio": 0.6}},
    )
    assert ratio == pytest.approx(2 / 3)
