"""notifier.py 단위 테스트"""

from constants import MAX_TELEGRAM_MESSAGE_LENGTH
from notifier import (
    build_error_message,
    build_no_new_message,
    build_no_relevant_message,
    build_relevant_message,
    split_message,
)

# --- build_relevant_message ---


def test_build_relevant_message(make_article):
    matched = [(make_article(title="장학금", board_name="장학공지", link="https://example.com"), 5, "장학 관련")]
    msg = build_relevant_message(matched, 10)
    assert "장학금" in msg
    assert "장학공지" in msg
    assert "10" in msg


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
