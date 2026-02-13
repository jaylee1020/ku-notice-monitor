"""텔레그램 봇 알림 모듈"""

import os
from datetime import datetime

from telegram import Bot

from feeds import Article

MAX_MESSAGE_LENGTH = 4096


def build_relevant_message(
    matched: list[tuple[Article, int, str]],
    total_new: int,
) -> str:
    """관련 공지가 있을 때 텔레그램 메시지 생성"""
    today = datetime.now().strftime("%Y-%m-%d")
    header = f"{today} 새 공지 {total_new}건 중 관련 {len(matched)}건\n"

    items = []
    for i, (article, score, reason) in enumerate(matched, 1):
        item = (
            f"\n{i}. [{article.board_name}] {article.title}\n"
            f"→ {reason}\n"
            f"{article.link}"
        )
        items.append(item)

    return header + "\n".join(items)


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
    """텔레그램 봇으로 메시지 전송"""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        print("[텔레그램] 봇 토큰 또는 채팅 ID가 설정되지 않았습니다.")
        print("[텔레그램] 환경변수 TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID를 설정하세요.")
        print("--- 메시지 미리보기 ---")
        print(text)
        return

    bot = Bot(token=token)
    for msg in split_message(text):
        try:
            await bot.send_message(chat_id=chat_id, text=msg)
        except Exception as e:
            print(f"[텔레그램 전송 오류] {e}")


async def notify_relevant(
    matched: list[tuple[Article, int, str]],
    total_new: int,
):
    """관련 공지를 텔레그램으로 전송"""
    text = build_relevant_message(matched, total_new)
    await send_telegram(text)


