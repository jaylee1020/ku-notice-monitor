"""텔레그램 메시지 수신 → 공지 검색 → 응답 핸들러

GitHub Actions 크론잡(30분 간격)으로 실행되어
텔레그램 봇에 온 새 메시지를 확인하고,
검색어에 맞는 공지를 찾아 답변합니다.
"""

import asyncio
import json
import os
from pathlib import Path

from telegram import Bot

from main import load_config
from feeds import fetch_all_feeds, load_articles_cache, Article
from notifier import split_message

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
        try:
            with open(SEARCH_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, KeyError) as e:
            print(f"[검색 상태 오류] search_state.json 손상, 초기화합니다: {e}")
    return {"last_update_id": 0}


def save_search_state(state: dict):
    with open(SEARCH_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# --- 검색 결과 타입 ---

class SearchResult:
    """검색 결과 항목 (Article + 선택적 사유)"""
    def __init__(self, article: Article, reason: str = ""):
        self.article = article
        self.reason = reason


# --- 검색 ---

def keyword_search(query: str, articles: list[Article]) -> list[SearchResult]:
    """키워드 기반 단순 검색"""
    query_lower = query.lower()
    terms = query_lower.split()
    results = []
    for a in articles:
        text = (a.title + " " + a.description + " " + a.board_name).lower()
        if all(t in text for t in terms):
            results.append(SearchResult(a))
    return results


def search_with_gemini(query: str, articles: list[Article]) -> list[SearchResult] | None:
    """Gemini API로 검색어와 관련된 공지 찾기. 에러 시 None, 결과 없으면 빈 리스트."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return None

    try:
        from google import genai
    except ImportError:
        return None

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
        raw_results = json.loads(text)

        matched = []
        for r in raw_results:
            idx = r.get("index", 0) - 1
            if 0 <= idx < len(articles):
                matched.append(SearchResult(articles[idx], r.get("reason", "")))
        return matched
    except Exception as e:
        print(f"[Gemini 검색 오류] {e}")
        return None


def search_articles(query: str, articles: list[Article]) -> tuple[list[SearchResult], bool]:
    """검색 실행: Gemini 우선, 실패(None) 시 키워드 폴백. (결과, gemini_used) 반환"""
    gemini_results = search_with_gemini(query, articles)
    if gemini_results is not None:
        return gemini_results, True

    return keyword_search(query, articles), False


# --- 메시지 포맷팅 ---

def format_search_response(query: str, results: list[SearchResult], gemini_used: bool) -> str:
    if not results:
        return f"'{query}' 관련 공지를 찾지 못했습니다."

    method = "AI" if gemini_used else "키워드"
    msg = f"'{query}' 검색 결과 {len(results)}건 ({method} 검색):\n"

    for i, sr in enumerate(results[:10], 1):
        reason_line = f"  → {sr.reason}\n" if sr.reason else ""
        msg += f"\n{i}. [{sr.article.board_name}] {sr.article.title}\n{reason_line}{sr.article.link}\n"

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

    try:
        updates = await bot.get_updates(offset=offset, timeout=10)
    except Exception as e:
        print(f"[검색] 텔레그램 업데이트 조회 실패: {e}")
        return

    if not updates:
        print("[검색] 새 메시지 없음")
        return

    print(f"[검색] {len(updates)}건의 새 업데이트")

    # 처리할 메시지가 있을 때만 RSS 피드 수집
    config = load_config()
    articles = None

    for update in updates:
        state["last_update_id"] = update.update_id

        if not update.message or not update.message.text:
            continue

        msg = update.message

        # 설정된 채팅에서 온 메시지만 처리
        if str(msg.chat_id) != chat_id:
            continue

        query = msg.text.strip()

        # /help 명령
        if query in ("/help", "/start"):
            try:
                await bot.send_message(chat_id=chat_id, text=HELP_TEXT)
            except Exception as e:
                print(f"[검색] 도움말 전송 실패: {e}")
            continue

        # 빈 메시지 무시
        if not query or query.startswith("/"):
            continue

        # 캐시에서 공지 로드 (RSS 재수집 없이 즉시 검색)
        if articles is None:
            articles = load_articles_cache()
            if articles:
                print(f"[검색] 캐시에서 {len(articles)}건 로드")
            else:
                print("[검색] 캐시 없음, RSS 피드 수집 중...")
                articles = fetch_all_feeds(config)
                print(f"[검색] {len(articles)}건 수집 완료")

        # 검색 실행
        print(f"[검색] 쿼리: {query}")
        results, gemini_used = search_articles(query, articles)
        response = format_search_response(query, results, gemini_used)
        try:
            for part in split_message(response):
                await bot.send_message(chat_id=chat_id, text=part)
        except Exception as e:
            print(f"[검색] 응답 전송 실패: {e}")
            continue
        print(f"[검색] 응답 전송 ({len(results)}건)")

    save_search_state(state)
    print("[검색] 완료")


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
