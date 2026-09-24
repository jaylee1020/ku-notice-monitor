"""텔레그램 메시지 구성과 전송."""

import logging
import os
import re
from datetime import date
from html import escape, unescape

import aiohttp

from .constants import MAX_TELEGRAM_MESSAGE_LENGTH
from .models import ClassifiedNotice
from .util import now_kst

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


def _deadline_label(deadline: str | None) -> str | None:
    if not deadline:
        return None
    try:
        deadline_date = date.fromisoformat(deadline)
    except ValueError:
        return deadline
    today = now_kst().date()
    days = (deadline_date - today).days
    if days == 0:
        relative = "D-DAY"
    elif days > 0:
        relative = f"D-{days}"
    else:
        relative = f"D+{abs(days)}"
    if deadline_date.year == today.year:
        displayed = f"{deadline_date.month}월 {deadline_date.day}일"
    else:
        displayed = (
            f"{deadline_date.year}년 {deadline_date.month}월 {deadline_date.day}일"
        )
    return f"{displayed} ({relative})"


def _build_item(match: ClassifiedNotice, index: int) -> str:
    article = match.article
    update_badge = " [수정]" if article.is_update else ""
    lines = [
        (
            f"{index}. [{_html(article.board_name, 50)}] "
            f"{_html(article.title, 180)}{update_badge}"
        )
    ]

    if match.summary and match.summary != article.title:
        lines.append(f"→ {_html(match.summary, 220)}")

    if match.delivery == "review":
        detail = match.uncertainties[0] if match.uncertainties else match.reason
        lines.append(f"확인 필요: {_html(detail, 180)}")

    if deadline := _deadline_label(match.deadline):
        lines.append(f"마감: {_html(deadline, 50)}")
    if match.actions:
        actions = " · ".join(match.actions[:2])
        lines.append(f"할 일: {_html(actions, 180)}")

    article_link = escape(article.link, quote=True)
    lines.append(f'<a href="{article_link}">공지 보기</a>')
    if article.attachments:
        lines[-1] += f" · 첨부 {len(article.attachments)}개"
    return "\n".join(lines)


def _paginate(header: str, matched: list[ClassifiedNotice]) -> list[str]:
    """공지 경계에서 메시지를 나누고 각 조각에 헤더를 유지한다."""
    parts: list[str] = []
    current_items: list[str] = []

    def flush() -> None:
        if current_items:
            parts.append(header + "\n\n" + _ITEM_SEPARATOR.join(current_items))
            current_items.clear()

    for index, match in enumerate(matched, 1):
        item = _build_item(match, index)
        candidate = header + "\n\n" + _ITEM_SEPARATOR.join([*current_items, item])
        if len(candidate) <= MAX_TELEGRAM_MESSAGE_LENGTH:
            current_items.append(item)
            continue
        flush()
        if len(header) + 2 + len(item) <= MAX_TELEGRAM_MESSAGE_LENGTH:
            current_items.append(item)
            continue
        # 정상 공지는 한 항목이 제한을 넘지 않지만 비정상적으로 긴 URL도 안전하게 처리한다.
        body_limit = MAX_TELEGRAM_MESSAGE_LENGTH - len(header) - 2
        parts.extend(header + "\n\n" + body for body in _split_text(item, body_limit))
    flush()
    return parts


def build_urgent_messages(
    matched: list[ClassifiedNotice],
    total_new: int,
) -> list[str]:
    today = now_kst().strftime("%Y-%m-%d")
    return _paginate(f"{today} 새 공지 {total_new}건 중 관련 {len(matched)}건", matched)


def build_digest_messages(matched: list[ClassifiedNotice]) -> list[str]:
    today = now_kst().strftime("%Y-%m-%d")
    return _paginate(f"{today} 관심 공지 {len(matched)}건", matched)


def build_no_new_message() -> str:
    """새 공지가 없을 때 메시지"""
    today = now_kst().strftime("%Y-%m-%d")
    return f"{today} 새로운 공지가 없습니다."


def build_no_relevant_message(total_new: int) -> str:
    """새 공지는 있지만 관련 공지가 없을 때 메시지"""
    today = now_kst().strftime("%Y-%m-%d")
    return f"{today} 새 공지 {total_new}건 확인, 관련 공지 없음"


def build_error_message(error_detail: str) -> str:
    """워크플로우 오류 알림 메시지"""
    today = now_kst().strftime("%Y-%m-%d %H:%M")
    return f"[오류] {today} 모니터링 실패\n{_html(error_detail, 1000)}"


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


async def notify_error(error_detail: str) -> None:
    """실행 실패를 outbox를 거치지 않고 즉시 알린다. 알림 실패는 로그만 남긴다."""
    try:
        await send_telegram(build_error_message(error_detail))
    except TelegramDeliveryError as exc:
        logger.error("오류 알림도 전송하지 못했습니다: %s", exc)
