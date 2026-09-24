"""여러 모듈이 공유하는 시간·텍스트 도우미."""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

# GitHub Actions 러너는 UTC이므로 사용자 기준 날짜 판단은 항상 KST로 한다.
KST = ZoneInfo("Asia/Seoul")

_NON_WORD = re.compile(r"[^0-9a-z가-힣]+")


def now_kst() -> datetime:
    return datetime.now(KST)


def normalize_for_match(value: str) -> str:
    """공백·구두점·대소문자 차이를 무시하고 원문 포함 여부를 비교하기 위해 정규화한다."""
    return _NON_WORD.sub("", value.lower())


def is_grounded(quote: str, normalized_source: str, *, min_length: int = 2) -> bool:
    """인용이 정규화된 원문에 실제로 들어 있는지 확인한다.

    구두점만 있는 인용은 정규화하면 빈 문자열이 되어 어디에나 포함되므로 거부한다.
    """
    normalized = normalize_for_match(quote)
    return len(normalized) >= min_length and normalized in normalized_source
