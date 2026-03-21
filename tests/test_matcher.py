"""matcher.py 단위 테스트"""

import asyncio
from unittest.mock import AsyncMock, patch

from matcher import _guess_mime_type, build_profile_text, build_prompt, keyword_fallback, match_articles

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
    assert "이미지 1장 첨부" in prompt
    assert "이미지의 내용도 함께 분석" in prompt


def test_build_prompt_without_images(make_article):
    articles = [make_article(title="텍스트 공지")]
    prompt = build_prompt(articles, "테스트 프로필")
    assert "이미지" not in prompt


# --- _guess_mime_type ---


def test_guess_mime_type_jpeg():
    assert _guess_mime_type("https://example.com/photo.jpg") == "image/jpeg"


def test_guess_mime_type_png():
    assert _guess_mime_type("https://example.com/photo.png") == "image/png"


def test_guess_mime_type_unknown_defaults_jpeg():
    assert _guess_mime_type("https://example.com/image") == "image/jpeg"


def test_guess_mime_type_with_query_params():
    assert _guess_mime_type("https://example.com/photo.png?w=100") == "image/png"


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
