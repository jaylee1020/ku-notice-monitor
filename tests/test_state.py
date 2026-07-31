"""수정 공지 감지와 일일 요약 큐 테스트."""

from datetime import datetime

from ku_notice_monitor.state import (
    clear_pending_digest,
    complete_delivery,
    due_classification_retry_keys,
    due_deliveries,
    enqueue_delivery,
    enqueue_digest,
    filter_new_articles,
    get_pending_digest,
    load_state,
    mark_as_seen,
    record_delivery_failure,
    schedule_classification_retry,
)


def test_seen_article_without_baseline_fingerprint_is_not_realerted(make_article):
    article = make_article(id="1", description="내용")
    state = {"seen_ids": {article.key: "2026-01-01T00:00:00"}, "article_fingerprints": {}}
    assert filter_new_articles([article], state) == []


def test_changed_article_is_returned_as_update(make_article):
    original = make_article(id="1", description="원문")
    changed = make_article(id="1", description="수정됨")
    state = {"seen_ids": {}, "article_fingerprints": {}}
    mark_as_seen([original], state)
    result = filter_new_articles([changed], state)
    assert len(result) == 1
    assert result[0].is_update is True


def test_unchanged_article_is_filtered(make_article):
    article = make_article(id="1", description="원문")
    state = {"seen_ids": {}, "article_fingerprints": {}}
    mark_as_seen([article], state)
    assert filter_new_articles([make_article(id="1", description="원문")], state) == []


def test_mark_as_seen_can_preserve_pre_enrichment_fingerprint(make_article):
    article = make_article(description="RSS 요약")
    source_fingerprint = article.fingerprint
    article.description = "상세 페이지에서 보강한 긴 본문"
    state = {"seen_ids": {}, "article_fingerprints": {}}

    mark_as_seen(
        [article],
        state,
        fingerprints={article.key: source_fingerprint},
    )

    assert state["article_fingerprints"][article.key] == source_fingerprint


def test_enriched_fingerprint_detects_body_only_change(make_article):
    article = make_article(description="RSS 요약")
    state = {"seen_ids": {}, "article_fingerprints": {}, "enriched_fingerprints": {}}
    mark_as_seen(
        [article],
        state,
        fingerprints={article.key: article.fingerprint},
        enriched_fingerprints={article.key: "old-detail"},
    )
    result = filter_new_articles(
        [make_article(description="RSS 요약")],
        state,
        source_fingerprints={article.key: article.fingerprint},
        enriched_fingerprints={article.key: "new-detail"},
    )
    assert len(result) == 1
    assert result[0].is_update is True


def test_filter_new_articles_deduplicates_by_key(make_article):
    one = make_article(id="1")
    duplicate = make_article(id="1")
    state = {"seen_ids": {}, "article_fingerprints": {}}
    assert len(filter_new_articles([one, duplicate], state)) == 1


def test_digest_queue_deduplicates_and_round_trips(make_article, make_classified):
    article = make_article(id="5", title="인턴 모집")
    first = make_classified(article=article, reason="관심", summary="첫 요약")
    updated = make_classified(article=article, reason="더 중요", summary="새 요약")
    state = {"pending_digest": []}
    enqueue_digest([first], state)
    enqueue_digest([updated], state)
    pending = get_pending_digest(state)
    assert len(pending) == 1
    assert pending[0].summary == "새 요약"
    clear_pending_digest(state)
    assert get_pending_digest(state) == []


def test_outbox_deduplicates_and_completes():
    state = {"pending_deliveries": []}
    first = enqueue_delivery(
        ["첫 조각", "둘째 조각"],
        state,
        kind="urgent",
        dedup_key="notice-1",
        metadata={"group_id": "group-1"},
    )
    second = enqueue_delivery(
        ["첫 조각", "둘째 조각"],
        state,
        kind="urgent",
        dedup_key="notice-1",
        metadata={"group_id": "group-1"},
    )
    assert first == second
    assert len(state["pending_deliveries"]) == 2
    completed = complete_delivery(state, first[0])
    assert completed["text"] == "첫 조각"
    assert len(state["pending_deliveries"]) == 1


def test_completed_delivery_is_not_queued_again():
    state = {"pending_deliveries": [], "delivery_history": {}}
    delivery_id = enqueue_delivery(
        ["메시지"],
        state,
        kind="urgent",
        dedup_key="notice-1",
    )[0]
    complete_delivery(state, delivery_id)
    enqueue_delivery(
        ["메시지"],
        state,
        kind="urgent",
        dedup_key="notice-1",
    )
    assert state["pending_deliveries"] == []


def test_outbox_failure_records_backoff():
    state = {"pending_deliveries": []}
    delivery_id = enqueue_delivery(
        ["메시지"],
        state,
        kind="urgent",
        dedup_key="notice-1",
    )[0]
    now = datetime.fromisoformat("2026-08-01T10:00:00")
    record_delivery_failure(state, delivery_id, "temporary failure", now=now)
    item = state["pending_deliveries"][0]
    assert item["attempts"] == 1
    assert item["next_attempt_at"] == "2026-08-01T10:05:00"
    assert due_deliveries(state, now=now) == []


def test_classification_retry_uses_bounded_backoff():
    state = {"classification_retries": {}}
    now = datetime.fromisoformat("2026-08-01T10:00:00")
    schedule_classification_retry(state, "234:1", now=now)
    assert due_classification_retry_keys(state, now=now) == set()
    assert due_classification_retry_keys(
        state,
        now=datetime.fromisoformat("2026-08-01T11:00:00"),
    ) == {"234:1"}


def test_state_v3_migrates_profile_hash_without_personal_data(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(
        '{"schema_version":3,"seen_ids":{},"article_fingerprints":{},'
        '"enriched_fingerprints":{},"pending_digest":[],'
        '"pending_deliveries":[],"delivery_history":{},'
        '"classification_retries":{}}',
        encoding="utf-8",
    )
    state = load_state(str(path))
    assert state["schema_version"] == 4
    assert state["profile_document_hash"] is None
    assert "profile_snapshot" not in state
