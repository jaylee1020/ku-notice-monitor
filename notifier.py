"""텔레그램 봇 알림 모듈"""

import html
import logging
import os
from datetime import datetime

from telegram import Bot

from feeds import Article

logger = logging.getLogger("monitor.notifier")

MAX_MESSAGE_LENGTH = 4096

# 관련도 점수별 아이콘
_SCORE_ICON = {5: "🔴", 4: "🟠", 3: "🟡"}


def build_relevant_message(
    matched: list[tuple[Article, int, str]],
    total_new: int,
) -> str:
    """관련 공지가 있을 때 텔레그램 HTML 메시지 생성"""
    today = datetime.now().strftime("%Y-%m-%d")
    header = (
        f"<b>{today}</b> 새 공지 {total_new}건 중 "
        f"<b>관련 {len(matched)}건</b>\n"
    )

    items = []
    for i, (article, score, reason) in enumerate(matched, 1):
        icon = _SCORE_ICON.get(score, "⚪")
        title_escaped = html.escape(article.title)
        reason_escaped = html.escape(reason)
        item = (
            f"\n{icon} <b>{i}. [{article.board_name}]</b>\n"
            f"<a href=\"{article.link}\">{title_escaped}</a>\n"
            f"<i>→ {reason_escaped}</i>"
        )
        items.append(item)

    return header + "\n".join(items)


def build_no_new_message() -> str:
    """새 공지가 없을 때 메시지"""
    today = datetime.now().strftime("%Y-%m-%d")
    return f"<b>{today}</b> 새로운 공지가 없습니다."


def build_no_relevant_message(total_new: int) -> str:
    """새 공지는 있지만 관련 공지가 없을 때 메시지"""
    today = datetime.now().strftime("%Y-%m-%d")
    return f"<b>{today}</b> 새 공지 {total_new}건 확인, 관련 공지 없음"


def split_message(text: str) -> list[str]:
    """텔레그램 메시지 길이 제한에 맞게 분할"""
    if len(text) <= MAX_MESSAGE_LENGTH:
        return [text]

    messages = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > MAX_MESSAGE_LENGTH:
            messages.append(current)
            current = line
        else:
            current = current + "\n" + line if current else line
    if current:
        messages.append(current)
    return messages


async def send_telegram(text: str):
    """텔레그램 봇으로 HTML 메시지 전송"""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        logger.warning("봇 토큰 또는 채팅 ID 미설정, 메시지 미리보기만 출력합니다.")
        logger.info("--- 메시지 미리보기 ---\n%s", text)
        return

    bot = Bot(token=token)
    for msg in split_message(text):
        await bot.send_message(
            chat_id=chat_id,
            text=msg,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )


async def notify_relevant(
    matched: list[tuple[Article, int, str]],
    total_new: int,
):
    """관련 공지를 텔레그램으로 전송"""
    text = build_relevant_message(matched, total_new)
    await send_telegram(text)


async def notify_no_new():
    """새 공지 없음 알림"""
    text = build_no_new_message()
    await send_telegram(text)


async def notify_no_relevant(total_new: int):
    """새 공지는 있지만 관련 공지 없음 알림"""
    text = build_no_relevant_message(total_new)
    await send_telegram(text)
