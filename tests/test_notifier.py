"""notifier.py 단위 테스트"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from ku_notice_monitor import notifier
from ku_notice_monitor.constants import MAX_TELEGRAM_MESSAGE_LENGTH
from ku_notice_monitor.notifier import (
    TelegramDeliveryError,
    TelegramNotConfiguredError,
    _error_from_response,
    build_digest_messages,
    build_error_message,
    build_first_run_message,
    build_no_new_message,
    build_no_relevant_message,
    build_urgent_messages,
    html_to_plain_text,
    send_telegram,
    send_telegram_part,
    split_message,
)


def build_urgent_message(matched, total_new):
    return "\n".join(build_urgent_messages(matched, total_new))


def build_digest_message(matched):
    return "\n".join(build_digest_messages(matched))


# --- 메시지 구성 ---


def test_build_digest_message_lists_notice(make_article, make_classified):
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
    msg = build_digest_message(matched)
    assert "장학금" in msg
    assert "장학공지" in msg
    assert "관심 공지 1건" in msg


def test_message_counts_attachments(make_article, make_classified):
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
    msg = build_digest_message(matched)
    assert "첨부 2개" in msg


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
    assert "새 공지 3건 중 관련 1건" in msg
    assert "2099년 12월 31일" in msg
    assert "수강바구니 확인" in msg
    assert "분류:" not in msg
    assert "이유:" not in msg
    assert "내 조건과 일치" not in msg
    assert "🚨" not in msg


def test_long_urgent_batch_splits_on_notice_boundaries_with_header(
    make_article,
    make_classified,
):
    matched = [
        make_classified(
            article=make_article(id=str(index), title=f"긴 공지 {index} " + "가" * 170),
            delivery="review",
            summary="요약 " + "나" * 210,
            uncertainties=["확인할 조건 " + "다" * 160],
            actions=["원문 확인 " + "라" * 160],
        )
        for index in range(1, 13)
    ]

    parts = build_urgent_messages(matched, 12)

    assert len(parts) > 1
    assert all(part[:4].isdigit() for part in parts)
    assert all("새 공지 12건 중 관련 12건" in part for part in parts)
    assert all(len(part) <= MAX_TELEGRAM_MESSAGE_LENGTH for part in parts)
    combined = "\n".join(parts)
    assert all(f"긴 공지 {index}" in combined for index in range(1, 13))


def test_build_digest_message_marks_updated_article(make_article, make_classified):
    matched = [
        make_classified(
            article=make_article(title="인턴 모집", is_update=True),
            reason="관심 분야",
        )
    ]
    msg = build_digest_message(matched)
    assert "관심 공지 1건" in msg
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
    assert "확인 필요:" in msg
    assert "학번별 적용 기준 불명확" in msg
    assert "unknown" not in msg


def test_message_escapes_dynamic_html_and_uses_link(
    make_article, make_classified
):
    matched = [
        make_classified(
            article=make_article(
                title="<필독> 등록금 & 장학",
                link="https://example.com/?a=1&b=2",
            ),
            summary="A < B",
        )
    ]
    msg = build_urgent_message(matched, 1)
    assert "&lt;필독&gt; 등록금 &amp; 장학" in msg
    assert "→ A &lt; B" in msg
    assert 'href="https://example.com/?a=1&amp;b=2"' in msg
    assert "https://example.com/?a=1&b=2\n" not in msg


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


def test_long_digest_splits_on_notice_boundaries_with_header(
    make_article,
    make_classified,
):
    matched = [
        make_classified(
            article=make_article(id=str(index), title=f"긴 공지 {index} " + "가" * 170),
            summary="요약 " + "나" * 210,
            actions=["원문 확인 " + "라" * 160],
        )
        for index in range(1, 20)
    ]

    parts = build_digest_messages(matched)

    assert len(parts) > 1
    assert all("관심 공지 19건" in part for part in parts)
    assert all(len(part) <= MAX_TELEGRAM_MESSAGE_LENGTH for part in parts)
    # 모든 조각의 링크 태그가 온전해야 텔레그램 HTML 파싱이 실패하지 않는다.
    assert all(part.count("<a ") == part.count("</a>") for part in parts)


# --- 전송 ---


def test_send_telegram_reports_complete_delivery():
    with patch("ku_notice_monitor.notifier.send_telegram_part", new_callable=AsyncMock) as send:
        sent = asyncio.run(send_telegram("hello"))
    assert sent == 1
    send.assert_awaited_once_with("hello")


def test_send_telegram_raises_on_partial_failure():
    text = "x" * (MAX_TELEGRAM_MESSAGE_LENGTH + 10)
    with patch(
        "ku_notice_monitor.notifier.send_telegram_part",
        new_callable=AsyncMock,
        side_effect=[None, TelegramDeliveryError("telegram down")],
    ):
        with pytest.raises(TelegramDeliveryError, match="1/2"):
            asyncio.run(send_telegram(text))


def test_send_telegram_part_requires_credentials(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    with pytest.raises(TelegramNotConfiguredError) as info:
        asyncio.run(send_telegram_part("hello"))
    assert info.value.configuration is True


def test_send_telegram_part_posts_html_without_preview(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    with patch.object(notifier, "_post_message", new_callable=AsyncMock) as post:
        asyncio.run(send_telegram_part("<b>hello</b>"))
    args, kwargs = post.await_args
    assert args[1:] == ("token", "chat", "<b>hello</b>")
    assert kwargs == {"html": True}


def test_send_telegram_part_falls_back_to_plain_text_on_parse_error(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    parse_error = _error_from_response(
        400,
        {"ok": False, "error_code": 400, "description": "Bad Request: can't parse entities"},
    )
    with patch.object(
        notifier,
        "_post_message",
        new_callable=AsyncMock,
        side_effect=[parse_error, None],
    ) as post:
        asyncio.run(send_telegram_part('<a href="https://x">공지 &amp; 보기</a>'))
    fallback_args, fallback_kwargs = post.await_args_list[1]
    assert fallback_args[3] == "공지 & 보기"
    assert fallback_kwargs == {"html": False}


def test_send_telegram_part_rejects_oversized_message_permanently():
    with pytest.raises(TelegramDeliveryError) as info:
        asyncio.run(send_telegram_part("x" * (MAX_TELEGRAM_MESSAGE_LENGTH + 1)))
    assert info.value.permanent is True


def test_html_to_plain_text():
    assert html_to_plain_text("<b>A</b> &lt; B") == "A < B"


@pytest.mark.parametrize(
    "status,payload,permanent,configuration,retry_after",
    [
        (429, {"error_code": 429, "parameters": {"retry_after": 30}}, False, False, 30),
        (400, {"error_code": 400, "description": "Bad Request: message is too long"}, True, False, None),
        (400, {"error_code": 400, "description": "Bad Request: chat not found"}, False, True, None),
        (401, {"error_code": 401, "description": "Unauthorized"}, False, True, None),
        (403, {"error_code": 403, "description": "Forbidden: bot was blocked by the user"}, False, True, None),
        (502, {}, False, False, None),
    ],
)
def test_telegram_error_classification(status, payload, permanent, configuration, retry_after):
    error = _error_from_response(status, payload)
    assert error.permanent is permanent
    assert error.configuration is configuration
    assert error.retry_after == retry_after


class _FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    async def json(self, content_type=None):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, json):
        self.calls.append((url, json))
        return self.response


def test_post_message_sends_expected_payload():
    session = _FakeSession(_FakeResponse(200, {"ok": True}))
    asyncio.run(notifier._post_message(session, "TOKEN", "42", "<b>hi</b>", html=True))
    url, payload = session.calls[0]
    assert url.endswith("/botTOKEN/sendMessage")
    assert payload == {
        "chat_id": "42",
        "text": "<b>hi</b>",
        "link_preview_options": {"is_disabled": True},
        "parse_mode": "HTML",
    }


def test_post_message_raises_classified_api_error():
    session = _FakeSession(
        _FakeResponse(429, {"ok": False, "error_code": 429, "parameters": {"retry_after": 7}})
    )
    with pytest.raises(TelegramDeliveryError) as info:
        asyncio.run(notifier._post_message(session, "TOKEN", "42", "hi", html=False))
    assert info.value.retry_after == 7
    assert "parse_mode" not in session.calls[0][1]


def test_post_message_hides_token_on_connection_error():
    class _BrokenSession:
        def post(self, url, json):
            raise notifier.aiohttp.ClientConnectionError(f"cannot connect to {url}")

    with pytest.raises(TelegramDeliveryError) as info:
        asyncio.run(notifier._post_message(_BrokenSession(), "SECRET", "42", "hi", html=True))
    assert "SECRET" not in str(info.value)
    assert info.value.__cause__ is None
