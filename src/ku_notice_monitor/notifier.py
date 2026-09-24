"""텔레그램 메시지 구성과 전송."""

import logging
import os
import re
from datetime import date, datetime
from html import escape, unescape

import aiohttp

from .constants import MAX_TELEGRAM_MESSAGE_LENGTH
from .models import ClassifiedNotice
from .util import normalize_for_match, now_kst

logger = logging.getLogger(__name__)

_ITEM_SEPARATOR = "\n\n"
_TELEGRAM_API = "https://api.telegram.org"
_TELEGRAM_TIMEOUT_SECONDS = 20
_CONFIGURATION_ERROR_HINTS = ("chat not found", "bot was blocked", "not enough rights")


class TelegramDeliveryError(RuntimeError):
    """텔레그램 메시지가 전송되지 않았을 때 발생한다.

    - ``permanent``: 메시지 자체의 문제라 같은 내용을 다시 보내도 실패한다.
    - ``configuration``: 토큰·채팅 설정 문제라 메시지를 보존하고 설정을 고쳐야 한다.
    - ``retry_after``: 텔레그램이 요구한 최소 대기 시간(초).
    """

    def __init__(
        self,
        message: str,
        *,
        permanent: bool = False,
        configuration: bool = False,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.permanent = permanent
        self.configuration = configuration
        self.retry_after = retry_after


class TelegramNotConfiguredError(TelegramDeliveryError):
    """텔레그램 자격 증명이 없을 때 발생한다."""

    def __init__(self, message: str) -> None:
        super().__init__(message, configuration=True)


def _compact(value: str, limit: int) -> str:
    """메시지용 텍스트를 한 줄로 정리하고 지나치게 긴 내용은 줄인다."""
    clean = re.sub(r"\s+", " ", value).strip()
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"


def _html(value: str, limit: int) -> str:
    return escape(_compact(value, limit), quote=True)


_WEEKDAYS = "월화수목금토일"
_SENTENCE_END = re.compile(r"(?<=[.!?。])\s+")
_LEADING_TAG = re.compile(r"^\s*[\[【<]([^\]】>]{1,30})[\]】>]\s*")
_DATE_KIND_LABELS = {
    "event_start": "일시",
    "event_end": "종료",
    "application_open": "신청 시작",
    "other": "일정",
}


def _date_text(value: date, today: date) -> str:
    weekday = _WEEKDAYS[value.weekday()]
    if value.year == today.year:
        return f"{value.month}월 {value.day}일({weekday})"
    return f"{value.year}년 {value.month}월 {value.day}일({weekday})"


def _relative_day(value: date, today: date) -> str:
    days = (value - today).days
    if days == 0:
        return "D-DAY"
    if days > 0:
        return f"D-{days}"
    return "지남"


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _deadline_label(deadline: str | None) -> str | None:
    """마감일을 '10월 20일(화) · D-26'처럼 요일과 남은 날짜로 표시한다."""
    if not deadline:
        return None
    parsed = _parse_date(deadline)
    if parsed is None:
        return deadline
    today = now_kst().date()
    return f"{_date_text(parsed, today)} · {_relative_day(parsed, today)}"


def _schedule_line(match: ClassifiedNotice) -> str | None:
    """학생이 가장 먼저 알아야 할 날짜 하나를 한 줄로 만든다.

    행동 마감이 있으면 마감을(신청 시작일이 있으면 기간으로), 없으면 가장 가까운
    행사·기타 일정을 보여 준다. 채용설명회처럼 마감 대신 행사일만 있는 공지도
    날짜를 한눈에 볼 수 있게 하기 위함이다.
    """
    today = now_kst().date()
    deadline = _parse_date(match.deadline)
    if match.deadline and deadline is None:
        return f"마감 {_html(match.deadline, 40)}"

    dated: list[tuple[str, date]] = []
    for item in match.dates:
        if not isinstance(item, dict):
            continue
        parsed = _parse_date(item.get("date"))
        if parsed is not None:
            dated.append((str(item.get("kind", "other")), parsed))

    if deadline is not None:
        opens = [
            value
            for kind, value in dated
            if kind == "application_open" and today < value <= deadline
        ]
        label = f"마감 {_date_text(deadline, today)}"
        if opens:
            label = f"신청 {_date_text(min(opens), today)} ~ {_date_text(deadline, today)}"
        return f"{label} · {_relative_day(deadline, today)}"

    upcoming = sorted(
        (value, kind)
        for kind, value in dated
        if kind in _DATE_KIND_LABELS and value >= today
    )
    if not upcoming:
        return None
    value, kind = upcoming[0]
    return f"{_DATE_KIND_LABELS[kind]} {_date_text(value, today)} · {_relative_day(value, today)}"


def _sort_key(match: ClassifiedNotice) -> tuple[int, date, str]:
    """마감·일정이 가까운 공지를 먼저 보여 준다. 지난 마감과 날짜 없는 공지는 뒤로."""
    today = now_kst().date()
    candidates = [_parse_date(match.deadline)]
    candidates.extend(
        _parse_date(item.get("date")) for item in match.dates if isinstance(item, dict)
    )
    upcoming = [value for value in candidates if value is not None and value >= today]
    if upcoming:
        return (0, min(upcoming), match.article.key)
    return (1, date.max, match.article.key)


def _normalize_tag(value: str) -> str:
    return normalize_for_match(value.replace("+", "플러스"))


def clean_title(title: str, board_name: str) -> str:
    """게시판 이름과 같은 말머리를 제거한다.

    예: 게시판 '대학일자리플러스'의 '[대학일자리+] 한미그룹 채용설명회'는
    '한미그룹 채용설명회'로 표시해 '[대학일자리플러스] [대학일자리+]' 중복을 없앤다.
    """
    board = _normalize_tag(board_name)
    cleaned = title.strip()
    while board and (match := _LEADING_TAG.match(cleaned)):
        tag = _normalize_tag(match.group(1))
        if not tag or (tag not in board and board not in tag):
            break
        remainder = cleaned[match.end():].strip()
        if not remainder:
            break
        cleaned = remainder
    return cleaned


def _short_summary(summary: str, limit: int = 150) -> str:
    """요약은 문장 경계에서 줄여 한눈에 읽히는 길이로 맞춘다."""
    clean = re.sub(r"\s+", " ", summary).strip()
    if len(clean) <= limit:
        return clean
    sentences = [part.strip() for part in _SENTENCE_END.split(clean) if part.strip()]
    kept = ""
    for sentence in sentences:
        candidate = f"{kept} {sentence}".strip()
        if len(candidate) > limit:
            break
        kept = candidate
    return kept or _compact(clean, limit)


def _is_redundant_summary(summary: str, title: str) -> bool:
    normalized_summary = normalize_for_match(summary)
    normalized_title = normalize_for_match(title)
    return not normalized_summary or normalized_summary == normalized_title


def _build_item(match: ClassifiedNotice, index: int | None) -> str:
    """공지 하나를 '제목 → 날짜 → 요약 → 할 일 → 링크' 순서로 구성한다."""
    article = match.article
    title = clean_title(article.title, article.board_name)
    prefix = f"{index}. " if index is not None else ""
    update_badge = " (수정됨)" if article.is_update else ""
    lines = [f"{prefix}<b>{_html(title, 160)}</b>{update_badge}"]

    meta = [_html(article.board_name, 30)]
    if schedule := _schedule_line(match):
        meta.append(schedule)
    lines.append(" · ".join(meta))

    if match.summary and not _is_redundant_summary(match.summary, title):
        lines.append(_html(_short_summary(match.summary), 200))

    if match.actions:
        actions = " · ".join(match.actions[:2])
        lines.append(f"할 일: {_html(actions, 140)}")

    if match.delivery == "review":
        detail = match.uncertainties[0] if match.uncertainties else match.reason
        lines.append(f"확인할 점: {_html(detail, 160)}")

    article_link = escape(article.link, quote=True)
    link_line = f'<a href="{article_link}">공지 보기</a>'
    if article.attachments:
        link_line += f" · 첨부 {len(article.attachments)}개"
    lines.append(link_line)
    return "\n".join(lines)


def _paginate(
    header: str,
    items: list[str],
    *,
    subheader: str | None = None,
) -> list[str]:
    """공지 경계에서 메시지를 나누고, 여러 조각이면 헤더에 (1/2)처럼 순번을 붙인다."""

    def head(part_label: str = "") -> str:
        text = header + part_label
        return f"{text}\n{subheader}" if subheader else text

    # 순번 표기가 붙을 여유를 두고 먼저 나눈 뒤 최종 헤더를 붙인다.
    reserve = len(" (99/99)")
    limit = MAX_TELEGRAM_MESSAGE_LENGTH - reserve
    bodies: list[list[str]] = []
    current: list[str] = []
    base_head = head()

    def length_with(candidate: list[str]) -> int:
        return len(base_head) + 2 + len(_ITEM_SEPARATOR.join(candidate))

    for item in items:
        if length_with([*current, item]) <= limit:
            current.append(item)
            continue
        if current:
            bodies.append(current)
            current = []
        if length_with([item]) <= limit:
            current.append(item)
            continue
        # 정상 공지는 한 항목이 제한을 넘지 않지만 비정상적으로 긴 URL도 안전하게 처리한다.
        body_limit = limit - len(base_head) - 2
        bodies.extend([chunk] for chunk in _split_text(item, body_limit))
    if current:
        bodies.append(current)

    total = len(bodies)
    return [
        head(f" ({number}/{total})" if total > 1 else "")
        + "\n\n"
        + _ITEM_SEPARATOR.join(body)
        for number, body in enumerate(bodies, 1)
    ]


def _today_label() -> str:
    today = now_kst().date()
    return _date_text(today, today)


def build_urgent_messages(matched: list[ClassifiedNotice]) -> list[str]:
    """즉시·검토 공지 알림. 공지마다 따로 보내므로 번호 없이 한 건씩 구성한다."""
    if not matched:
        return []
    if all(item.delivery == "review" for item in matched):
        header = "<b>[확인 필요] 대상인지 확인해 주세요</b>"
    else:
        header = "<b>[중요] 놓치면 안 되는 공지</b>"
    numbered = len(matched) > 1
    items = [
        _build_item(match, index if numbered else None)
        for index, match in enumerate(sorted(matched, key=_sort_key), 1)
    ]
    return _paginate(header, items)


def build_digest_messages(matched: list[ClassifiedNotice]) -> list[str]:
    """일일 요약. 마감·일정이 가까운 순으로 정렬한다."""
    ordered = sorted(matched, key=_sort_key)
    items = [_build_item(match, index) for index, match in enumerate(ordered, 1)]
    return _paginate(
        f"<b>{_today_label()} 관심 공지 {len(matched)}건</b>",
        items,
        subheader="마감·일정이 가까운 순",
    )


def build_no_new_message() -> str:
    """새 공지가 없을 때 메시지"""
    return f"{_today_label()} 새로운 공지가 없습니다."


def build_no_relevant_message(total_new: int) -> str:
    """새 공지는 있지만 관련 공지가 없을 때 메시지"""
    return f"{_today_label()} 새 공지 {total_new}건 확인, 관련 공지 없음"


def _run_log_url() -> str | None:
    """GitHub Actions에서 실행 중이면 이번 실행 로그 주소를 만든다."""
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repository = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if not repository or not run_id:
        return None
    return f"{server}/{repository}/actions/runs/{run_id}"


def build_error_message(
    error_detail: str,
    *,
    title: str = "공지 확인 중 오류가 발생했습니다",
    guidance: str | None = None,
) -> str:
    """사람이 읽을 수 있는 실행 오류 알림. 기술적 상세는 한 줄로 줄이고 로그 링크를 붙인다."""
    now = now_kst()
    lines = [
        f"<b>[모니터링 오류] {escape(title)}</b>",
        f"{_date_text(now.date(), now.date())} {now:%H:%M}",
    ]
    if guidance:
        lines.append(escape(guidance))
    lines.append(f"원인: {_html(error_detail, 300)}")
    if log_url := _run_log_url():
        lines.append(f'<a href="{escape(log_url, quote=True)}">실행 로그 보기</a>')
    return "\n".join(lines)


def build_source_outage_message(
    *,
    consecutive_runs: int,
    since: str,
    detail: str,
) -> str:
    """학교 홈페이지 장애가 이어질 때 한 번만 보내는 안내."""
    try:
        started = datetime.fromisoformat(since).astimezone(now_kst().tzinfo)
        since_text = f"{_date_text(started.date(), now_kst().date())} {started:%H:%M}"
    except (TypeError, ValueError):
        since_text = "이전 실행"
    return "\n".join(
        [
            "<b>[안내] 학교 홈페이지에 접속할 수 없습니다</b>",
            f"{since_text}부터 {consecutive_runs}회 연속으로 공지 게시판을 읽지 못했습니다.",
            "모니터 문제는 아니며, 접속이 복구되면 그동안 올라온 공지를 자동으로 확인해 "
            "알려 드립니다. 급한 공지는 학교 홈페이지에서 직접 확인해 주세요.",
            f"원인: {_html(detail, 200)}",
        ]
    )


def build_source_recovered_message(consecutive_runs: int) -> str:
    """장애 안내를 보낸 뒤 접속이 복구되면 보내는 안내."""
    return (
        "<b>[안내] 학교 홈페이지 접속이 복구되었습니다</b>\n"
        f"{consecutive_runs}회 연속 실패 뒤 정상적으로 공지를 확인했습니다. "
        "그동안 올라온 공지도 함께 분석합니다."
    )


def build_new_boards_message(seeded: dict[str, int]) -> str:
    """새로 추가된 게시판을 알림 없이 시드했음을 알린다."""
    boards = ", ".join(f"{escape(name)}(기존 공지 {count}건)" for name, count in seeded.items())
    return (
        "<b>[안내] 새 게시판 확인을 시작합니다</b>\n"
        f"{boards}\n"
        "기존 공지는 '확인함'으로 처리했고, 앞으로 올라오는 공지부터 분석해 알려 드립니다."
    )


def build_first_run_message(seeded_count: int) -> str:
    """최초 실행 시드 처리 안내 메시지"""
    today = now_kst().strftime("%Y-%m-%d")
    return (
        f"{today} 모니터링을 시작합니다.\n"
        f"기존 공지 {seeded_count}건은 '확인함'으로 처리했으며, "
        f"이후 등록되는 새 공지부터 대상 조건과 필요한 행동을 분석해 알려드립니다."
    )


def _split_text(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]

    messages: list[str] = []
    current = ""

    for raw_line in text.split("\n"):
        line = raw_line

        while len(line) > limit:
            if current:
                messages.append(current)
                current = ""
            messages.append(line[:limit])
            line = line[limit:]

        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            if current:
                messages.append(current)
            current = line
        else:
            current = candidate

    if current:
        messages.append(current)

    return messages


def split_message(text: str) -> list[str]:
    """텔레그램 메시지 길이 제한에 맞게 분할"""
    return _split_text(text, MAX_TELEGRAM_MESSAGE_LENGTH)


def _telegram_credentials() -> tuple[str, str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        raise TelegramNotConfiguredError(
            "TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID가 설정되지 않았습니다. "
            "메시지는 outbox에 보존됩니다."
        )
    return token, chat_id


def html_to_plain_text(text: str) -> str:
    """HTML 파싱이 거부된 메시지를 서식 없이라도 전달하기 위한 변환."""
    return unescape(re.sub(r"<[^>]+>", "", text))


def _error_from_response(status: int, payload: dict) -> TelegramDeliveryError:
    description = str(payload.get("description") or f"HTTP {status}")
    code = int(payload.get("error_code") or status)
    parameters = payload.get("parameters") or {}
    if code == 429:
        retry_after = parameters.get("retry_after")
        return TelegramDeliveryError(
            f"텔레그램 전송 속도 제한: {description}",
            retry_after=int(retry_after) if isinstance(retry_after, int) else None,
        )
    lowered = description.lower()
    configuration = code in {401, 403, 404} or any(
        hint in lowered for hint in _CONFIGURATION_ERROR_HINTS
    )
    return TelegramDeliveryError(
        f"텔레그램 API 오류 {code}: {description}",
        permanent=code == 400 and not configuration,
        configuration=configuration,
    )


async def _post_message(
    session: aiohttp.ClientSession,
    token: str,
    chat_id: str,
    text: str,
    *,
    html: bool,
) -> None:
    payload: dict = {
        "chat_id": chat_id,
        "text": text,
        "link_preview_options": {"is_disabled": True},
    }
    if html:
        payload["parse_mode"] = "HTML"
    try:
        async with session.post(
            f"{_TELEGRAM_API}/bot{token}/sendMessage",
            json=payload,
        ) as response:
            try:
                body = await response.json(content_type=None)
            except ValueError:
                body = {}
            if response.status == 200 and isinstance(body, dict) and body.get("ok"):
                return
            raise _error_from_response(response.status, body if isinstance(body, dict) else {})
    except TelegramDeliveryError:
        raise
    except (aiohttp.ClientError, TimeoutError) as exc:
        # 토큰이 포함된 URL이 로그에 남지 않도록 예외 종류만 전달한다.
        raise TelegramDeliveryError(f"텔레그램 연결 실패: {type(exc).__name__}") from None


async def send_telegram_part(text: str) -> None:
    """이미 분할된 텔레그램 메시지 한 조각을 전송한다.

    텔레그램이 HTML 서식을 거부하면 같은 내용을 일반 텍스트로 한 번 더 보낸다.
    """
    if len(text) > MAX_TELEGRAM_MESSAGE_LENGTH:
        raise TelegramDeliveryError(
            "텔레그램 메시지 한 조각이 길이 제한을 초과했습니다.",
            permanent=True,
        )
    token, chat_id = _telegram_credentials()
    timeout = aiohttp.ClientTimeout(total=_TELEGRAM_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
        try:
            await _post_message(session, token, chat_id, text, html=True)
        except TelegramDeliveryError as exc:
            if not (exc.permanent and "parse entities" in str(exc)):
                raise
            logger.warning("텔레그램이 HTML 서식을 거부해 일반 텍스트로 다시 보냅니다: %s", exc)
            await _post_message(session, token, chat_id, html_to_plain_text(text), html=False)


async def send_telegram(text: str) -> int:
    """긴 메시지를 나누어 모두 보내고 전송한 조각 수를 반환한다."""
    parts = split_message(text)
    for index, part in enumerate(parts, 1):
        try:
            await send_telegram_part(part)
        except TelegramDeliveryError as exc:
            raise TelegramDeliveryError(
                f"텔레그램 메시지 전송 실패 ({index - 1}/{len(parts)}개 완료): {exc}",
                permanent=exc.permanent,
                configuration=exc.configuration,
                retry_after=exc.retry_after,
            ) from exc
    return len(parts)


async def notify_error(
    error_detail: str,
    *,
    title: str = "공지 확인 중 오류가 발생했습니다",
    guidance: str | None = None,
) -> bool:
    """실행 실패를 outbox를 거치지 않고 즉시 알린다.

    전송에 성공하면 True를 반환한다. 알림 실패는 로그만 남긴다.
    """
    try:
        await send_telegram(build_error_message(error_detail, title=title, guidance=guidance))
    except TelegramDeliveryError as exc:
        logger.error("오류 알림도 전송하지 못했습니다: %s", exc)
        return False
    return True
