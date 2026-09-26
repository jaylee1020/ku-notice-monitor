"""알림 이후의 기능: 마감 리마인더, 버튼 피드백, 주간 리포트 집계.

알림을 보낸 공지는 ``tracked_notices``에 짧은 참조 ID로 기록한다. 이 기록으로
행동 마감이 다가오면 다시 알리고, 텔레그램 버튼(완료·유용·관련 없음)이 눌리면
해당 공지에 결과를 남긴다. ``weekly_report``에는 한 주 동안의 실행 결과를 모아
일요일에 리포트로 보낸다.
"""

import hashlib
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from .models import ClassifiedNotice
from .notifier import (
    TelegramDeliveryError,
    call_telegram,
    notice_keyboard,
    telegram_credentials,
    telegram_session,
)
from .util import KST

logger = logging.getLogger(__name__)

# 마감·일정이 지난 뒤에도 주간 리포트의 피드백 집계와 늦게 눌린 버튼을 위해 남겨 둔다.
TRACK_RETENTION_DAYS = 30
MAX_TRACKED_NOTICES = 300
MAX_REPORTED_SUPPRESSED = 30
MAX_UPCOMING_IN_REPORT = 10

_FEEDBACK_ACTIONS = {"u": "useful", "n": "not_relevant"}


def notice_ref(article_key: str) -> str:
    """버튼 callback_data(64바이트 제한)에 넣을 짧은 공지 참조 ID."""
    return hashlib.sha256(article_key.encode("utf-8")).hexdigest()[:16]


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _as_kst(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=KST) if parsed.tzinfo is None else parsed.astimezone(KST)


def _stored_notice(notice: ClassifiedNotice) -> dict:
    """리마인더를 다시 만들 수 있을 만큼만 저장한다. 긴 본문과 근거는 뺀다."""
    data = notice.to_dict()
    data["article"] = {**data["article"], "description": "", "images": []}
    data["evidence"] = []
    return data


def _keep_until(notice: ClassifiedNotice, today: date) -> str:
    dates = [today, _parse_date(notice.deadline)]
    dates.extend(_parse_date(item.get("date")) for item in notice.dates if isinstance(item, dict))
    latest = max(value for value in dates if value is not None)
    return (latest + timedelta(days=TRACK_RETENTION_DAYS)).isoformat()


def load_tracked_notice(record: dict) -> ClassifiedNotice | None:
    try:
        return ClassifiedNotice.from_dict(record["notice"])
    except (KeyError, TypeError, ValueError):
        return None


def track_notices(state: dict, notices: list[ClassifiedNotice], now: datetime) -> None:
    """알림 대상 공지를 기록한다. 수정 공지는 완료·피드백·리마인더 기록을 유지한다."""
    tracked = state.setdefault("tracked_notices", {})
    today = now.astimezone(KST).date()
    delivered_keys = set()
    for notice in notices:
        ref = notice_ref(notice.article.key)
        record = tracked.get(ref) or {
            "tracked_at": now.isoformat(),
            "done_at": None,
            "feedback": None,
            "feedback_at": None,
            "reminded": [],
        }
        record.update(
            key=notice.article.key,
            notice=_stored_notice(notice),
            keep_until=_keep_until(notice, today),
        )
        tracked[ref] = record
        delivered_keys.add(notice.article.key)
    if len(tracked) > MAX_TRACKED_NOTICES:
        oldest = sorted(tracked, key=lambda ref: str(tracked[ref].get("tracked_at", "")))
        for ref in oldest[: len(tracked) - MAX_TRACKED_NOTICES]:
            del tracked[ref]
    # 규칙 판정으로 숨겼다가 재분석에서 알린 공지는 "알림 안 한 공지"에서 뺀다.
    if delivered_keys and isinstance(state.get("weekly_report"), dict):
        weekly = state["weekly_report"]
        weekly["suppressed"] = [
            item for item in weekly.get("suppressed", []) if item.get("key") not in delivered_keys
        ]


def keyboard_for(ref: str, record: dict) -> dict:
    notice = load_tracked_notice(record)
    return notice_keyboard(
        ref,
        with_done=notice is not None and _parse_date(notice.deadline) is not None,
        done=bool(record.get("done_at")),
        feedback=record.get("feedback"),
    )


# ---------------------------------------------------------------------------
# 마감 리마인더
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DueReminder:
    ref: str
    notice: ClassifiedNotice
    deadline: date
    days_left: int
    stage: int

    @property
    def marker(self) -> str:
        return f"{self.deadline.isoformat()}:{self.stage}"


def due_reminders(
    state: dict,
    days_before: list[int],
    reminder_hour: int,
    now: datetime,
) -> list[DueReminder]:
    """지금 보내야 할 마감 리마인더.

    ``days_before=[3, 1]``이면 남은 날짜가 3일 이하일 때 3일 단계, 1일 이하일 때
    1일 단계를 한 번씩 보낸다. 실행이 하루를 건너뛰어도 다음 실행에서 따라잡는다.
    처음 알림을 보낸 날이 이미 그 단계 안이면 같은 내용을 반복하지 않도록 건너뛴다.
    """
    current = now.astimezone(KST)
    if not days_before or current.hour < reminder_hour:
        return []
    today = current.date()
    due: list[DueReminder] = []
    for ref, record in state.get("tracked_notices", {}).items():
        if record.get("done_at"):
            continue
        notice = load_tracked_notice(record)
        deadline = _parse_date(notice.deadline) if notice else None
        if notice is None or deadline is None:
            continue
        days_left = (deadline - today).days
        stages = [day for day in days_before if day >= days_left]
        if days_left < 0 or not stages:
            continue
        stage = min(stages)
        tracked_at = _as_kst(record.get("tracked_at"))
        if tracked_at is None or tracked_at.date() >= deadline - timedelta(days=stage):
            continue
        reminder = DueReminder(ref, notice, deadline, days_left, stage)
        if reminder.marker in record.get("reminded", []):
            continue
        due.append(reminder)
    return sorted(due, key=lambda item: (item.deadline, item.ref))


def mark_reminded(state: dict, reminder: DueReminder) -> None:
    record = state["tracked_notices"][reminder.ref]
    record["reminded"] = [*record.get("reminded", []), reminder.marker][-10:]


# ---------------------------------------------------------------------------
# 버튼
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ButtonResult:
    ref: str
    record: dict
    message: str


def apply_button_press(state: dict, data: str, now: datetime) -> ButtonResult | None:
    """callback_data를 공지 기록에 반영한다. 같은 버튼을 다시 누르면 취소한다."""
    action, _, ref = data.partition(":")
    record = state.get("tracked_notices", {}).get(ref)
    if record is None:
        return None
    notice = load_tracked_notice(record)
    title = notice.article.title if notice else record.get("key", ref)
    if action == "d":
        if record.get("done_at"):
            record["done_at"] = None
            message = "완료 표시를 취소했습니다."
        else:
            record["done_at"] = now.isoformat()
            message = "완료로 표시했습니다. 이 공지의 마감 알림을 멈춥니다."
        logger.info("버튼: %s — %s", message, title)
        return ButtonResult(ref, record, message)
    if action not in _FEEDBACK_ACTIONS:
        return None
    value = _FEEDBACK_ACTIONS[action]
    if record.get("feedback") == value:
        record["feedback"] = None
        record["feedback_at"] = None
        message = "피드백을 취소했습니다."
    else:
        record["feedback"] = value
        record["feedback_at"] = now.isoformat()
        message = "피드백을 기록했습니다. 고마워요!"
    # 판정 개선 사례(evals)로 옮길 수 있도록 판정 근거를 함께 남긴다.
    logger.info(
        "피드백=%s key=%s delivery=%s category=%s reason=%s title=%s",
        record.get("feedback"),
        record.get("key"),
        notice.delivery if notice else None,
        notice.category if notice else None,
        notice.reason if notice else None,
        title,
    )
    return ButtonResult(ref, record, message)


async def process_button_presses(state: dict, now: datetime) -> int:
    """지난 실행 이후 눌린 버튼을 가져와 반영하고 처리한 버튼 수를 반환한다.

    서버 없이 매 실행마다 ``getUpdates``로 가져온다. 텔레그램은 눌린 버튼을 24시간
    보관하므로 실행 간격이 그보다 짧으면 놓치지 않는다. 버튼 표시는 결과에 맞게
    바꾸고, 이미 늦어 실패하는 응답(answerCallbackQuery)은 무시한다.
    """
    token, chat_id = telegram_credentials()
    offset = state.get("telegram_update_offset")
    payload: dict[str, Any] = {"timeout": 0, "allowed_updates": ["callback_query"]}
    if isinstance(offset, int):
        payload["offset"] = offset
    processed = 0
    async with telegram_session() as session:
        updates = await call_telegram(session, token, "getUpdates", payload)
        for update in updates if isinstance(updates, list) else []:
            update_id = update.get("update_id") if isinstance(update, dict) else None
            if not isinstance(update_id, int):
                continue
            state["telegram_update_offset"] = update_id + 1
            query = update.get("callback_query")
            if not isinstance(query, dict):
                continue
            message = query.get("message")
            message = message if isinstance(message, dict) else {}
            chat = message.get("chat")
            chat = chat if isinstance(chat, dict) else {}
            # 이 봇의 알림 채팅에서 눌린 버튼만 받아들인다.
            if str(chat.get("id")) != str(chat_id):
                continue
            result = apply_button_press(state, str(query.get("data", "")), now)
            processed += result is not None
            try:
                await call_telegram(
                    session,
                    token,
                    "answerCallbackQuery",
                    {
                        "callback_query_id": query.get("id"),
                        "text": result.message if result else "이미 정리된 공지입니다.",
                    },
                )
            except TelegramDeliveryError:
                pass
            if result is not None and isinstance(message.get("message_id"), int):
                try:
                    await call_telegram(
                        session,
                        token,
                        "editMessageReplyMarkup",
                        {
                            "chat_id": chat.get("id"),
                            "message_id": message["message_id"],
                            "reply_markup": keyboard_for(result.ref, result.record),
                        },
                    )
                except TelegramDeliveryError as exc:
                    logger.warning("버튼 표시를 바꾸지 못했습니다: %s", exc)
    return processed


# ---------------------------------------------------------------------------
# 주간 리포트
# ---------------------------------------------------------------------------


def _new_weekly(now: datetime, last_run_at: str | None = None) -> dict:
    return {
        "period_start": now.isoformat(),
        "runs": 0,
        "outage_runs": 0,
        "max_gap_minutes": 0,
        "last_run_at": last_run_at,
        "new_articles": 0,
        "immediate": 0,
        "review": 0,
        "digest": 0,
        "suppressed_count": 0,
        "suppressed": [],
        "input_tokens": 0,
        "output_tokens": 0,
    }


def _weekly(state: dict, now: datetime) -> dict:
    weekly = state.get("weekly_report")
    if not isinstance(weekly, dict) or _as_kst(weekly.get("period_start")) is None:
        weekly = _new_weekly(now)
        state["weekly_report"] = weekly
    for name, default in _new_weekly(now).items():
        weekly.setdefault(name, default)
    return weekly


def _tokens(stats: dict, name: str) -> int:
    total = 0
    for section in ("analysis_metrics", "profile_metrics"):
        value = stats.get(section, {}).get(name, 0)
        total += value if isinstance(value, int) else 0
    return total


def record_run(state: dict, stats: dict, now: datetime) -> None:
    """이번 실행을 주간 집계에 더한다. 실행 간격으로 예약 누락도 드러난다."""
    weekly = _weekly(state, now)
    weekly["runs"] = int(weekly.get("runs", 0)) + 1
    if stats.get("method") == "source_outage":
        weekly["outage_runs"] = int(weekly.get("outage_runs", 0)) + 1
    if previous := _as_kst(weekly.get("last_run_at")):
        gap = int((now - previous).total_seconds() // 60)
        weekly["max_gap_minutes"] = max(int(weekly.get("max_gap_minutes", 0)), gap)
    weekly["last_run_at"] = now.isoformat()
    weekly["new_articles"] = int(weekly.get("new_articles", 0)) + int(stats.get("new_articles", 0))
    for name in ("input_tokens", "output_tokens"):
        weekly[name] = int(weekly.get(name, 0)) + _tokens(stats, name)


def record_classification(
    state: dict,
    now: datetime,
    *,
    immediate: int,
    review: int,
    digest: int,
    suppressed: list[ClassifiedNotice],
) -> None:
    weekly = _weekly(state, now)
    weekly["immediate"] = int(weekly.get("immediate", 0)) + immediate
    weekly["review"] = int(weekly.get("review", 0)) + review
    weekly["digest"] = int(weekly.get("digest", 0)) + digest
    listed = {item.get("key"): item for item in weekly.get("suppressed", [])}
    for notice in suppressed:
        if notice.article.key not in listed:
            weekly["suppressed_count"] = int(weekly.get("suppressed_count", 0)) + 1
        listed[notice.article.key] = {
            "key": notice.article.key,
            "title": notice.article.title,
            "board": notice.article.board_name,
            "link": notice.article.link,
            "reason": notice.reason,
        }
    weekly["suppressed"] = list(listed.values())[-MAX_REPORTED_SUPPRESSED:]


def report_time_after(start: datetime, hour: int) -> datetime:
    """``start`` 이후 처음 오는 일요일 ``hour``시(KST)."""
    local = start.astimezone(KST)
    candidate = local.replace(hour=hour, minute=0, second=0, microsecond=0) + timedelta(
        days=(6 - local.weekday()) % 7
    )
    return candidate if candidate > local else candidate + timedelta(days=7)


def weekly_report_due(state: dict, hour: int, now: datetime) -> bool:
    weekly = state.get("weekly_report")
    if not isinstance(weekly, dict):
        return False
    start = _as_kst(weekly.get("period_start"))
    return start is not None and now >= report_time_after(start, hour)


def weekly_summary(state: dict, now: datetime) -> dict[str, Any]:
    """리포트 메시지에 넣을 값. 피드백과 다가오는 마감은 공지 기록에서 계산한다."""
    weekly = _weekly(state, now)
    start = _as_kst(weekly["period_start"]) or now
    today = now.astimezone(KST).date()
    useful = 0
    not_relevant: list[str] = []
    upcoming: list[ClassifiedNotice] = []
    for record in state.get("tracked_notices", {}).values():
        notice = load_tracked_notice(record)
        feedback_at = _as_kst(record.get("feedback_at"))
        if feedback_at and feedback_at >= start and notice is not None:
            if record.get("feedback") == "useful":
                useful += 1
            elif record.get("feedback") == "not_relevant":
                not_relevant.append(notice.article.title)
        deadline = _parse_date(notice.deadline) if notice else None
        if notice is not None and deadline is not None and deadline >= today and not record.get("done_at"):
            upcoming.append(notice)
    upcoming.sort(key=lambda item: (item.deadline or "", item.article.key))
    return {
        **weekly,
        "start": start,
        "end": now,
        "useful": useful,
        "not_relevant": not_relevant,
        "upcoming": upcoming[:MAX_UPCOMING_IN_REPORT],
    }


def start_new_week(state: dict, now: datetime) -> None:
    previous = state.get("weekly_report")
    last_run_at = previous.get("last_run_at") if isinstance(previous, dict) else None
    state["weekly_report"] = _new_weekly(now, last_run_at)
