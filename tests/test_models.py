"""models.py 단위 테스트"""

import pytest

from models import Attachment, ClassifiedNotice


@pytest.mark.parametrize("filename,expected_ext", [
    ("문서.hwp", ".hwp"),
    ("파일.PDF", ".pdf"),
    ("양식.hwpx", ".hwpx"),
    ("image.JPG", ".jpg"),
    ("noext", ""),
])
def test_attachment_ext(filename, expected_ext):
    att = Attachment(filename=filename, url="https://example.com/download.do")
    assert att.ext == expected_ext


def test_article_fingerprint_changes_with_content(make_article):
    original = make_article(description="원문")
    changed = make_article(description="수정된 원문")
    assert original.fingerprint != changed.fingerprint


def test_classified_notice_round_trip(make_article):
    match = ClassifiedNotice(
        article=make_article(title="장학 공지"),
        delivery="immediate",
        category="scholarship",
        summary="신청 필요",
        reason="직접 관련",
        audience_fit="eligible",
        interest_fit="high",
        consequence="missed_opportunity",
        deadline="2026-08-10",
        actions=["서류 제출"],
    )
    restored = ClassifiedNotice.from_dict(match.to_dict())
    assert restored == match


def test_classified_notice_reads_legacy_state(make_article):
    legacy = {
        "article": make_article(title="옛 공지").to_dict(),
        "score": 5,
        "reason": "이전 결과",
        "summary": "옛 요약",
        "urgency": "urgent",
        "deadline": None,
        "actions": [],
    }
    restored = ClassifiedNotice.from_dict(legacy)
    assert restored.delivery == "immediate"
    assert restored.source == "legacy"
