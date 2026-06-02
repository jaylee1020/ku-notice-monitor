"""matcher.py 단위 테스트"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from matcher import (
    _extension_of,
    _guess_attachment_mime_type,
    _guess_mime_type,
    _is_retryable_gemini_error,
    _parse_gemini_json,
    analyze_with_gemini,
    build_profile_text,
    build_prompt,
    keyword_fallback,
    match_articles,
)
from models import Attachment

# --- build_profile_text ---


def test_build_profile_text_full():
    config = {
        "profile": {"major": "컴퓨터공학부", "year": 2, "campus": "서울", "status": "재학"},
        "keywords": {"high": ["장학"], "medium": ["취업"]},
    }
    text = build_profile_text(config)
    assert "컴퓨터공학부" in text
    assert "장학" in text


def test_build_profile_text_empty():
    config = {"profile": {}, "keywords": {}}
    text = build_profile_text(config)
    assert "프로필 미설정" in text


# --- build_prompt ---


def test_build_prompt_contains_articles(make_article):
    articles = [make_article(title="장학금 공지", board_name="장학공지")]
    prompt = build_prompt(articles, "테스트 프로필")
    assert "장학금 공지" in prompt
    assert "장학공지" in prompt
    assert "테스트 프로필" in prompt
    assert "JSON" in prompt


def test_build_prompt_with_images(make_article):
    articles = [make_article(title="포스터 공지", images=["https://example.com/img.jpg"])]
    prompt = build_prompt(articles, "테스트 프로필")
    assert "이미지 1장" in prompt
    assert "첨부된 파일의 내용도 함께 분석" in prompt


def test_build_prompt_without_media(make_article):
    articles = [make_article(title="텍스트 공지")]
    prompt = build_prompt(articles, "테스트 프로필")
    assert "이미지" not in prompt
    assert "첨부파일" not in prompt


def test_build_prompt_with_attachments(make_article):
    att = Attachment(filename="장학금안내.hwp", url="https://example.com/download.do")
    articles = [make_article(title="장학금 공지", attachments=[att])]
    prompt = build_prompt(articles, "테스트 프로필")
    assert "장학금안내.hwp" in prompt
    assert "첨부된 파일의 내용도 함께 분석" in prompt


def test_build_prompt_with_images_and_attachments(make_article):
    att = Attachment(filename="양식.pdf", url="https://example.com/download.do")
    articles = [make_article(
        title="공지",
        images=["https://example.com/img.jpg"],
        attachments=[att],
    )]
    prompt = build_prompt(articles, "테스트 프로필")
    assert "이미지 1장" in prompt
    assert "양식.pdf" in prompt


# --- _guess_mime_type ---


@pytest.mark.parametrize("url,expected", [
    ("https://example.com/photo.jpg", "image/jpeg"),
    ("https://example.com/photo.png", "image/png"),
    ("https://example.com/image", "image/jpeg"),
    ("https://example.com/photo.png?w=100", "image/png"),
])
def test_guess_mime_type(url, expected):
    assert _guess_mime_type(url) == expected


# --- _parse_gemini_json ---


def test_parse_gemini_json_clean():
    text = '[{"index": 1, "score": 5, "reason": "test"}]'
    result = _parse_gemini_json(text)
    assert len(result) == 1
    assert result[0]["score"] == 5


def test_parse_gemini_json_with_code_fence():
    text = '```json\n[{"index": 1, "score": 3, "reason": "test"}]\n```'
    result = _parse_gemini_json(text)
    assert len(result) == 1


def test_parse_gemini_json_not_array():
    with pytest.raises(ValueError, match="JSON 배열"):
        _parse_gemini_json('{"index": 1}')


def test_parse_gemini_json_missing_fields():
    with pytest.raises(ValueError, match="필수 필드"):
        _parse_gemini_json('[{"index": 1}]')


def test_parse_gemini_json_invalid_json():
    with pytest.raises(Exception):
        _parse_gemini_json("not json at all")


def test_parse_gemini_json_none_raises():
    with pytest.raises(ValueError, match="비어 있습니다"):
        _parse_gemini_json(None)


def test_parse_gemini_json_oneline_code_fence():
    text = '```[{"index": 1, "score": 4, "reason": "test"}]```'
    result = _parse_gemini_json(text)
    assert len(result) == 1
    assert result[0]["score"] == 4


# --- keyword_fallback ---


def test_keyword_fallback_high_match(make_article):
    articles = [make_article(title="장학금 신청 안내")]
    config = {"keywords": {"high": ["장학"], "medium": []}}
    results = keyword_fallback(articles, config)
    assert results[0]["score"] == 4
    assert "장학" in results[0]["reason"]


def test_keyword_fallback_medium_match(make_article):
    articles = [make_article(title="취업 박람회")]
    config = {"keywords": {"high": ["장학"], "medium": ["취업"]}}
    results = keyword_fallback(articles, config)
    assert results[0]["score"] == 3


def test_keyword_fallback_no_match(make_article):
    articles = [make_article(title="기숙사 청소 안내")]
    config = {"keywords": {"high": ["장학"], "medium": ["취업"]}}
    results = keyword_fallback(articles, config)
    assert results[0]["score"] == 1


def test_keyword_fallback_matches_attachment_filename(make_article):
    att = Attachment(filename="장학금신청양식.hwp", url="https://example.com/download.do")
    articles = [make_article(title="서류 제출 안내", attachments=[att])]
    config = {"keywords": {"high": ["장학"], "medium": []}}
    results = keyword_fallback(articles, config)
    assert results[0]["score"] == 4
    assert "장학" in results[0]["reason"]


# --- _guess_attachment_mime_type ---


@pytest.mark.parametrize("filename,expected", [
    # 이미지
    ("file.pdf", "application/pdf"),
    ("file.jpg", "image/jpeg"),
    ("file.jpeg", "image/jpeg"),
    ("file.png", "image/png"),
    ("file.webp", "image/webp"),
    ("file.heic", "image/heic"),
    # 비디오
    ("clip.mp4", "video/mp4"),
    ("clip.mov", "video/mov"),
    ("clip.webm", "video/webm"),
    # 오디오
    ("sound.mp3", "audio/mp3"),
    ("sound.wav", "audio/wav"),
    ("sound.ogg", "audio/ogg"),
    ("sound.m4a", "audio/mp4"),
    # 텍스트
    ("readme.txt", "text/plain"),
    ("doc.md", "text/md"),
    ("data.csv", "text/csv"),
    ("page.html", "text/html"),
    ("data.json", "application/json"),
])
def test_guess_attachment_mime_type_gemini_native(filename, expected):
    """Gemini inline 지원 포맷은 환경에 관계없이 고정 매핑을 사용한다."""
    assert _guess_attachment_mime_type(filename) == expected


def test_guess_attachment_mime_type_unknown_extension():
    # .hwp 등 Gemini 미지원 확장자는 시스템 mimetypes DB에 따라 달라짐.
    # 핵심은 빈 문자열이 아닌 유효한 MIME 문자열을 반환하는 것.
    result = _guess_attachment_mime_type("file.hwp")
    assert isinstance(result, str) and "/" in result and result


def test_guess_mime_type_handles_query_string():
    """URL 쿼리스트링이 있어도 확장자를 올바르게 감지"""
    assert _guess_mime_type("https://example.com/photo.webp?v=123") == "image/webp"
    assert _guess_mime_type("https://example.com/photo.png#frag") == "image/png"


@pytest.mark.parametrize("name,expected", [
    ("photo.JPG", ".jpg"),
    ("doc.PDF?v=1", ".pdf"),
    ("file.tar.gz", ".gz"),
    ("no-extension", ""),
    ("https://example.com/path/file.webm#t=10", ".webm"),
])
def test_extension_of(name, expected):
    assert _extension_of(name) == expected


# --- match_articles ---


def test_match_articles_gemini_success(make_article):
    articles = [make_article(title="장학금"), make_article(id="2", title="기숙사")]
    config = {"gemini": {"model": "test", "relevance_threshold": 3}, "profile": {}, "keywords": {}}
    mock_results = [
        {"index": 1, "score": 5, "reason": "장학 관련"},
        {"index": 2, "score": 1, "reason": "무관"},
    ]
    with patch("matcher.analyze_with_gemini", new_callable=AsyncMock, return_value=mock_results):
        matched, method = asyncio.get_event_loop().run_until_complete(match_articles(articles, config))
    assert len(matched) == 1
    assert matched[0][1] == 5
    assert method == "gemini"


def test_match_articles_gemini_fail_falls_back(make_article):
    articles = [make_article(title="장학금 안내")]
    config = {
        "gemini": {"model": "test", "relevance_threshold": 3},
        "profile": {},
        "keywords": {"high": ["장학"], "medium": []},
    }
    with patch("matcher.analyze_with_gemini", new_callable=AsyncMock, return_value=[]):
        matched, method = asyncio.get_event_loop().run_until_complete(match_articles(articles, config))
    assert method == "keyword"
    assert len(matched) == 1


def test_match_articles_empty():
    matched, method = asyncio.get_event_loop().run_until_complete(
        match_articles([], {"gemini": {"relevance_threshold": 3}})
    )
    assert matched == []
    assert method == "none"


def test_match_articles_gemini_string_score_and_invalid_entries(make_article):
    articles = [make_article(title="장학금")]
    config = {"gemini": {"model": "test", "relevance_threshold": 3}, "profile": {}, "keywords": {}}
    mock_results = [
        {"index": "1", "score": "5", "reason": "문자열 점수"},
        {"index": "x", "score": 5, "reason": "잘못된 index"},
        {"index": 1, "score": "bad", "reason": "잘못된 score"},
    ]
    with patch("matcher.analyze_with_gemini", new_callable=AsyncMock, return_value=mock_results):
        matched, method = asyncio.get_event_loop().run_until_complete(match_articles(articles, config))

    assert method == "gemini"
    assert len(matched) == 1
    assert matched[0][1] == 5


# --- _is_retryable_gemini_error ---


def test_is_retryable_gemini_error_network():
    assert _is_retryable_gemini_error(asyncio.TimeoutError()) is True
    assert _is_retryable_gemini_error(ConnectionError()) is True
    assert _is_retryable_gemini_error(TimeoutError()) is True


def test_is_retryable_gemini_error_value_errors():
    # JSON/스키마 오류는 재시도하지 않음
    assert _is_retryable_gemini_error(ValueError("bad json")) is False
    assert _is_retryable_gemini_error(KeyError("missing")) is False
    assert _is_retryable_gemini_error(TypeError("bad")) is False


def test_is_retryable_gemini_error_unknown_exception_not_retried():
    # 분류되지 않은 예외는 무한 재시도 방지를 위해 기본 False
    assert _is_retryable_gemini_error(RuntimeError("unknown")) is False


# --- analyze_with_gemini 배치 index 오프셋 회귀 테스트 ---


def test_analyze_with_gemini_multi_batch_index_offset(make_article, monkeypatch):
    """2개 이상의 배치로 나뉠 때 전역 index가 올바르게 계산되는지 검증 (_extract_matched와 함께 동작)."""
    from matcher import GEMINI_BATCH_SIZE, _extract_matched

    # 25개 기사 → 3개 배치 (10/10/5)
    articles = [make_article(id=str(i), title=f"공지 {i}") for i in range(25)]
    # 각 배치별로 1번째 항목에 score=5, 나머지는 1을 부여
    call_log: list[int] = []

    async def fake_analyze_batch(client, model_name, batch, config):
        call_log.append(len(batch))
        return [
            {"index": i + 1, "score": 5 if i == 0 else 1, "reason": "test"}
            for i in range(len(batch))
        ]

    monkeypatch.setenv("GEMINI_API_KEY", "dummy")
    import matcher
    monkeypatch.setattr(matcher, "_analyze_batch", fake_analyze_batch)
    # genai.Client는 MagicMock이므로 그대로 둠

    config = {"gemini": {"model": "test", "relevance_threshold": 5}, "profile": {}, "keywords": {}}
    results = asyncio.get_event_loop().run_until_complete(analyze_with_gemini(articles, config))

    # 3개 배치가 호출되었는지 확인
    assert call_log == [GEMINI_BATCH_SIZE, GEMINI_BATCH_SIZE, 5]

    # threshold 5 이상인 항목만 추출 → 각 배치 첫 항목 (전역 index 1, 11, 21)
    matched, valid = _extract_matched(results, articles, threshold=5)
    matched_ids = sorted(a.id for a, _, _ in matched)
    assert matched_ids == ["0", "10", "20"], f"배치 오프셋 계산 오류: {matched_ids}"
    assert valid == 25


def test_analyze_with_gemini_partial_batch_failure_uses_keyword_for_failed_batch(make_article, monkeypatch):
    """일부 배치만 실패하면 성공 배치는 유지하고, 실패 배치만 키워드 매칭으로 대체한다."""
    from matcher import _extract_matched

    # 15개 → 2개 배치 (10/5). 두 번째 배치만 실패시킨다.
    articles = [make_article(id=str(i), title=f"장학 공지 {i}") for i in range(15)]

    async def fake_analyze_batch(client, model_name, batch, config):
        if len(batch) == 5:  # 두 번째 배치
            raise RuntimeError("Gemini 호출 실패")
        return [{"index": i + 1, "score": 5, "reason": "gemini"} for i in range(len(batch))]

    monkeypatch.setenv("GEMINI_API_KEY", "dummy")
    import matcher
    monkeypatch.setattr(matcher, "_analyze_batch", fake_analyze_batch)

    config = {
        "gemini": {"model": "test", "relevance_threshold": 3},
        "profile": {},
        "keywords": {"high": ["장학"], "medium": []},
    }
    results = asyncio.get_event_loop().run_until_complete(analyze_with_gemini(articles, config))

    # 15개 모두 점수가 매겨져야 한다 (성공 배치 10 + 키워드 폴백 5)
    matched, valid = _extract_matched(results, articles, threshold=3)
    assert valid == 15
    matched_ids = sorted((int(a.id) for a, _, _ in matched))
    assert matched_ids == list(range(15)), f"누락된 공지가 있음: {matched_ids}"


def test_analyze_with_gemini_all_batches_fail_returns_empty(make_article, monkeypatch):
    """모든 배치가 실패하면 빈 리스트를 반환해 호출부의 전체 키워드 폴백으로 넘긴다."""
    articles = [make_article(id=str(i), title=f"공지 {i}") for i in range(3)]

    async def always_fail(client, model_name, batch, config):
        raise RuntimeError("실패")

    monkeypatch.setenv("GEMINI_API_KEY", "dummy")
    import matcher
    monkeypatch.setattr(matcher, "_analyze_batch", always_fail)

    config = {"gemini": {"model": "test", "relevance_threshold": 3}, "profile": {}, "keywords": {}}
    results = asyncio.get_event_loop().run_until_complete(analyze_with_gemini(articles, config))
    assert results == []


def test_match_articles_gemini_invalid_results_fallback_to_keyword(make_article):
    articles = [make_article(title="장학금 안내")]
    config = {
        "gemini": {"model": "test", "relevance_threshold": 3},
        "profile": {},
        "keywords": {"high": ["장학"], "medium": []},
    }
    mock_results = [{"index": "x", "score": "bad", "reason": "형식 오류"}]
    with patch("matcher.analyze_with_gemini", new_callable=AsyncMock, return_value=mock_results):
        matched, method = asyncio.get_event_loop().run_until_complete(match_articles(articles, config))

    assert method == "keyword"
    assert len(matched) == 1
