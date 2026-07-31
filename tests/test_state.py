"""수정 공지 감지와 일일 요약 큐 테스트."""

from state import (
    clear_pending_digest,
    enqueue_digest,
    filter_new_articles,
    get_pending_digest,
    mark_as_seen,
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
