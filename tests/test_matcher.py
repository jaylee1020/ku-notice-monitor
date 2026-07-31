"""축 분리형 공지 분류와 전달 정책 테스트."""

import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from ku_notice_monitor import openai_classifier
from ku_notice_monitor.analysis_models import NoticeAssessment, NoticeDate
from ku_notice_monitor.classification import Delivery, classify_assessment, decide_delivery
from ku_notice_monitor.matcher import keyword_fallback, match_articles, validate_assessment_grounding
from ku_notice_monitor.models import Attachment
from ku_notice_monitor.openai_classifier import (
    MediaPayload,
    _build_input_content,
    _classify_one,
    _extension_of,
    _guess_mime_type,
    _is_retryable_openai_error,
    _media_items,
)
from ku_notice_monitor.prompts import build_profile_text, build_prompt, select_relevant_excerpt


def _config(**ai_overrides):
    ai = {
        "model": "gpt-5.6-luna",
        "reasoning_effort": "low",
        "max_concurrency": 4,
        "image_detail": "low",
        "file_detail": "low",
    }
    ai.update(ai_overrides)
    return {
        "ai": ai,
        "classification": {"action_window_days": 21},
        "profile": {
            "major": "컴퓨터공학부",
            "previous_major": "KU자유전공학부",
            "year": 2,
        },
        "keywords": {"high": ["장학", "수강신청"], "medium": ["인턴"]},
        "settings": {"ssl_verify": True},
    }


def _assessment(**overrides) -> NoticeAssessment:
    data = {
        "category": "other",
        "summary": "공지 요약",
        "audience_fit": "eligible",
        "audience_reason": "전체 재학생 대상",
        "interest_fit": "low",
        "interest_reason": "관심사 불일치",
        "obligation": "none",
        "consequence": "none",
        "dates": [],
        "actions": [],
        "benefits": [],
        "evidence": ["전체 재학생"],
        "uncertainties": [],
        "attachment_need": "not_needed",
    }
    data.update(overrides)
    return NoticeAssessment.model_validate(data)


def test_build_profile_text_contains_profile_and_keywords():
    text = build_profile_text(_config())
    assert "컴퓨터공학부" in text
    assert "KU자유전공학부" in text
    assert "장학" in text


def test_build_prompt_is_single_notice_and_marks_attachment_pass(make_article):
    article = make_article(
        title="수강신청 변경 안내",
        description="8월 10일까지 신청",
        is_update=True,
        attachments=[Attachment("안내.pdf", "https://example.com/a.pdf")],
    )
    prompt = build_prompt(article, "테스트 프로필", attachments_included=True)
    assert "수강신청 변경 안내" in prompt
    assert "기존 공지 수정본" in prompt
    assert "안내.pdf" in prompt
    assert "직접 확인" in prompt
    assert "<notice_content>" in prompt


def test_prompt_excerpt_preserves_head_critical_window_and_tail():
    body = "앞부분 " + ("일반 내용 " * 800) + "신청 마감 2026-08-10 " + ("기타 " * 800) + "맨 끝 자격 조건"
    excerpt = select_relevant_excerpt(body, limit=1000)
    assert excerpt.startswith("[본문 앞부분]")
    assert "신청 마감 2026-08-10" in excerpt
    assert "맨 끝 자격 조건" in excerpt
    assert len(excerpt) <= 1000


def test_notice_assessment_keeps_axes_separate():
    value = _assessment(
        category="scholarship",
        audience_fit="possibly_eligible",
        interest_fit="high",
        obligation="optional",
        consequence="missed_opportunity",
    )
    assert value.audience_fit.value == "possibly_eligible"
    assert value.interest_fit.value == "high"


def test_notice_date_rejects_impossible_date():
    with pytest.raises(ValidationError):
        NoticeDate(kind="application_deadline", date="2026-02-30", label="마감")


def test_policy_immediate_for_eligible_high_impact():
    delivery, reason = decide_delivery(
        _assessment(consequence="academic_risk"),
        today=date(2026, 8, 1),
    )
    assert delivery == Delivery.IMMEDIATE
    assert "손실" in reason


def test_policy_review_for_unknown_high_impact():
    delivery, _ = decide_delivery(
        _assessment(audience_fit="unknown", consequence="financial_loss"),
        today=date(2026, 8, 1),
    )
    assert delivery == Delivery.REVIEW


def test_policy_suppresses_explicitly_ineligible_notice():
    delivery, _ = decide_delivery(
        _assessment(audience_fit="ineligible", interest_fit="high"),
        today=date(2026, 8, 1),
    )
    assert delivery == Delivery.SUPPRESS


def test_policy_digest_for_interest_without_obligation():
    delivery, _ = decide_delivery(
        _assessment(interest_fit="medium"),
        today=date(2026, 8, 1),
    )
    assert delivery == Delivery.DIGEST


def test_policy_uses_deadline_window_not_relevance_score():
    assessment = _assessment(
        obligation="required",
        dates=[
            {
                "kind": "application_deadline",
                "date": "2026-08-10",
                "label": "신청 마감",
            }
        ],
    )
    delivery, _ = decide_delivery(
        assessment,
        today=date(2026, 8, 1),
        action_window_days=21,
    )
    assert delivery == Delivery.IMMEDIATE


def test_classify_assessment_builds_domain_result(make_article):
    result = classify_assessment(
        make_article(title="장학금"),
        _assessment(
            category="scholarship",
            interest_fit="high",
            consequence="missed_opportunity",
        ),
        source="openai",
        today=date(2026, 8, 1),
    )
    assert result.delivery == "digest"
    assert result.category == "scholarship"
    assert result.score == 3


def test_keyword_fallback_is_conservative(make_article):
    result = keyword_fallback(
        make_article(
            title="수강신청 마감 안내",
            description="2026-08-10까지 신청",
        ),
        _config(),
    )
    assert result.consequence.value == "academic_risk"
    assert result.audience_fit.value == "unknown"
    assert result.uncertainties


def test_match_articles_openai_success(make_article):
    article = make_article(title="인턴 모집")
    raw = _assessment(
        category="career",
        interest_fit="medium",
        consequence="missed_opportunity",
    ).model_dump(mode="json")
    with patch(
        "ku_notice_monitor.matcher.analyze_with_openai",
        new_callable=AsyncMock,
        return_value={article.key: raw},
    ):
        matched, method = asyncio.run(match_articles([article], _config()))
    assert method == "openai"
    assert matched[0].delivery == "digest"


def test_match_articles_partially_falls_back(make_article):
    openai_article = make_article(id="1", title="인턴 모집")
    failed_article = make_article(id="2", title="수강신청 안내")
    raw = _assessment(
        category="career",
        interest_fit="medium",
        consequence="missed_opportunity",
    ).model_dump(mode="json")
    with patch(
        "ku_notice_monitor.matcher.analyze_with_openai",
        new_callable=AsyncMock,
        return_value={openai_article.key: raw},
    ):
        matched, method = asyncio.run(
            match_articles([openai_article, failed_article], _config())
        )
    assert method == "openai+rules"
    assert any(item.source == "rules" for item in matched)


def test_match_articles_empty():
    matched, method = asyncio.run(match_articles([], _config()))
    assert matched == []
    assert method == "none"


@pytest.mark.parametrize(
    "name,expected",
    [
        ("file.PDF?download=1", ".pdf"),
        ("image.JPG#x", ".jpg"),
        ("no-extension", ""),
    ],
)
def test_extension_of(name, expected):
    assert _extension_of(name) == expected


def test_guess_mime_type_uses_stable_mapping():
    assert _guess_mime_type("notice.pdf") == "application/pdf"
    assert _guess_mime_type("photo.JPG", image=True) == "image/jpeg"


def test_hwp_attachments_are_selected_for_safe_conversion(make_article):
    article = make_article(
        attachments=[
            Attachment("지원서.hwp", "https://www.konkuk.ac.kr/a.hwp"),
            Attachment("안내.hwpx", "https://www.konkuk.ac.kr/b.hwpx"),
        ]
    )
    items = _media_items(article)
    assert [item[3] for item in items] == ["hwp", "hwp"]


def test_build_input_content_uses_low_detail():
    media = [
        MediaPayload("photo.jpg", "image/jpeg", b"image", "image"),
        MediaPayload("guide.pdf", "application/pdf", b"pdf", "file"),
    ]
    content = _build_input_content("prompt", media, _config())
    image = next(item for item in content if item["type"] == "input_image")
    file_item = next(item for item in content if item["type"] == "input_file")
    assert image["detail"] == "low"
    assert image["image_url"].startswith("data:image/jpeg;base64,")
    assert file_item["detail"] == "low"
    assert file_item["file_data"].startswith("data:application/pdf;base64,")


class _StatusError(Exception):
    def __init__(self, status_code):
        self.status_code = status_code


def test_retryable_openai_error():
    assert _is_retryable_openai_error(_StatusError(429)) is True
    assert _is_retryable_openai_error(_StatusError(503)) is True
    assert _is_retryable_openai_error(_StatusError(401)) is False
    assert _is_retryable_openai_error(ValueError("bad schema")) is False


def test_ungrounded_ineligible_is_changed_to_unknown(make_article):
    assessment = _assessment(
        audience_fit="ineligible",
        audience_reason="대학원생만 가능",
        evidence=["대학원 재학생만 지원 가능"],
    )
    grounded = validate_assessment_grounding(
        make_article(title="전체 학생 대상 프로그램", description="재학생 신청 가능"),
        assessment,
    )
    assert grounded.audience_fit.value == "unknown"
    assert grounded.evidence == []
    assert grounded.uncertainties


def test_hallucinated_dates_are_removed(make_article):
    assessment = _assessment(
        dates=[
            {
                "kind": "application_deadline",
                "date": "2026-09-30",
                "label": "신청 마감",
            }
        ]
    )
    grounded = validate_assessment_grounding(
        make_article(description="신청 일정은 추후 공지"),
        assessment,
    )
    assert grounded.dates == []
    assert any("날짜" in item for item in grounded.uncertainties)


def test_call_openai_api_returns_parsed_schema():
    parsed = _assessment(interest_fit="medium")
    response = SimpleNamespace(
        output_parsed=parsed,
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15),
    )
    client = SimpleNamespace(
        responses=SimpleNamespace(parse=AsyncMock(return_value=response))
    )
    metrics = {}
    result = asyncio.run(
        openai_classifier._call_openai_api(
            client,
            model_name="gpt-5.6-luna",
            reasoning_effort="low",
            content=[{"type": "input_text", "text": "test"}],
            metrics=metrics,
        )
    )
    assert result.interest_fit.value == "medium"
    kwargs = client.responses.parse.await_args.kwargs
    assert kwargs["model"] == "gpt-5.6-luna"
    assert kwargs["text_format"] is NoticeAssessment
    assert kwargs["store"] is False
    assert metrics == {
        "request_attempts": 1,
        "successful_calls": 1,
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "cached_input_tokens": 0,
    }


def test_classify_one_only_loads_attachments_when_required(make_article):
    article = make_article(
        attachments=[Attachment("guide.pdf", "https://example.com/guide.pdf")]
    )
    first = _assessment(attachment_need="required")
    second = _assessment(attachment_need="not_needed", evidence=["첨부 확인"])
    with patch(
        "ku_notice_monitor.openai_classifier._analyze_article",
        new_callable=AsyncMock,
        side_effect=[first, second],
    ) as analyze:
        result = asyncio.run(
            _classify_one(
                object(),
                article,
                _config(),
                asyncio.Semaphore(1),
            )
        )
    assert result is not None
    assert analyze.await_count == 2
    assert analyze.await_args_list[0].kwargs["include_media"] is False
    assert analyze.await_args_list[1].kwargs["include_media"] is True


def test_analyze_with_openai_without_key(make_article, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = asyncio.run(
        openai_classifier.analyze_with_openai([make_article()], _config())
    )
    assert result == {}


def test_analyze_with_openai_returns_results_by_article_key(
    make_article,
    monkeypatch,
):
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    articles = [make_article(id="1"), make_article(id="2")]

    async def fake_classify(client, article, config, semaphore, metrics=None):
        return article.key, _assessment().model_dump(mode="json")

    monkeypatch.setattr(openai_classifier, "_classify_one", fake_classify)
    monkeypatch.setattr(openai_classifier, "AsyncOpenAI", lambda **kwargs: object())
    results = asyncio.run(
        openai_classifier.analyze_with_openai(articles, _config())
    )
    assert set(results) == {article.key for article in articles}
