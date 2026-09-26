"""수정 공지 감지와 일일 요약 큐 테스트."""

import json
from datetime import datetime

from ku_notice_monitor.state import (
    STATE_SCHEMA_VERSION,
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
    save_state,
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


def test_delivery_dedup_is_stable_when_rendered_text_changes():
    state = {"pending_deliveries": [], "delivery_history": {}}
    first_id = enqueue_delivery(
        ["새 공지 6건 중 관련 1건"],
        state,
        kind="urgent",
        dedup_key="notice-1",
    )[0]
    complete_delivery(state, first_id)

    second_ids = enqueue_delivery(
        ["새 공지 0건 중 관련 1건"],
        state,
        kind="urgent",
        dedup_key="notice-1",
    )

    assert second_ids == [first_id]
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
    assert state["schema_version"] == STATE_SCHEMA_VERSION
    assert state["profile_document_hash"] is None
    assert state["urgent_notice_history"] == {}
    assert "profile_snapshot" not in state


def test_state_v6_rebaselines_detail_fingerprints_without_flagging_updates(
    tmp_path,
    make_article,
):
    """본문 병합 방식이 바뀌어도 기존 공지가 한꺼번에 '수정됨'으로 잡히지 않는다."""
    article = make_article(id="1", description="RSS 요약")
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 5,
                "seen_ids": {article.key: datetime.now().isoformat()},
                "article_fingerprints": {article.key: article.fingerprint},
                "enriched_fingerprints": {article.key: "fingerprint-from-old-merge"},
            }
        ),
        encoding="utf-8",
    )

    state = load_state(str(path))
    result = filter_new_articles(
        [article],
        state,
        source_fingerprints={article.key: article.fingerprint},
        enriched_fingerprints={article.key: "fingerprint-from-new-merge"},
    )

    assert state["schema_version"] == STATE_SCHEMA_VERSION
    assert state["enriched_fingerprints"] == {}
    assert result == []
    # 이번 실행에서 새 기준을 저장하면 다음 수정부터 정상 감지한다.
    mark_as_seen(
        [article],
        state,
        fingerprints={article.key: article.fingerprint},
        enriched_fingerprints={article.key: "fingerprint-from-new-merge"},
    )
    assert state["enriched_fingerprints"][article.key] == "fingerprint-from-new-merge"


# --- 새 게시판 시드 ---


def test_seed_new_boards_marks_existing_posts_of_added_board(make_article):
    from ku_notice_monitor.state import seed_new_boards

    old = make_article(id="1", board_id=234)
    added = [make_article(id=str(i), board_id=775) for i in range(10, 13)]
    state = {"seen_ids": {old.key: "2026-09-20T00:00:00"}, "article_fingerprints": {}}

    seeded = seed_new_boards([old, *added], {234, 775}, state)

    assert seeded == {775: 3}
    assert all(article.key in state["seen_ids"] for article in added)
    assert state["known_boards"] == [234, 775]
    # 다음 실행에는 같은 게시판을 다시 시드하지 않는다.
    later = make_article(id="99", board_id=775)
    assert seed_new_boards([later], {234, 775}, state) == {}
    assert later.key not in state["seen_ids"]


def test_seed_new_boards_skips_failed_board_until_first_success(make_article):
    from ku_notice_monitor.state import seed_new_boards

    state = {"seen_ids": {"234:1": "2026-09-20T00:00:00"}, "article_fingerprints": {}}
    assert seed_new_boards([], {234}, state) == {}
    assert state["known_boards"] == [234]
    added = make_article(id="5", board_id=775)
    assert seed_new_boards([added], {234, 775}, state) == {775: 1}


def test_seed_new_boards_does_nothing_without_history(make_article):
    from ku_notice_monitor.state import seed_new_boards

    state = {"seen_ids": {}, "article_fingerprints": {}}
    article = make_article(id="1", board_id=775)
    assert seed_new_boards([article], {775}, state) == {}
    assert article.key not in state["seen_ids"]
    assert state["known_boards"] == [775]


# --- 게시판 간 중복 ---


def test_cross_board_duplicate_in_same_run_is_dropped(make_article):
    from ku_notice_monitor.state import drop_cross_board_duplicates

    main = make_article(id="1", board_id=238, title="[융합혁신교육센터] 교육만족도 조사 안내")
    dept = make_article(id="2", board_id=775, title="교육만족도 조사 안내")
    other = make_article(id="3", board_id=775, title="중간 강의평가 시행 안내")
    state: dict = {}
    kept = drop_cross_board_duplicates([main, dept, other], state)
    assert kept == [main, other]


def test_repost_of_previously_seen_notice_is_dropped(make_article):
    from ku_notice_monitor.state import drop_cross_board_duplicates

    seen = make_article(id="1", board_id=234, title="2학기 최종마감등록 안내")
    state: dict = {}
    assert drop_cross_board_duplicates([], state, current_articles=[seen]) == []
    repost = make_article(id="9", board_id=775, title="2학기 최종마감등록 안내")
    assert drop_cross_board_duplicates([repost], state, current_articles=[seen, repost]) == []


def test_same_board_repost_and_updates_are_kept(make_article):
    from ku_notice_monitor.state import drop_cross_board_duplicates

    first = make_article(id="1", board_id=234, title="수강신청 안내")
    state: dict = {}
    drop_cross_board_duplicates([first], state)
    again = make_article(id="2", board_id=234, title="수강신청 안내")
    updated = make_article(id="3", board_id=775, title="수강신청 안내", is_update=True)
    assert drop_cross_board_duplicates([again, updated], state) == [again, updated]


def test_state_v6_migrates_followup_records(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 6,
                "seen_ids": {},
                "weekly_report": "broken",
                "telegram_update_offset": "12",
            }
        ),
        encoding="utf-8",
    )
    state = load_state(str(path))
    assert state["schema_version"] == STATE_SCHEMA_VERSION
    assert state["tracked_notices"] == {}
    assert state["weekly_report"] is None
    assert state["telegram_update_offset"] is None


def test_state_drops_malformed_tracked_notices(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps({"schema_version": 7, "tracked_notices": {"a": {"key": "234:1"}, "b": "x", "c": {}}}),
        encoding="utf-8",
    )
    assert list(load_state(str(path))["tracked_notices"]) == ["a"]


def test_save_state_prunes_expired_tracked_notices(tmp_path):
    state = {
        "seen_ids": {},
        "tracked_notices": {
            "old": {"key": "234:1", "keep_until": "2000-01-01"},
            "live": {"key": "234:2", "keep_until": "2999-01-01"},
        },
    }
    save_state(state, str(tmp_path / "state.json"))
    assert list(state["tracked_notices"]) == ["live"]


def test_reply_markup_is_attached_to_last_part_only():
    state: dict = {}
    markup = {"inline_keyboard": [[{"text": "✅", "callback_data": "d:1"}]]}
    enqueue_delivery(["one", "two"], state, kind="urgent", dedup_key="k", reply_markup=markup)
    first, last = state["pending_deliveries"]
    assert "reply_markup" not in first
    assert last["reply_markup"] == markup
