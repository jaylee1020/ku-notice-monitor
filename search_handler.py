"""텔레그램 메시지 수신 → 공지 검색 → 응답 핸들러

GitHub Actions 크론잡(5분 간격)으로 실행되어
텔레그램 봇에 온 새 메시지를 확인하고,
검색어에 맞는 공지를 찾아 답변합니다.
"""

import asyncio
import json
import os
import time
from pathlib import Path

from telegram import Bot

from main import load_config
from feeds import fetch_all_feeds, Article

SEARCH_STATE_FILE = Path(__file__).parent / "search_state.json"

HELP_TEXT = """건국대 공지 검색 봇 사용법

메시지를 보내면 현재 RSS 피드에서 관련 공지를 검색합니다.

예시:
  장학금 → 장학금 관련 공지 검색
  수강신청 → 수강신청 관련 공지 검색
  /help → 이 도움말 표시"""


# --- 상태 관리 ---

def load_search_state() -> dict:
    if SEARCH_STATE_FILE.exists():
        with open(SEARCH_STATE_FILE, "r") as f:
            return json.load(f)
    return {"last_update_id": 0}


def save_search_state(state: dict):
    with open(SEARCH_STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# --- 검색 ---

def keyword_search(query: str, articles: list[Article]) -> list[Article]:
    """키워드 기반 단순 검색"""
    query_lower = query.lower()
    terms = query_lower.split()
    results = []
    for a in articles:
        text = (a.title + " " + a.description + " " + a.board_name).lower()
        if all(t in text for t in terms):
            results.append(a)
    return results


def search_with_gemini(query: str, articles: list[Article]) -> list[Article]:
    """Gemini API로 검색어와 관련된 공지 찾기"""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return []

    try:
        from google import genai
    except ImportError:
        return []

    article_list = ""
    for i, a in enumerate(articles, 1):
        desc = (a.description[:200] if a.description else "")
        article_list += f"{i}. [{a.board_name}] {a.title} - {desc}\n"

    prompt = f"""사용자가 건국대학교 공지사항에서 "{query}"에 대해 검색했습니다.

아래 공지사항 목록에서 검색어와 관련된 공지를 찾아주세요.
관련된 공지만 선별하여 반환해주세요.

반드시 아래 JSON 형식으로만 응답하세요. 다른 텍스트는 포함하지 마세요:
[{{"index": 1, "reason": "관련 이유 한줄 설명"}}, ...]

관련 공지가 없으면 빈 배열 []을 반환하세요.

공지사항 목록:
{article_list}"""

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
        )
        text = response.text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        results = json.loads(text)

        matched = []
        for r in results:
            idx = r.get("index", 0) - 1
            if 0 <= idx < len(articles):
                articles[idx]._search_reason = r.get("reason", "")
                matched.append(articles[idx])
        return matched
    except Exception as e:
        print(f"[Gemini 검색 오류] {e}")
        return []


def search_articles(query: str, articles: list[Article]) -> tuple[list[Article], bool]:
    """검색 실행: Gemini 우선, 실패 시 키워드 폴백. (결과, gemini_used) 반환"""
    gemini_results = search_with_gemini(query, articles)
    if gemini_results:
        return gemini_results, True

    return keyword_search(query, articles), False


# --- 메시지 포맷팅 ---

def format_search_response(query: str, results: list[Article], gemini_used: bool) -> str:
    if not results:
        return f"'{query}' 관련 공지를 찾지 못했습니다."

    method = "AI" if gemini_used else "키워드"
    msg = f"'{query}' 검색 결과 {len(results)}건 ({method} 검색):\n"

    for i, a in enumerate(results[:10], 1):
        reason = getattr(a, "_search_reason", "")
        reason_line = f"  → {reason}\n" if reason else ""
        msg += f"\n{i}. [{a.board_name}] {a.title}\n{reason_line}{a.link}\n"

    if len(results) > 10:
        msg += f"\n... 외 {len(results) - 10}건"

    return msg


# --- 메인 실행 ---

async def run():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        print("[검색] TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID가 설정되지 않았습니다.")
        return

    bot = Bot(token=token)
    state = load_search_state()

    # offset: 마지막 처리한 update_id + 1 부터 조회
    offset = state.get("last_update_id", 0)
    if offset:
        offset += 1

    updates = await bot.get_updates(offset=offset, timeout=10)

    if not updates:
        print("[검색] 새 메시지 없음")
        return

    print(f"[검색] {len(updates)}건의 새 업데이트")

    # 처리할 메시지가 있을 때만 RSS 피드 수집
    config = load_config()
    articles = None

    for update in updates:
        state["last_update_id"] = update.update_id
        save_search_state(state)

        if not update.message or not update.message.text:
            continue

        msg = update.message

        # 설정된 채팅에서 온 메시지만 처리
        if str(msg.chat_id) != chat_id:
            continue

        query = msg.text.strip()

        # /help 명령
        if query in ("/help", "/start"):
            await bot.send_message(chat_id=chat_id, text=HELP_TEXT)
            continue

        # 빈 메시지 무시
        if not query or query.startswith("/"):
            continue

        # 처음 검색 시 RSS 피드 한 번만 수집
        if articles is None:
            print("[검색] RSS 피드 수집 중...")
            articles = fetch_all_feeds(config)
            print(f"[검색] {len(articles)}건 수집 완료")

        # 검색 실행
        print(f"[검색] 쿼리: {query}")
        results, gemini_used = search_articles(query, articles)
        response = format_search_response(query, results, gemini_used)
        await bot.send_message(chat_id=chat_id, text=response)
        print(f"[검색] 응답 전송 ({len(results)}건)")

    print("[검색] 완료")


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
