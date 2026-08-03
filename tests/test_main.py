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
    _queue_urgent_notifications,
    _validate_feed_health,
)
from ku_notice_monitor.state import enqueue_delivery, enqueue_digest


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


def test_urgent_notices_are_queued_as_separate_deduplicated_messages(
    make_article,
    make_classified,
):
    first = make_article(id="1", title="수강신청 확인")
    second = make_article(id="2", title="등록금 납부 확인")
    urgent = [
        make_classified(article=first, delivery="review"),
        make_classified(article=second, delivery="immediate"),
    ]
    fingerprints = {
        first.key: first.fingerprint,
        second.key: second.fingerprint,
    }
    state = {"pending_deliveries": [], "delivery_history": {}}

    queued = _queue_urgent_notifications(state, urgent, 6, fingerprints)
    queued_again = _queue_urgent_notifications(state, urgent, 6, fingerprints)
    queued_reordered = _queue_urgent_notifications(
        state,
        list(reversed(urgent)),
        0,
        fingerprints,
    )

    assert queued == 2
    assert queued_again == 0
    assert queued_reordered == 0
    assert len(state["pending_deliveries"]) == 2
    messages = [item["text"] for item in state["pending_deliveries"]]
    assert all("새 공지 6건 중 관련 1건" in message for message in messages)
    assert all("1. [" in message for message in messages)
    assert all("2. [" not in message for message in messages)
    assert sum("수강신청 확인" in message for message in messages) == 1
    assert sum("등록금 납부 확인" in message for message in messages) == 1


def test_urgent_dedup_filters_completed_subset_from_later_batch(
    tmp_path,
    make_article,
    make_classified,
):
    first = make_article(id="1", title="공지 A")
    second = make_article(id="2", title="공지 B")
    third = make_article(id="3", title="공지 C")
    notice_a = make_classified(article=first, delivery="review")
    notice_b = make_classified(article=second, delivery="immediate")
    notice_c = make_classified(article=third, delivery="review")
    fingerprints = {
        article.key: article.fingerprint
        for article in (first, second, third)
    }
    state = {
        "seen_ids": {},
        "article_fingerprints": {},
        "enriched_fingerprints": {},
        "pending_digest": [],
        "pending_deliveries": [],
        "delivery_history": {},
        "urgent_notice_history": {},
        "classification_retries": {},
    }
    path = str(tmp_path / "state.json")

    assert _queue_urgent_notifications(
        state,
        [notice_a, notice_b],
        2,
        fingerprints,
    ) == 2
    with patch("ku_notice_monitor.main.send_telegram_part", new_callable=AsyncMock):
        asyncio.run(_flush_pending_deliveries(state, path))

    assert len(state["urgent_notice_history"]) == 2
    assert _queue_urgent_notifications(state, [notice_a], 0, fingerprints) == 0
    assert _queue_urgent_notifications(
        state,
        [notice_a, notice_c],
        1,
        fingerprints,
    ) == 1
    message = state["pending_deliveries"][0]["text"]
    assert "공지 A" not in message
    assert "공지 C" in message


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


def test_multipart_outbox_retries_in_order(tmp_path):
    state = {
        "seen_ids": {},
        "article_fingerprints": {},
        "enriched_fingerprints": {},
        "pending_digest": [],
        "pending_deliveries": [],
        "delivery_history": {},
        "classification_retries": {},
    }
    enqueue_delivery(
        ["첫 조각", "둘째 조각", "셋째 조각"],
        state,
        kind="urgent",
        dedup_key="urgent-batch",
        metadata={"group_id": "urgent-batch"},
    )
    path = str(tmp_path / "state.json")

    with patch(
        "ku_notice_monitor.main.send_telegram_part",
        new_callable=AsyncMock,
        side_effect=[None, RuntimeError("temporary")],
    ) as send:
        first = asyncio.run(_flush_pending_deliveries(state, path))

    assert first == {"sent_parts": 1, "failed_parts": 1, "digest_notices_sent": 0}
    assert [item["text"] for item in state["pending_deliveries"]] == [
        "둘째 조각",
        "셋째 조각",
    ]
    assert send.await_count == 2

    with patch(
        "ku_notice_monitor.main.send_telegram_part",
        new_callable=AsyncMock,
    ) as send_too_early:
        second = asyncio.run(_flush_pending_deliveries(state, path))

    assert second["sent_parts"] == 0
    send_too_early.assert_not_awaited()

    state["pending_deliveries"][0]["next_attempt_at"] = None
    with patch(
        "ku_notice_monitor.main.send_telegram_part",
        new_callable=AsyncMock,
    ) as retry_send:
        third = asyncio.run(_flush_pending_deliveries(state, path))

    assert third["sent_parts"] == 2
    assert [call.args[0] for call in retry_send.await_args_list] == [
        "둘째 조각",
        "셋째 조각",
    ]
    assert state["pending_deliveries"] == []


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
