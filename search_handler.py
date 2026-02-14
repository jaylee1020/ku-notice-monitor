"""텔레그램 메시지 수신 → 공지 검색 → 응답 핸들러

GitHub Actions(5분 간격)으로 실행되어
텔레그램 봇에 온 새 메시지를 확인하고,
검색어에 맞는 공지를 찾아 답변합니다.
"""

import asyncio
import json
import os
from pathlib import Path
from datetime import datetime

from telegram import Bot

from main import load_config
from feeds import Article, fetch_all_feeds

SEARCH_STATE_FILE = Path(__file__).parent / "search_state.json"
LATEST_ARTICLES_FILE = Path(__file__).parent / "latest_articles.json"
MAX_TRACKED_UPDATES = 200
MAX_CACHE_AGE_SECONDS = 36 * 60 * 60

HELP_TEXT = """건국대 공지 검색 봇 사용법

메시지를 보내면 현재 RSS 피드에서 관련 공지를 검색합니다.

예시:
  장학금 → 장학금 관련 공지 검색
  수강신청 → 수강신청 관련 공지 검색
  /help → 이 도움말 표시"""


def load_search_state() -> dict:
    if SEARCH_STATE_FILE.exists():
        try:
            with open(SEARCH_STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"[검색] 상태 파일 읽기 실패: {e}")
            return {"last_update_id": 0, "processed_update_ids": []}

        if not isinstance(state, dict):
            return {"last_update_id": 0, "processed_update_ids": []}

        last_update_id = state.get("last_update_id", 0)
        try:
            last_update_id = int(last_update_id)
        except (TypeError, ValueError):
            last_update_id = 0

        processed = []
        for update_id in state.get("processed_update_ids", []):
            try:
                processed.append(int(update_id))
            except (TypeError, ValueError):
                continue

        return {
            "last_update_id": last_update_id,
            "processed_update_ids": processed[-MAX_TRACKED_UPDATES:],
        }

    return {"last_update_id": 0, "processed_update_ids": []}


def save_search_state(state: dict):
    payload = {
        "last_update_id": state.get("last_update_id", 0),
        "processed_update_ids": state.get("processed_update_ids", [])[-MAX_TRACKED_UPDATES:],
    }

    tmp_file = SEARCH_STATE_FILE.with_suffix(".tmp")
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp_file.replace(SEARCH_STATE_FILE)


def _to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_latest_articles() -> tuple[list[Article], str | None]:
    if not LATEST_ARTICLES_FILE.exists():
        print("[검색] 최신 스크랩 캐시가 없습니다.")
        return [], None

    try:
        with open(LATEST_ARTICLES_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[검색] 스크랩 캐시 읽기 실패: {e}")
        return [], None

    updated_at: str | None = None
    raw_articles = []

    if isinstance(payload, dict):
        updated_at = payload.get("updated_at")
        raw_articles = payload.get("articles", [])
    elif isinstance(payload, list):
        raw_articles = payload
    else:
        return [], None

    if not isinstance(raw_articles, list):
        return [], updated_at

    articles = []
    for item in raw_articles:
        if not isinstance(item, dict):
            continue

        articles.append(Article(
            id=str(item.get("id", "")),
            title=item.get("title", ""),
            link=item.get("link", ""),
            pub_date=item.get("pub_date", ""),
            author=item.get("author", ""),
            description=item.get("description", ""),
            board_name=item.get("board_name", ""),
            board_id=_to_int(item.get("board_id", 0)),
            view_count=_to_int(item.get("view_count", 0)),
            is_pinned=bool(item.get("is_pinned", False)),
            attachment_count=_to_int(item.get("attachment_count", 0)),
        ))

    return articles, updated_at


def is_cache_fresh(updated_at: str | None) -> bool:
    if not updated_at:
        return False
    try:
        cached = datetime.fromisoformat(updated_at)
    except ValueError:
        return False

    elapsed = (datetime.now() - cached).total_seconds()
    return elapsed <= MAX_CACHE_AGE_SECONDS


def keyword_search(query: str, articles: list[Article]) -> list[Article]:
    """키워드 기반 단순 검색"""
    query_lower = query.lower()
    terms = query_lower.split()
    results = []
    for a in articles:
        text = (a.title + " " + a.description + " " + a.board_name).lower()
        if all(term in text for term in terms):
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

    article_lines = []
    for i, a in enumerate(articles, 1):
        desc = (a.description[:200] if a.description else "")
        article_lines.append(f"{i}. [{a.board_name}] {a.title} - {desc}")
    article_list = "\n".join(article_lines) + "\n" if article_lines else ""

    prompt = f"""사용자가 건국대학교 공지사항에서 \"{query}\"에 대해 검색했습니다.

아래 공지사항 목록에서 검색어와 관련된 공지를 찾아주세요.
관련된 공지만 선별하여 반환해주세요.

반드시 아래 JSON 형식으로만 응답하세요. 다른 텍스트는 포함하지 마세요:
[{{\"index\": 1, \"reason\": \"관련 이유 한줄 설명\"}}, ...]

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
    """검색 실행: Gemini 우선, 실패 시 키워드 폴백"""
    gemini_results = search_with_gemini(query, articles)
    if gemini_results:
        return gemini_results, True
    return keyword_search(query, articles), False


def format_search_response(query: str, results: list[Article], gemini_used: bool) -> str:
    if not results:
        return f"'{query}' 관련 공지를 찾지 못했습니다."

    method = "AI" if gemini_used else "키워드"
    parts = [f"'{query}' 검색 결과 {len(results)}건 ({method} 검색):"]

    for i, a in enumerate(results[:10], 1):
        reason = getattr(a, "_search_reason", "")
        reason_line = f"  → {reason}\n" if reason else ""
        parts.append(f"\n{i}. [{a.board_name}] {a.title}\n{reason_line}{a.link}")

    if len(results) > 10:
        parts.append(f"\n... 외 {len(results) - 10}건")

    return "\n".join(parts)


async def run():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        print("[검색] TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID가 설정되지 않았습니다.")
        return

    bot = Bot(token=token)
    state = load_search_state()
    processed_update_ids = set(state.get("processed_update_ids", []))

    offset = state.get("last_update_id", 0)
    if offset:
        offset += 1

    updates = await bot.get_updates(offset=offset, timeout=10)
    if not updates:
        print("[검색] 새 메시지 없음")
        return

    print(f"[검색] {len(updates)}건의 새 업데이트")

    config = load_config()
    articles = None

    for update in updates:
        update_id = int(update.update_id)

        # 항상 offset을 전진시켜 다음 실행 시 같은 업데이트를 다시 받지 않도록 함
        state["last_update_id"] = max(state.get("last_update_id", 0), update_id)

        if update_id in processed_update_ids:
            print(f"[검색] 중복 업데이트 스킵: {update_id}")
            continue

        try:
            if not update.message or not update.message.text:
                continue

            msg = update.message
            if str(msg.chat_id) != chat_id:
                continue

            query = msg.text.strip()
            if query in ("/help", "/start"):
                await bot.send_message(chat_id=chat_id, text=HELP_TEXT)
                continue

            if not query or query.startswith("/"):
                continue

            if articles is None:
                cached_articles, cached_at = load_latest_articles()
                if cached_articles and is_cache_fresh(cached_at):
                    print(f"[검색] 최신 스크랩 캐시 사용 (갱신: {cached_at})")
                    articles = cached_articles
                elif cached_articles:
                    print(f"[검색] 캐시가 오래됨 ({cached_at}), 스크랩 재실행 중...")
                    articles = fetch_all_feeds(config)
                    print(f"[검색] {len(articles)}건 수집 완료")
                else:
                    print("[검색] 캐시 없음, 실시간 스크랩 실행 중...")
                    articles = fetch_all_feeds(config)
                    print(f"[검색] {len(articles)}건 수집 완료")

            print(f"[검색] 쿼리: {query}")
            results, gemini_used = search_articles(query, articles)
            response = format_search_response(query, results, gemini_used)
            await bot.send_message(chat_id=chat_id, text=response)
            print(f"[검색] 응답 전송 ({len(results)}건)")
        except Exception as e:
            print(f"[검색] 업데이트 처리 실패: update_id={update_id}, error={e}")
        finally:
            processed_update_ids.add(update_id)
            state["processed_update_ids"] = list(processed_update_ids)
            save_search_state(state)

    # 루프 종료 후 최종 상태 저장 (스킵된 업데이트의 offset 반영)
    state["processed_update_ids"] = list(processed_update_ids)
    save_search_state(state)

    # 처리 완료된 업데이트를 Telegram 서버에서 확인(confirm)
    # offset+1로 getUpdates를 호출하면 이전 업데이트가 서버에서 제거됨
    # → search_state.json 저장/push 실패 시에도 같은 메시지를 다시 받지 않음
    max_id = state.get("last_update_id", 0)
    if max_id:
        await bot.get_updates(offset=max_id + 1, timeout=0)
        print(f"[검색] Telegram offset 확인 완료: {max_id + 1}")

    print("[검색] 완료")


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
