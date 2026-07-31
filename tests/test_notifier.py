"""notifier.py 단위 테스트"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from ku_notice_monitor.constants import MAX_TELEGRAM_MESSAGE_LENGTH
from ku_notice_monitor.notifier import (
    TelegramDeliveryError,
    build_digest_message,
    build_error_message,
    build_first_run_message,
    build_no_new_message,
    build_no_relevant_message,
    build_relevant_message,
    build_urgent_message,
    send_telegram,
    split_message,
)

# --- build_relevant_message ---


def test_build_relevant_message(make_article, make_classified):
    matched = [
        make_classified(
            article=make_article(
                title="장학금",
                board_name="장학공지",
                link="https://example.com",
            ),
            reason="장학 관련",
        )
    ]
    msg = build_relevant_message(matched, 10)
    assert "장학금" in msg
    assert "장학공지" in msg
    assert "10" in msg


def test_build_relevant_message_with_attachments(make_article, make_classified):
    from ku_notice_monitor.models import Attachment

    att1 = Attachment(filename="안내문.hwp", url="https://example.com/1/download.do")
    att2 = Attachment(filename="양식.pdf", url="https://example.com/2/download.do")
    matched = [
        make_classified(
            article=make_article(
                title="장학금",
                board_name="장학공지",
                link="https://example.com",
                attachments=[att1, att2],
            ),
            reason="장학 관련",
        )
    ]
    msg = build_relevant_message(matched, 5)
    assert "안내문.hwp" in msg
    assert "양식.pdf" in msg


def test_build_urgent_message_has_deadline_and_actions(make_article, make_classified):
    matched = [
        make_classified(
            article=make_article(title="수강신청"),
            delivery="immediate",
            reason="필수 일정",
            summary="오늘 확인",
            deadline="2099-12-31",
            actions=["수강바구니 확인"],
        )
    ]
    msg = build_urgent_message(matched, 3)
    assert "🚨" in msg
    assert "2099-12-31" in msg
    assert "수강바구니 확인" in msg


def test_build_digest_message_marks_updated_article(make_article, make_classified):
    matched = [
        make_classified(
            article=make_article(title="인턴 모집", is_update=True),
            reason="관심 분야",
        )
    ]
    msg = build_digest_message(matched)
    assert "🗂" in msg
    assert "[수정]" in msg


def test_review_message_explains_uncertainty(make_article, make_classified):
    matched = [
        make_classified(
            article=make_article(title="졸업 요건"),
            delivery="review",
            audience_fit="unknown",
            uncertainties=["학번별 적용 기준 불명확"],
        )
    ]
    msg = build_urgent_message(matched, 1)
    assert "[대상 확인 필요]" in msg
    assert "학번별 적용 기준 불명확" in msg


# --- build_no_new_message ---


def test_build_no_new_message():
    msg = build_no_new_message()
    assert "새로운 공지가 없습니다" in msg


# --- build_no_relevant_message ---


def test_build_no_relevant_message():
    msg = build_no_relevant_message(5)
    assert "5" in msg
    assert "관련 공지 없음" in msg


# --- build_error_message ---


def test_build_error_message():
    msg = build_error_message("테스트 오류")
    assert "오류" in msg
    assert "테스트 오류" in msg


# --- build_first_run_message ---


def test_build_first_run_message():
    msg = build_first_run_message(123)
    assert "123" in msg
    assert "확인함" in msg


# --- split_message ---


def test_split_message_short():
    assert split_message("hello") == ["hello"]


def test_split_message_exactly_at_limit():
    text = "x" * MAX_TELEGRAM_MESSAGE_LENGTH
    assert split_message(text) == [text]


def test_split_message_long():
    long_text = "\n".join(["a" * 100] * 100)  # 100 lines of 100 chars
    parts = split_message(long_text)
    assert len(parts) >= 2
    for part in parts:
        assert len(part) <= MAX_TELEGRAM_MESSAGE_LENGTH


def test_split_message_long_single_line_no_loss():
    text = "x" * (MAX_TELEGRAM_MESSAGE_LENGTH * 2 + 123)
    parts = split_message(text)
    assert "".join(parts) == text
    assert all(len(part) <= MAX_TELEGRAM_MESSAGE_LENGTH for part in parts)


def test_send_telegram_reports_complete_delivery():
    with patch("ku_notice_monitor.notifier.send_telegram_part", new_callable=AsyncMock) as send:
        result = asyncio.run(send_telegram("hello"))
    assert result.complete is True
    assert result.sent_parts == 1
    send.assert_awaited_once_with("hello")


def test_send_telegram_raises_on_partial_failure():
    text = "x" * (MAX_TELEGRAM_MESSAGE_LENGTH + 10)
    with patch(
        "ku_notice_monitor.notifier.send_telegram_part",
        new_callable=AsyncMock,
        side_effect=[None, RuntimeError("telegram down")],
    ):
        with pytest.raises(TelegramDeliveryError, match="1/2"):
            asyncio.run(send_telegram(text))
