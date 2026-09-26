"""테스트 공유 픽스처."""

import pytest

from ku_notice_monitor.models import Article, Attachment, ClassifiedNotice


@pytest.fixture
def make_article():
    """Article 팩토리 픽스처. 기본값을 오버라이드하여 테스트용 Article 생성."""
    def _make(**overrides) -> Article:
        defaults = dict(
            id="1",
            title="테스트",
            link="https://example.com",
            pub_date="",
            author="",
            description="",
            board_name="테스트게시판",
            board_id=234,
            view_count=0,
            is_pinned=False,
            attachment_count=0,
        )
        defaults.update(overrides)
        return Article(**defaults)
    return _make


@pytest.fixture
def make_attachment():
    """Attachment 팩토리 픽스처."""
    def _make(filename: str = "test.pdf", url: str = "https://example.com/download.do") -> Attachment:
        return Attachment(filename=filename, url=url)
    return _make


@pytest.fixture
def make_classified(make_article):
    """새 분류 결과 팩토리."""
    def _make(**overrides) -> ClassifiedNotice:
        article = overrides.pop("article", make_article())
        defaults = dict(
            article=article,
            delivery="digest",
            category="other",
            summary=article.title,
            reason="관심 공지",
        )
        defaults.update(overrides)
        return ClassifiedNotice(**defaults)
    return _make


@pytest.fixture(autouse=True)
def _no_real_telegram(monkeypatch):
    """개발자 환경의 실제 봇 설정으로 테스트가 텔레그램을 호출하지 않게 한다."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
