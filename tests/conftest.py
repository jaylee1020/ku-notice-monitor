"""테스트 공통 설정 - 환경 호환성을 위한 모듈 모킹 및 공유 픽스처"""

import sys
from unittest.mock import MagicMock

import pytest

# google.genai와 telegram 모듈이 설치되지 않았거나 로드 불가한 환경에서도
# 테스트가 실행될 수 있도록 mock 처리
for mod_name in [
    "google",
    "google.genai",
    "telegram",
    "feedparser",
    "aiohttp",
    "certifi",
]:
    if mod_name not in sys.modules:
        sys.modules[mod_name] = MagicMock()

from models import Article  # noqa: E402


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
