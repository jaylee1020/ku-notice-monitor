"""텔레그램 봇 알림 모듈"""

import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from html import escape
from zoneinfo import ZoneInfo

from telegram import Bot

from .constants import MAX_TELEGRAM_MESSAGE_LENGTH
from .models import ClassifiedNotice

logger = logging.getLogger(__name__)

# GitHub Actions 러너는 UTC이므로, 사용자에게 보이는 날짜/시각은 KST로 표기한다.
_KST = ZoneInfo("Asia/Seoul")
_ITEM_SEPARATOR = "\n\n────────\n\n"

_CATEGORY_LABELS = {
    "academic": ("🎓", "학사"),
    "tuition": ("💳", "등록금"),
    "scholarship": ("🎁", "장학"),
    "career": ("💼", "취업·진로"),
    "international": ("🌏", "국제교류"),
    "event": ("🎉", "행사"),
    "campus_life": ("🏫", "학생생활"),
    "administrative": ("📋", "행정"),
    "other": ("📌", "기타"),
}

_AUDIENCE_LABELS = {
    "eligible": ("✅", "내 조건과 일치"),
    "possibly_eligible": ("⚠️", "일부 조건 확인"),
    "unknown": ("⚠️", "대상 확인 필요"),
    "ineligible": ("⛔", "대상 아님"),
}


class TelegramDeliveryError(RuntimeError):
    """텔레그램 메시지가 완전히 전송되지 않았을 때 발생한다."""


class TelegramNotConfiguredError(TelegramDeliveryError):
    """텔레그램 자격 증명이 없을 때 발생한다."""


@dataclass(frozen=True)
class DeliveryResult:
    sent_parts: int
    total_parts: int

    @property
    def complete(self) -> bool:
        return self.sent_parts == self.total_parts


def _now_kst() -> datetime:
    return datetime.now(_KST)


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
    days = (deadline_date - _now_kst().date()).days
    if days == 0:
        relative = "D-DAY"
    elif days > 0:
        relative = f"D-{days}"
    else:
        relative = f"D+{abs(days)}"
    if deadline_date.year == _now_kst().year:
        displayed = f"{deadline_date.month}월 {deadline_date.day}일"
    else:
        displayed = (
            f"{deadline_date.year}년 {deadline_date.month}월 {deadline_date.day}일"
        )
    return f"{displayed} · {relative}"


def _build_items(matched: list[ClassifiedNotice]) -> str:
    items: list[str] = []
    for match in matched:
        article = match.article
        category_icon, category_label = _CATEGORY_LABELS.get(
            match.category, ("📌", match.category)
        )
        update_badge = " · <b>수정됨</b>" if article.is_update else ""
        lines = [
            (
                f"{category_icon} <b>{_html(category_label, 30)}</b>"
                f" · {_html(article.board_name, 50)}{update_badge}"
            ),
            f"<b>{_html(article.title, 180)}</b>",
        ]

        if match.summary and match.summary != article.title:
            lines.append(f"<i>{_html(match.summary, 220)}</i>")

        audience_icon, audience_label = _AUDIENCE_LABELS.get(
            match.audience_fit, ("⚠️", match.audience_fit)
        )
        lines.append(
            f"\n{audience_icon} <b>{_html(audience_label, 40)}</b>"
        )
        if match.delivery == "review" and match.uncertainties:
            lines.append(_html(match.uncertainties[0], 180))
        elif match.reason:
            lines.append(f"💡 {_html(match.reason, 180)}")

        if deadline := _deadline_label(match.deadline):
            lines.append(f"⏰ <b>마감</b>  {_html(deadline, 50)}")
        if match.actions:
            actions = " · ".join(match.actions[:2])
            lines.append(f"👉 <b>할 일</b>  {_html(actions, 180)}")
        if match.benefits:
            benefits = " · ".join(match.benefits[:2])
            lines.append(f"🎁 <b>혜택</b>  {_html(benefits, 180)}")

        article_link = escape(article.link, quote=True)
        lines.append(f'\n🔗 <a href="{article_link}">공지 열기 →</a>')
        if article.attachments:
            lines[-1] += f"  ·  📎 첨부 {len(article.attachments)}개"
        items.append("\n".join(lines))
    return _ITEM_SEPARATOR.join(items)


def build_urgent_message(matched: list[ClassifiedNotice], total_new: int) -> str:
    del total_new  # 전체 수보다 실제로 확인할 항목 수를 전면에 보여준다.
    count = f" · {len(matched)}건" if len(matched) > 1 else ""
    header = f"🚨 <b>지금 확인할 공지</b>{count}"
    return header + "\n\n" + _build_items(matched)


def build_digest_message(matched: list[ClassifiedNotice]) -> str:
    header = f"🗂 <b>오늘의 관심 공지</b> · {len(matched)}건"
    return header + "\n\n" + _build_items(matched)


def build_relevant_message(matched: list[ClassifiedNotice], total_new: int) -> str:
    """기존 호출부 호환용: 관련 공지를 일반 요약 형태로 생성한다."""
    return (
        f"📬 <b>새 관심 공지</b> · {len(matched)}건"
        f" <i>(전체 신규 {total_new}건)</i>\n\n"
        + _build_items(matched)
    )


def build_no_new_message() -> str:
    """새 공지가 없을 때 메시지"""
    today = _now_kst().strftime("%Y-%m-%d")
    return f"{today} 새로운 공지가 없습니다."


def build_no_relevant_message(total_new: int) -> str:
    """새 공지는 있지만 관련 공지가 없을 때 메시지"""
    today = _now_kst().strftime("%Y-%m-%d")
    return f"{today} 새 공지 {total_new}건 확인, 관련 공지 없음"


def build_error_message(error_detail: str) -> str:
    """워크플로우 오류 알림 메시지"""
    today = _now_kst().strftime("%Y-%m-%d %H:%M")
    return (
        f"🔴 <b>모니터링 실패</b> · {today}\n"
        f"{_html(error_detail, 1000)}"
    )


def build_first_run_message(seeded_count: int) -> str:
    """최초 실행 시드 처리 안내 메시지"""
    today = _now_kst().strftime("%Y-%m-%d")
    return (
        f"{today} 모니터링을 시작합니다.\n"
        f"기존 공지 {seeded_count}건은 '확인함'으로 처리했으며, "
        f"이후 등록되는 새 공지부터 대상 조건과 필요한 행동을 분석해 알려드립니다."
    )


def split_message(text: str) -> list[str]:
    """텔레그램 메시지 길이 제한에 맞게 분할"""
    limit = MAX_TELEGRAM_MESSAGE_LENGTH
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


def _telegram_credentials() -> tuple[str, str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        raise TelegramNotConfiguredError(
            "TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID가 설정되지 않았습니다. "
            "메시지는 outbox에 보존됩니다."
        )
    return token, chat_id


async def send_telegram_part(text: str) -> None:
    """이미 분할된 텔레그램 메시지 한 조각을 전송한다."""
    if len(text) > MAX_TELEGRAM_MESSAGE_LENGTH:
        raise ValueError("텔레그램 메시지 한 조각이 길이 제한을 초과했습니다.")
    token, chat_id = _telegram_credentials()
    bot = Bot(token=token)
    await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def send_telegram(text: str) -> DeliveryResult:
    """모든 조각의 전송 성공을 보장하며, 일부 실패도 호출자에게 알린다."""
    parts = split_message(text)
    sent = 0
    for i, msg in enumerate(parts, 1):
        try:
            await send_telegram_part(msg)
            sent += 1
        except Exception as e:
            logger.error("텔레그램 메시지 전송 실패 (%d/%d): %s", i, len(parts), e)
            raise TelegramDeliveryError(
                f"텔레그램 메시지 전송 실패 ({sent}/{len(parts)}개 완료): {e}"
            ) from e
    logger.info("텔레그램 메시지 전송 완료 (%d/%d개 전송)", sent, len(parts))
    return DeliveryResult(sent_parts=sent, total_parts=len(parts))


async def notify_relevant(
    matched: list[ClassifiedNotice],
    total_new: int,
) -> None:
    """기존 호출부 호환용 관련 공지 전송."""
    await notify_digest(matched)


async def notify_urgent(matched: list[ClassifiedNotice], total_new: int) -> None:
    await send_telegram(build_urgent_message(matched, total_new))


async def notify_digest(matched: list[ClassifiedNotice]) -> None:
    await send_telegram(build_digest_message(matched))


async def notify_no_new() -> None:
    """새 공지 없음 알림"""
    text = build_no_new_message()
    await send_telegram(text)


async def notify_no_relevant(total_new: int) -> None:
    """새 공지는 있지만 관련 공지 없음 알림"""
    text = build_no_relevant_message(total_new)
    await send_telegram(text)


async def notify_error(error_detail: str) -> DeliveryResult | None:
    """워크플로우 오류 발생 시 텔레그램으로 알림"""
    text = build_error_message(error_detail)
    try:
        return await send_telegram(text)
    except TelegramDeliveryError as exc:
        logger.error("오류 알림도 전송하지 못했습니다: %s", exc)
        return None


async def notify_first_run(seeded_count: int) -> None:
    """최초 실행 시 기존 공지를 시드 처리했음을 한 건의 메시지로 알림"""
    text = build_first_run_message(seeded_count)
    await send_telegram(text)
