"""건국대학교 공지 모니터링 에이전트 - 메인 실행 파일"""

import asyncio
import sys
from pathlib import Path

import yaml

from feeds import fetch_all_feeds, filter_new_articles, load_state, save_state, mark_as_seen, enrich_articles_with_body
from matcher import match_articles
from notifier import notify_relevant, notify_no_new, notify_no_relevant


def load_config() -> dict:
    config_path = Path(__file__).parent / "config.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


async def run():
    print("=== 건국대 공지 모니터링 시작 ===")

    # 1. 설정 로드
    config = load_config()
    state_path = Path(__file__).parent / config["settings"]["state_file"]

    # 2. 상태 로드
    state = load_state(str(state_path))
    print(f"[상태] 기존 확인 공지: {len(state.get('seen_ids', {}))}건")

    # 3. RSS 피드 수집
    print("[피드] RSS 피드 수집 중...")
    all_articles = fetch_all_feeds(config)
    print(f"[피드] 총 {len(all_articles)}건 수집")

    # 4. 새 공지 필터링
    new_articles = filter_new_articles(all_articles, state)
    print(f"[필터] 새 공지: {len(new_articles)}건")

    if not new_articles:
        print("[결과] 새로운 공지가 없습니다.")
        await notify_no_new()
        save_state(state, str(state_path))
        return

    # 5. 새 공지 본문 크롤링
    print("[크롤링] 새 공지 본문 수집 중...")
    enrich_articles_with_body(new_articles)

    # 6. Gemini 관련도 분석
    print("[분석] Gemini로 관련도 분석 중...")
    matched = match_articles(new_articles, config)
    print(f"[분석] 관련 공지: {len(matched)}건")

    # 6. 텔레그램 알림
    if matched:
        print("[알림] 텔레그램으로 관련 공지 전송 중...")
        await notify_relevant(matched, len(new_articles))
        print("[알림] 전송 완료")
    else:
        print("[알림] 관련 공지 없음 알림 전송 중...")
        await notify_no_relevant(len(new_articles))
        print("[알림] 전송 완료")

    # 7. 상태 업데이트
    mark_as_seen(all_articles, state)
    save_state(state, str(state_path))
    print(f"[상태] 저장 완료 (총 {len(state['seen_ids'])}건 기록)")

    print("=== 완료 ===")


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
