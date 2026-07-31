"""텔레그램 봇 알림 모듈"""

import logging
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

from telegram import Bot

from constants import MAX_TELEGRAM_MESSAGE_LENGTH
from models import ClassifiedNotice

logger = logging.getLogger(__name__)

# GitHub Actions 러너는 UTC이므로, 사용자에게 보이는 날짜/시각은 KST로 표기한다.
_KST = ZoneInfo("Asia/Seoul")


def _now_kst() -> datetime:
    return datetime.now(_KST)


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
    return f"{deadline} ({relative})"


def _build_items(matched: list[ClassifiedNotice]) -> str:
    items: list[str] = []
    for index, match in enumerate(matched, 1):
        article = match.article
        update_badge = " [수정]" if article.is_update else ""
        review_badge = " [대상 확인 필요]" if match.delivery == "review" else ""
        item = (
            f"\n{index}. [{article.board_name}] "
            f"{article.title}{update_badge}{review_badge}\n"
        )
        if match.summary and match.summary != article.title:
            item += f"요약: {match.summary}\n"
        item += f"이유: {match.reason}\n"
        if match.audience_fit != "eligible":
            item += f"대상 판정: {match.audience_fit}\n"
        if deadline := _deadline_label(match.deadline):
            item += f"⏰ 마감: {deadline}\n"
        if match.actions:
            item += "✅ 할 일: " + " · ".join(match.actions) + "\n"
        if match.uncertainties:
            item += "⚠️ 확인 필요: " + " · ".join(match.uncertainties[:2]) + "\n"
        item += article.link
        if article.attachments:
            filenames = ", ".join(att.filename for att in article.attachments)
            item += f"\n📎 {filenames}"
        items.append(item)
    return "\n".join(items)


def build_urgent_message(matched: list[ClassifiedNotice], total_new: int) -> str:
    today = _now_kst().strftime("%Y-%m-%d %H:%M")
    header = f"🚨 {today} 바로 확인할 공지 {len(matched)}건 (신규 {total_new}건)\n"
    return header + _build_items(matched)


def build_digest_message(matched: list[ClassifiedNotice]) -> str:
    today = _now_kst().strftime("%Y-%m-%d")
    header = f"🗂 {today} 관심 공지 요약 {len(matched)}건\n"
    return header + _build_items(matched)


def build_relevant_message(matched: list[ClassifiedNotice], total_new: int) -> str:
    """기존 호출부 호환용: 관련 공지를 일반 요약 형태로 생성한다."""
    today = _now_kst().strftime("%Y-%m-%d")
    return f"{today} 새 공지 {total_new}건 중 관련 {len(matched)}건\n" + _build_items(matched)


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
    return f"[오류] {today} 모니터링 실패\n{error_detail}"


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


async def send_telegram(text: str) -> None:
    """텔레그램 봇으로 메시지 전송"""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        logger.warning(
            "TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID가 설정되지 않았습니다. "
            "메시지를 전송하지 않고 콘솔에 출력합니다."
        )
        logger.info("--- 메시지 미리보기 ---\n%s", text)
        return

    bot = Bot(token=token)
    parts = split_message(text)
    sent = 0
    for i, msg in enumerate(parts, 1):
        try:
            await bot.send_message(chat_id=chat_id, text=msg)
            sent += 1
        except Exception as e:
            # 일부 조각 전송 실패가 전체 실행을 중단시키지 않도록 한다.
            # (예외를 올리면 main에서 notify_error가 다시 텔레그램 전송을 시도해 실패가 연쇄될 수 있음)
            logger.error("텔레그램 메시지 전송 실패 (%d/%d): %s", i, len(parts), e)
    logger.info("텔레그램 메시지 전송 완료 (%d/%d개 전송)", sent, len(parts))


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


async def notify_error(error_detail: str) -> None:
    """워크플로우 오류 발생 시 텔레그램으로 알림"""
    text = build_error_message(error_detail)
    await send_telegram(text)


async def notify_first_run(seeded_count: int) -> None:
    """최초 실행 시 기존 공지를 시드 처리했음을 한 건의 메시지로 알림"""
    text = build_first_run_message(seeded_count)
    await send_telegram(text)
