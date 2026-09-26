"""마감 리마인더·버튼 피드백·주간 리포트 테스트."""

import asyncio
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest

from ku_notice_monitor import followups, notifier
from ku_notice_monitor.followups import (
    apply_button_press,
    due_reminders,
    keyboard_for,
    mark_reminded,
    notice_ref,
    process_button_presses,
    record_classification,
    record_run,
    report_time_after,
    start_new_week,
    track_notices,
    weekly_report_due,
    weekly_summary,
)
from ku_notice_monitor.notifier import (
    build_reminder_messages,
    build_urgent_messages,
    build_weekly_report,
    calendar_url,
    notice_keyboard,
)
from ku_notice_monitor.util import KST

NOW = datetime(2026, 9, 28, 10, 0, tzinfo=KST)  # 월요일


@pytest.fixture
def fixed_today(monkeypatch):
    monkeypatch.setattr(notifier, "now_kst", lambda: NOW)


@pytest.fixture
def deadline_notice(make_article, make_classified):
    def _make(deadline: str | None = "2026-10-01", key_id: str = "1", **overrides):
        article = make_article(
            id=key_id,
            title=f"[학사] 장학금 신청 {key_id}",
            board_name="장학공지",
            link=f"https://www.konkuk.ac.kr/notice/{key_id}",
            description="긴 본문 " * 200,
        )
        return make_classified(
            article=article,
            delivery=overrides.pop("delivery", "immediate"),
            deadline=deadline,
            actions=["신청서 제출"],
            **overrides,
        )

    return _make


# --- 공지 기록 ---


def test_track_notices_keeps_user_marks_and_drops_long_body(deadline_notice):
    state: dict = {}
    notice = deadline_notice()
    track_notices(state, [notice], NOW)
    ref = notice_ref(notice.article.key)
    record = state["tracked_notices"][ref]
    assert record["notice"]["article"]["description"] == ""
    assert record["keep_until"] == "2026-10-31"

    record["done_at"] = NOW.isoformat()
    record["feedback"] = "useful"
    revised = deadline_notice(deadline="2026-10-05")
    track_notices(state, [revised], NOW + timedelta(days=1))

    record = state["tracked_notices"][ref]
    assert record["done_at"] == NOW.isoformat()
    assert record["feedback"] == "useful"
    assert record["tracked_at"] == NOW.isoformat()
    assert record["notice"]["deadline"] == "2026-10-05"


def test_track_notices_removes_notice_from_weekly_suppressed(deadline_notice):
    state: dict = {}
    notice = deadline_notice()
    record_classification(state, NOW, immediate=0, review=0, digest=0, suppressed=[notice])
    assert state["weekly_report"]["suppressed_count"] == 1

    track_notices(state, [notice], NOW)
    assert state["weekly_report"]["suppressed"] == []


def test_keyboard_offers_done_only_for_notices_with_deadline(deadline_notice):
    state: dict = {}
    with_deadline = deadline_notice()
    without = deadline_notice(deadline=None, key_id="2")
    track_notices(state, [with_deadline, without], NOW)

    tracked = state["tracked_notices"]
    first_ref = notice_ref(with_deadline.article.key)
    second_ref = notice_ref(without.article.key)
    first = keyboard_for(first_ref, tracked[first_ref])
    second = keyboard_for(second_ref, tracked[second_ref])
    assert [button["text"] for button in first["inline_keyboard"][0]] == ["✅ 완료", "👍 유용", "👎 관련 없음"]
    assert [button["text"] for button in second["inline_keyboard"][0]] == ["👍 유용", "👎 관련 없음"]
    assert all(len(button["callback_data"].encode()) <= 64 for button in first["inline_keyboard"][0])


def test_notice_keyboard_marks_selection():
    keyboard = notice_keyboard("abc", with_done=True, done=True, feedback="not_relevant")
    assert [button["text"] for button in keyboard["inline_keyboard"][0]] == [
        "✅ 완료됨",
        "👍 유용",
        "👎 관련 없음 ✓",
    ]


# --- 리마인더 ---


def _tracked(state, notice, tracked_at):
    track_notices(state, [notice], tracked_at)
    return notice_ref(notice.article.key)


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2026, 9, 27), []),  # D-4
        (date(2026, 9, 28), [(3, 3)]),  # D-3
        (date(2026, 9, 29), [(2, 3)]),  # D-3 실행을 놓쳤으면 D-2에 따라잡는다
        (date(2026, 9, 30), [(1, 1)]),  # D-1
        (date(2026, 10, 1), [(0, 1)]),  # 당일에도 D-1 단계를 놓쳤으면 보낸다
        (date(2026, 10, 2), []),  # 지남
    ],
)
def test_due_reminders_stage_by_days_left(deadline_notice, today, expected):
    state: dict = {}
    _tracked(state, deadline_notice(), datetime(2026, 9, 20, 12, tzinfo=KST))
    now = datetime.combine(today, datetime.min.time(), tzinfo=KST).replace(hour=10)
    due = due_reminders(state, [3, 1], 9, now)
    assert [(item.days_left, item.stage) for item in due] == expected


def test_due_reminders_are_sent_once_per_stage(deadline_notice):
    state: dict = {}
    _tracked(state, deadline_notice(), datetime(2026, 9, 20, 12, tzinfo=KST))
    due = due_reminders(state, [3, 1], 9, NOW)
    mark_reminded(state, due[0])

    assert due_reminders(state, [3, 1], 9, NOW) == []
    assert due_reminders(state, [3, 1], 9, NOW + timedelta(days=1)) == []
    assert [item.stage for item in due_reminders(state, [3, 1], 9, NOW + timedelta(days=2))] == [1]


def test_due_reminders_skip_stage_already_covered_by_first_alert(deadline_notice):
    state: dict = {}
    # D-2에 처음 알렸으면 D-3 단계는 건너뛰고 D-1에만 다시 알린다.
    _tracked(state, deadline_notice(), datetime(2026, 9, 29, 8, tzinfo=KST))
    assert due_reminders(state, [3, 1], 9, datetime(2026, 9, 29, 12, tzinfo=KST)) == []
    due = due_reminders(state, [3, 1], 9, datetime(2026, 9, 30, 9, tzinfo=KST))
    assert [item.stage for item in due] == [1]


def test_due_reminders_respect_hour_done_and_missing_deadline(deadline_notice):
    state: dict = {}
    ref = _tracked(state, deadline_notice(), datetime(2026, 9, 20, tzinfo=KST))
    _tracked(state, deadline_notice(deadline=None, key_id="2"), datetime(2026, 9, 20, tzinfo=KST))

    assert due_reminders(state, [3, 1], 9, NOW.replace(hour=8)) == []
    assert due_reminders(state, [], 9, NOW) == []
    assert len(due_reminders(state, [3, 1], 9, NOW)) == 1

    state["tracked_notices"][ref]["done_at"] = NOW.isoformat()
    assert due_reminders(state, [3, 1], 9, NOW) == []


def test_changed_deadline_gets_fresh_reminders(deadline_notice):
    state: dict = {}
    _tracked(state, deadline_notice(), datetime(2026, 9, 20, tzinfo=KST))
    mark_reminded(state, due_reminders(state, [3, 1], 9, NOW)[0])

    track_notices(state, [deadline_notice(deadline="2026-10-02")], NOW)
    due = due_reminders(state, [3, 1], 9, NOW + timedelta(days=1))
    assert [(item.deadline, item.days_left) for item in due] == [(date(2026, 10, 2), 3)]


def test_reminder_message_shows_days_left_and_calendar(fixed_today, deadline_notice):
    [message] = build_reminder_messages(deadline_notice(), 3, calendar_links=True)
    assert message.startswith("<b>[마감 D-3] 마감이 다가옵니다</b>")
    assert "마감 10월 1일(목) · D-3" in message
    assert "캘린더에 추가" in message

    [today] = build_reminder_messages(deadline_notice(), 0)
    assert "오늘 마감입니다" in today
    assert "캘린더에 추가" not in today


# --- 캘린더 ---


def test_calendar_url_uses_deadline_as_all_day_event(fixed_today, deadline_notice):
    url = calendar_url(deadline_notice())
    assert url is not None
    query = parse_qs(urlparse(url).query)
    assert query["action"] == ["TEMPLATE"]
    assert query["dates"] == ["20261001/20261002"]
    assert query["text"] == ["[마감] [학사] 장학금 신청 1"]
    assert query["details"] == ["https://www.konkuk.ac.kr/notice/1"]


def test_calendar_url_falls_back_to_upcoming_event(fixed_today, deadline_notice):
    notice = deadline_notice(
        deadline=None,
        dates=[
            {"kind": "event_start", "date": "2026-09-01"},
            {"kind": "event_start", "date": "2026-10-10"},
            {"kind": "application_open", "date": "2026-10-03"},
        ],
    )
    query = parse_qs(urlparse(calendar_url(notice) or "").query)
    assert query["dates"] == ["20261010/20261011"]
    assert not query["text"][0].startswith("[마감]")


def test_calendar_url_skips_past_or_missing_dates(fixed_today, deadline_notice):
    assert calendar_url(deadline_notice(deadline="2026-09-01")) is None
    assert calendar_url(deadline_notice(deadline=None)) is None


def test_urgent_message_calendar_link_is_escaped(fixed_today, deadline_notice):
    [message] = build_urgent_messages([deadline_notice()], calendar_links=True)
    assert '<a href="https://calendar.google.com/calendar/render?action=TEMPLATE&amp;' in message
    [plain] = build_urgent_messages([deadline_notice()])
    assert "캘린더" not in plain


# --- 버튼 ---


def test_button_presses_toggle(deadline_notice):
    state: dict = {}
    ref = _tracked(state, deadline_notice(), NOW)
    record = state["tracked_notices"][ref]

    result = apply_button_press(state, f"d:{ref}", NOW)
    assert result is not None and record["done_at"] == NOW.isoformat()
    apply_button_press(state, f"d:{ref}", NOW)
    assert record["done_at"] is None

    apply_button_press(state, f"n:{ref}", NOW)
    assert record["feedback"] == "not_relevant"
    apply_button_press(state, f"u:{ref}", NOW)
    assert record["feedback"] == "useful"
    apply_button_press(state, f"u:{ref}", NOW)
    assert record["feedback"] is None and record["feedback_at"] is None

    assert apply_button_press(state, "d:unknown", NOW) is None
    assert apply_button_press(state, f"x:{ref}", NOW) is None


def test_process_button_presses_applies_and_updates_buttons(monkeypatch, deadline_notice):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    state: dict = {"telegram_update_offset": 7}
    ref = _tracked(state, deadline_notice(), NOW)
    updates = [
        {
            "update_id": 7,
            "callback_query": {
                "id": "q1",
                "data": f"d:{ref}",
                "message": {"message_id": 99, "chat": {"id": 42}},
            },
        },
        {  # 다른 채팅에서 온 버튼은 무시한다.
            "update_id": 8,
            "callback_query": {
                "id": "q2",
                "data": f"n:{ref}",
                "message": {"message_id": 5, "chat": {"id": 1}},
            },
        },
    ]
    calls: list[tuple[str, dict]] = []

    async def fake_call(session, token, method, payload):
        calls.append((method, payload))
        if method == "getUpdates":
            return updates
        if method == "answerCallbackQuery":
            raise notifier.TelegramDeliveryError("query is too old")
        return True

    @asynccontextmanager
    async def fake_session():
        yield object()

    with (
        patch.object(followups, "call_telegram", fake_call),
        patch.object(followups, "telegram_session", fake_session),
    ):
        processed = asyncio.run(process_button_presses(state, NOW))

    assert processed == 1
    assert state["telegram_update_offset"] == 9
    record = state["tracked_notices"][ref]
    assert record["done_at"] == NOW.isoformat()
    assert record["feedback"] is None
    assert calls[0] == (
        "getUpdates",
        {"timeout": 0, "allowed_updates": ["callback_query"], "offset": 7},
    )
    [edit] = [payload for method, payload in calls if method == "editMessageReplyMarkup"]
    assert edit["message_id"] == 99
    assert edit["reply_markup"]["inline_keyboard"][0][0]["text"] == "✅ 완료됨"


def test_process_button_presses_requires_credentials(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(notifier.TelegramNotConfiguredError):
        asyncio.run(process_button_presses({}, NOW))


# --- 주간 리포트 ---


@pytest.mark.parametrize(
    ("start", "expected"),
    [
        (datetime(2026, 9, 28, 10, tzinfo=KST), datetime(2026, 10, 4, 21, tzinfo=KST)),
        (datetime(2026, 10, 4, 20, tzinfo=KST), datetime(2026, 10, 4, 21, tzinfo=KST)),
        (datetime(2026, 10, 4, 21, 30, tzinfo=KST), datetime(2026, 10, 11, 21, tzinfo=KST)),
    ],
)
def test_report_time_is_next_sunday_at_hour(start, expected):
    assert report_time_after(start, 21) == expected


def test_weekly_counts_runs_gaps_outages_and_tokens():
    state: dict = {}
    record_run(state, {"new_articles": 2, "analysis_metrics": {"input_tokens": 100}}, NOW)
    record_run(
        state,
        {
            "new_articles": 1,
            "method": "source_outage",
            "analysis_metrics": {"input_tokens": 50, "output_tokens": 7},
            "profile_metrics": {"input_tokens": 5},
        },
        NOW + timedelta(hours=6, minutes=12),
    )
    weekly = state["weekly_report"]
    assert weekly["runs"] == 2
    assert weekly["outage_runs"] == 1
    assert weekly["max_gap_minutes"] == 372
    assert weekly["new_articles"] == 3
    assert (weekly["input_tokens"], weekly["output_tokens"]) == (155, 7)


def test_weekly_report_due_and_new_week_keeps_gap_tracking():
    state: dict = {}
    record_run(state, {}, NOW)
    assert not weekly_report_due(state, 21, datetime(2026, 10, 4, 20, tzinfo=KST))
    sunday = datetime(2026, 10, 4, 21, 5, tzinfo=KST)
    assert weekly_report_due(state, 21, sunday)

    start_new_week(state, sunday)
    assert not weekly_report_due(state, 21, sunday)
    assert state["weekly_report"]["runs"] == 0
    record_run(state, {}, sunday + timedelta(hours=2))
    assert state["weekly_report"]["max_gap_minutes"] > 0


def test_weekly_summary_collects_feedback_and_upcoming(deadline_notice):
    state: dict = {}
    record_run(state, {}, NOW)
    soon = deadline_notice(key_id="1", deadline="2026-10-01")
    later = deadline_notice(key_id="2", deadline="2026-10-20")
    past = deadline_notice(key_id="3", deadline="2026-09-01")
    done = deadline_notice(key_id="4", deadline="2026-10-02")
    track_notices(state, [later, soon, past, done], NOW)
    apply_button_press(state, f"u:{notice_ref(soon.article.key)}", NOW)
    apply_button_press(state, f"n:{notice_ref(past.article.key)}", NOW)
    apply_button_press(state, f"d:{notice_ref(done.article.key)}", NOW)

    summary = weekly_summary(state, NOW + timedelta(days=1))
    assert summary["useful"] == 1
    assert summary["not_relevant"] == [past.article.title]
    assert [item.article.key for item in summary["upcoming"]] == [
        soon.article.key,
        later.article.key,
    ]


def test_weekly_report_message(fixed_today, deadline_notice):
    suppressed = [deadline_notice(key_id=str(i), reason="대학원생 대상") for i in range(3)]
    state: dict = {}
    record_run(
        state,
        {"new_articles": 5, "analysis_metrics": {"input_tokens": 123_000, "output_tokens": 950}},
        NOW - timedelta(days=6),
    )
    record_run(state, {"method": "source_outage"}, NOW - timedelta(hours=1))
    record_classification(state, NOW, immediate=1, review=0, digest=1, suppressed=suppressed)
    summary = weekly_summary(state, NOW)
    summary["upcoming"] = [deadline_notice(key_id="9")]

    message = build_weekly_report(summary)
    assert message.startswith("<b>[주간 리포트] 9월 22일(화) ~ 9월 28일(월)</b>")
    assert "실행 2회 · 가장 긴 실행 간격 143시간" in message
    assert "새 공지 5건 → 즉시 1 · 확인 필요 0 · 요약 1 · 알림 안 함 3" in message
    assert "학교 서버 장애로 건너뛴 실행 1회" in message
    assert "AI 사용량 입력 12.3만 · 출력 950 토큰" in message
    assert "<b>다가오는 마감</b>" in message
    assert "10월 1일(목) D-3" in message
    assert message.count("대학원생 대상") == 3


def test_weekly_report_message_without_activity(fixed_today):
    state: dict = {}
    record_run(state, {}, NOW)
    message = build_weekly_report(weekly_summary(state, NOW))
    assert "실행 1회" in message
    assert "알림 안 한 공지" not in message
    assert "AI 사용량" not in message
