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


def build_urgent_message(matched):
    return "\n".join(build_urgent_messages(matched))


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
    msg = build_urgent_message(matched)
    assert "[중요]" in msg
    assert "<b>수강신청</b>" in msg
    assert "마감 2099년 12월 31일(목)" in msg
    assert "새 공지" not in msg
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

    parts = build_urgent_messages(matched)

    assert len(parts) > 1
    assert all("[확인 필요]" in part for part in parts)
    assert all(f"/{len(parts)})" in part.split("\n", 1)[0] for part in parts)
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
    assert "(수정됨)" in msg


def test_review_message_explains_uncertainty(make_article, make_classified):
    matched = [
        make_classified(
            article=make_article(title="졸업 요건"),
            delivery="review",
            audience_fit="unknown",
            uncertainties=["학번별 적용 기준 불명확"],
        )
    ]
    msg = build_urgent_message(matched)
    assert "확인할 점:" in msg
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
    msg = build_urgent_message(matched)
    assert "&lt;필독&gt; 등록금 &amp; 장학" in msg
    assert "\nA &lt; B\n" in msg
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


# --- 가독성 ---


def _freeze_today(monkeypatch, year=2026, month=9, day=24):
    from datetime import datetime

    from ku_notice_monitor.util import KST

    monkeypatch.setattr(
        notifier, "now_kst", lambda: datetime(year, month, day, 21, 0, tzinfo=KST)
    )


@pytest.mark.parametrize(
    "title,board,expected",
    [
        ("[대학일자리+] 한미그룹 채용설명회(10/1)", "대학일자리플러스", "한미그룹 채용설명회(10/1)"),
        ("[장학공지] [교내] 건국가족장학생 선발", "장학공지", "[교내] 건국가족장학생 선발"),
        ("[교내] 건국가족장학생 선발", "장학공지", "[교내] 건국가족장학생 선발"),
        ("[대학일자리+]", "대학일자리플러스", "[대학일자리+]"),
    ],
)
def test_clean_title_removes_tag_duplicating_board(title, board, expected):
    assert notifier.clean_title(title, board) == expected


def test_item_shows_event_date_when_no_deadline(monkeypatch, make_article, make_classified):
    _freeze_today(monkeypatch)
    matched = [
        make_classified(
            article=make_article(
                title="[대학일자리+] 키엔스코리아 채용설명회(9/30)",
                board_name="대학일자리플러스",
            ),
            summary="컨설턴트 세일즈 직무 채용설명회가 공학관에서 열린다.",
            dates=[{"kind": "event_start", "date": "2026-09-30", "label": "설명회"}],
            actions=["채용설명회 참석", "사전 신청"],
        )
    ]
    msg = build_digest_message(matched)
    assert "<b>키엔스코리아 채용설명회(9/30)</b>" in msg
    assert "[대학일자리+]" not in msg
    assert "대학일자리플러스 · 일시 9월 30일(수) · D-6" in msg
    assert "할 일: 채용설명회 참석 · 사전 신청" in msg


def test_item_shows_application_period(monkeypatch, make_article, make_classified):
    _freeze_today(monkeypatch)
    matched = [
        make_classified(
            article=make_article(title="건국가족장학생 선발", board_name="장학공지"),
            deadline="2026-10-20",
            dates=[
                {"kind": "application_open", "date": "2026-10-01", "label": "신청 시작"},
                {"kind": "application_deadline", "date": "2026-10-20", "label": "신청 마감"},
            ],
        )
    ]
    msg = build_digest_message(matched)
    assert "신청 10월 1일(목) ~ 10월 20일(화) · D-26" in msg


def test_item_marks_past_deadline(monkeypatch, make_article, make_classified):
    _freeze_today(monkeypatch)
    matched = [make_classified(article=make_article(title="지난 공지"), deadline="2026-09-20")]
    assert "마감 9월 20일(일) · 지남" in build_digest_message(matched)


def test_digest_orders_by_nearest_date(monkeypatch, make_article, make_classified):
    _freeze_today(monkeypatch)
    matched = [
        make_classified(article=make_article(id="1", title="날짜 없음")),
        make_classified(article=make_article(id="2", title="다음 달"), deadline="2026-10-20"),
        make_classified(article=make_article(id="3", title="이번 주"), deadline="2026-09-26"),
    ]
    msg = build_digest_message(matched)
    assert msg.index("1. <b>이번 주") < msg.index("2. <b>다음 달") < msg.index("3. <b>날짜 없음")
    assert "9월 24일(목) 관심 공지 3건" in msg


def test_summary_is_cut_at_sentence_boundary(make_article, make_classified):
    first = "첫 문장은 핵심 내용입니다."
    matched = [
        make_classified(
            article=make_article(title="공지"),
            summary=first + " " + "둘째 문장은 길게 이어집니다 " * 12 + "끝.",
        )
    ]
    msg = build_digest_message(matched)
    assert first in msg
    assert "둘째 문장" not in msg


def test_summary_repeating_title_is_omitted(make_article, make_classified):
    matched = [make_classified(article=make_article(title="수강 신청 안내"), summary="수강신청 안내")]
    msg = build_digest_message(matched)
    assert msg.count("수강 신청 안내") == 1


def test_error_message_is_readable_and_links_run(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    msg = build_error_message(
        "게시판 9개 중 3개만 읽음",
        title="일부 게시판을 읽지 못해 이번 확인을 건너뛰었습니다",
        guidance="다음 실행에서 다시 확인합니다.",
    )
    assert msg.startswith("<b>[모니터링 오류] 일부 게시판을 읽지 못해")
    assert "원인: 게시판 9개 중 3개만 읽음" in msg
    assert '<a href="https://github.com/owner/repo/actions/runs/42">실행 로그 보기</a>' in msg


def test_source_outage_and_recovery_messages(monkeypatch):
    _freeze_today(monkeypatch)
    msg = notifier.build_source_outage_message(
        consecutive_runs=2,
        since="2026-09-24T10:55:00+09:00",
        detail="응답 시간 초과 9개",
    )
    assert "9월 24일(목) 10:55부터 2회 연속" in msg
    assert "응답 시간 초과 9개" in msg
    assert "복구되었습니다" in notifier.build_source_recovered_message(3)


def test_new_boards_message_lists_seeded_boards():
    msg = notifier.build_new_boards_message({"컴퓨터공학부": 10})
    assert "컴퓨터공학부(기존 공지 10건)" in msg
    assert "앞으로 올라오는 공지부터" in msg


def test_post_message_includes_buttons():
    session = _FakeSession(_FakeResponse(200, {"ok": True}))
    markup = {"inline_keyboard": [[{"text": "✅ 완료", "callback_data": "d:1"}]]}
    asyncio.run(
        notifier._post_message(session, "TOKEN", "42", "hi", html=True, reply_markup=markup)
    )
    assert session.calls[0][1]["reply_markup"] == markup


def test_send_telegram_part_keeps_buttons_on_plain_text_fallback(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    parse_error = _error_from_response(
        400,
        {"ok": False, "error_code": 400, "description": "Bad Request: can't parse entities"},
    )
    markup = {"inline_keyboard": []}
    with patch.object(
        notifier, "_post_message", new_callable=AsyncMock, side_effect=[parse_error, None]
    ) as post:
        asyncio.run(send_telegram_part("<b>hi</b>", reply_markup=markup))
    assert [call.kwargs["reply_markup"] for call in post.await_args_list] == [markup, markup]


def test_call_telegram_returns_result():
    session = _FakeSession(_FakeResponse(200, {"ok": True, "result": [{"update_id": 1}]}))
    result = asyncio.run(notifier.call_telegram(session, "TOKEN", "getUpdates", {"timeout": 0}))
    assert result == [{"update_id": 1}]
    assert session.calls[0][0].endswith("/botTOKEN/getUpdates")
