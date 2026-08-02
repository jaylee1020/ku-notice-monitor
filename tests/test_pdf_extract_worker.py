"""pdf-inspector 격리 워커의 안전한 라우팅 기준 테스트."""

from types import SimpleNamespace

from ku_notice_monitor.pdf_extract_worker import _can_use_local_markdown


def _result(**overrides):
    values = {
        "pdf_type": "text_based",
        "confidence": 0.99,
        "pages_needing_ocr": [],
        "has_encoding_issues": False,
        "markdown": "# 공지",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_high_confidence_text_pdf_uses_markdown():
    assert _can_use_local_markdown(_result()) is True


def test_uncertain_or_partial_pdf_uses_native_fallback():
    assert _can_use_local_markdown(_result(confidence=0.94)) is False
    assert _can_use_local_markdown(_result(pdf_type="mixed")) is False
    assert _can_use_local_markdown(_result(pages_needing_ocr=[2])) is False
    assert _can_use_local_markdown(_result(has_encoding_issues=True)) is False
    assert _can_use_local_markdown(_result(markdown="")) is False
