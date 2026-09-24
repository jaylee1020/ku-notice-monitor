"""스마트 알림 스케줄 테스트."""

import asyncio
from datetime import datetime, timedelta
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
from ku_notice_monitor.notifier import TelegramDeliveryError, TelegramNotConfiguredError
from ku_notice_monitor.state import MAX_DELIVERY_ATTEMPTS, enqueue_delivery, enqueue_digest


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

    queued = _queue_urgent_notifications(state, urgent, fingerprints)
    queued_again = _queue_urgent_notifications(state, urgent, fingerprints)
    queued_reordered = _queue_urgent_notifications(
        state,
        list(reversed(urgent)),
        fingerprints,
    )

    assert queued == 2
    assert queued_again == 0
    assert queued_reordered == 0
    assert len(state["pending_deliveries"]) == 2
    messages = [item["text"] for item in state["pending_deliveries"]]
    # 공지마다 따로 보내므로 번호 없이 한 건씩 담긴다.
    assert all("1. " not in message and "2. " not in message for message in messages)
    assert sum("[중요]" in message for message in messages) == 1
    assert sum("[확인 필요]" in message for message in messages) == 1
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
        fingerprints,
    ) == 2
    with patch("ku_notice_monitor.main.send_telegram_part", new_callable=AsyncMock):
        asyncio.run(_flush_pending_deliveries(state, path))

    assert len(state["urgent_notice_history"]) == 2
    assert _queue_urgent_notifications(state, [notice_a], fingerprints) == 0
    assert _queue_urgent_notifications(
        state,
        [notice_a, notice_c],
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

    assert first["sent_parts"] == 1
    assert first["failed_parts"] == 1
    assert first["dropped_parts"] == 0
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


def _outbox_state(*texts, group="urgent-1"):
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
    enqueue_delivery(
        list(texts),
        state,
        kind="urgent",
        dedup_key=group,
        metadata={"group_id": group, "notice_tokens": [f"token-{group}"]},
    )
    return state


def test_permanently_rejected_part_is_dropped_and_rest_is_sent(tmp_path):
    state = _outbox_state("깨진 조각", "정상 조각")
    with patch(
        "ku_notice_monitor.main.send_telegram_part",
        new_callable=AsyncMock,
        side_effect=[TelegramDeliveryError("bad request", permanent=True), None],
    ):
        result = asyncio.run(_flush_pending_deliveries(state, str(tmp_path / "s.json")))

    assert result["dropped_parts"] == 1
    assert result["sent_parts"] == 1
    assert state["pending_deliveries"] == []
    # 완료로 기록해 같은 공지가 다시 큐에 들어오지 않게 한다.
    assert "token-urgent-1" in state["urgent_notice_history"]


def test_delivery_is_abandoned_after_max_attempts(tmp_path):
    state = _outbox_state("계속 실패")
    state["pending_deliveries"][0]["attempts"] = MAX_DELIVERY_ATTEMPTS - 1
    with patch(
        "ku_notice_monitor.main.send_telegram_part",
        new_callable=AsyncMock,
        side_effect=TelegramDeliveryError("network down"),
    ):
        result = asyncio.run(_flush_pending_deliveries(state, str(tmp_path / "s.json")))

    assert result["dropped_parts"] == 1
    assert state["pending_deliveries"] == []


def test_configuration_error_keeps_messages_without_counting_attempts(tmp_path):
    state = _outbox_state("첫 공지", group="a")
    enqueue_delivery(["둘째 공지"], state, kind="urgent", dedup_key="b", metadata={"group_id": "b"})
    with patch(
        "ku_notice_monitor.main.send_telegram_part",
        new_callable=AsyncMock,
        side_effect=TelegramDeliveryError("chat not found", configuration=True),
    ) as send:
        result = asyncio.run(_flush_pending_deliveries(state, str(tmp_path / "s.json")))

    send.assert_awaited_once()
    assert result["configuration_error"]
    assert [item["attempts"] for item in state["pending_deliveries"]] == [0, 0]


def test_missing_credentials_keep_outbox_untouched(tmp_path):
    state = _outbox_state("보존")
    with patch(
        "ku_notice_monitor.main.send_telegram_part",
        new_callable=AsyncMock,
        side_effect=TelegramNotConfiguredError("no token"),
    ):
        result = asyncio.run(_flush_pending_deliveries(state, str(tmp_path / "s.json")))

    assert result["configuration_error"] is None
    assert state["pending_deliveries"][0]["attempts"] == 0


def test_rate_limit_waits_at_least_retry_after(tmp_path):
    state = _outbox_state("속도 제한")
    with patch(
        "ku_notice_monitor.main.send_telegram_part",
        new_callable=AsyncMock,
        side_effect=TelegramDeliveryError("429", retry_after=3600),
    ):
        asyncio.run(_flush_pending_deliveries(state, str(tmp_path / "s.json")))

    item = state["pending_deliveries"][0]
    retry_at = datetime.fromisoformat(item["next_attempt_at"])
    assert retry_at - datetime.now() > timedelta(minutes=55)


def test_long_digest_is_queued_as_self_contained_parts(make_article, make_classified):
    state = {"pending_digest": []}
    enqueue_digest(
        [
            make_classified(
                article=make_article(id=str(index), title="공지 " + "가" * 170),
                summary="요약 " + "나" * 210,
            )
            for index in range(1, 25)
        ],
        state,
    )
    now = datetime(2026, 8, 1, 21, 15, tzinfo=ZoneInfo("Asia/Seoul"))
    asyncio.run(_flush_digest_if_due(state, _config(), now=now))

    parts = [item["text"] for item in state["pending_deliveries"]]
    assert len(parts) > 1
    assert all("관심 공지 24건" in part for part in parts)
    assert all(f"/{len(parts)})" in part.split("\n", 1)[0] for part in parts)


def test_feed_health_reports_source_outage_with_readable_cause():
    from ku_notice_monitor.main import SourceOutageError

    batch = FeedBatch(
        [],
        [
            FeedStatus("학사", 234, False, 0, 45.0, "응답 시간 초과", server_unavailable=True),
            FeedStatus("장학", 235, False, 0, 45.0, "응답 시간 초과", server_unavailable=True),
        ],
    )
    with pytest.raises(SourceOutageError) as info:
        _validate_feed_health(batch, {"settings": {"min_feed_success_ratio": 0.7}})
    assert info.value.detail == "응답 시간 초과 2개"
    assert "학교 서버가 응답하지 않아" in str(info.value)


def test_feed_health_partial_failure_explains_causes(make_article):
    batch = FeedBatch(
        [make_article()],
        [
            FeedStatus("학사", 234, True, 1, 0.1),
            FeedStatus("장학", 235, False, 0, 0.1, "HTTP 404 응답"),
            FeedStatus("취업", 236, False, 0, 0.1, "HTTP 404 응답"),
        ],
    )
    with pytest.raises(FeedCollectionError) as info:
        _validate_feed_health(batch, {"settings": {"min_feed_success_ratio": 0.7}})
    assert "게시판 3개 중 1개만" in str(info.value)
    assert "HTTP 404 응답 2개" in str(info.value)
