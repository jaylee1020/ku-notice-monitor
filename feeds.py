"""건국대학교 RSS 피드 수집 및 파싱 모듈"""

import json
import re
import ssl
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path

import feedparser
import urllib.request


def _create_ssl_context() -> ssl.SSLContext:
    """건국대 서버 SSL 인증서 문제 우회용 컨텍스트"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


@dataclass
class Article:
    id: str
    title: str
    link: str
    pub_date: str
    author: str
    description: str
    board_name: str
    board_id: int
    view_count: int
    is_pinned: bool
    attachment_count: int


def parse_pub_date(date_str: str) -> datetime:
    """건국대 RSS의 비표준 날짜 포맷 파싱: 'YYYY-MM-DD HH:MM:SS.mmm'"""
    base = date_str.split(".")[0]
    return datetime.strptime(base, "%Y-%m-%d %H:%M:%S")


def extract_article_id(link: str) -> str:
    """링크 경로에서 게시물 ID 추출: /bbs/konkuk/234/1166860/artclView.do"""
    match = re.search(r"/bbs/konkuk/\d+/(\d+)/artclView", link)
    return match.group(1) if match else link


def normalize_link(link: str, base_url: str) -> str:
    """상대 링크를 절대 URL로 변환하고 불필요한 쿼리 파라미터 제거"""
    link = link.split("?")[0]
    if link.startswith("/"):
        return base_url + link
    return link


def is_empty_feed_item(entry) -> bool:
    """빈 피드의 센티널 값 감지"""
    title = entry.get("title", "")
    return "No exist data" in title or "no exist data" in title.lower()


def fetch_feed(board_name: str, board_id: int, config: dict) -> list[Article]:
    """단일 RSS 피드를 수집하고 Article 리스트로 반환"""
    base_url = config["settings"]["base_url"]
    url = config["settings"]["rss_url_template"].format(board_id=board_id)

    ctx = _create_ssl_context()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            xml_data = resp.read()
        feed = feedparser.parse(xml_data)
    except Exception as e:
        print(f"[피드 오류] {board_name} ({board_id}): {e}")
        return []

    articles = []

    for entry in feed.entries:
        if is_empty_feed_item(entry):
            continue

        link = normalize_link(entry.get("link", ""), base_url)
        article_id = extract_article_id(entry.get("link", ""))

        articles.append(Article(
            id=article_id,
            title=entry.get("title", "").strip(),
            link=link,
            pub_date=entry.get("pubdate", entry.get("published", "")),
            author=entry.get("author", ""),
            description=entry.get("description", "").strip(),
            board_name=board_name,
            board_id=board_id,
            view_count=int(entry.get("viewco", 0) or 0),
            is_pinned=entry.get("topchk", "") == "FIXTOP",
            attachment_count=int(entry.get("atchco", 0) or 0),
        ))

    return articles


def fetch_all_feeds(config: dict) -> list[Article]:
    """모든 활성화된 피드에서 게시물 수집"""
    all_articles = []
    for board_name, feed_config in config["feeds"].items():
        if not feed_config.get("enabled", True):
            continue
        articles = fetch_feed(board_name, feed_config["id"], config)
        all_articles.extend(articles)
    return all_articles


def load_state(state_path: str) -> dict:
    """state.json 로드. 없으면 초기 상태 반환"""
    path = Path(state_path)
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"seen_ids": {}, "last_run": None}


def save_state(state: dict, state_path: str):
    """state.json 저장 + 90일 지난 ID 자동 정리"""
    cutoff = (datetime.now() - timedelta(days=90)).isoformat()
    state["seen_ids"] = {
        k: v for k, v in state["seen_ids"].items()
        if v > cutoff
    }
    state["last_run"] = datetime.now().isoformat()

    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def filter_new_articles(articles: list[Article], state: dict) -> list[Article]:
    """이미 확인한 공지를 제외하고 새 공지만 반환"""
    seen = state.get("seen_ids", {})
    return [a for a in articles if a.id not in seen]


def fetch_article_body(url: str) -> str:
    """게시물 웹페이지에서 본문 텍스트를 크롤링하여 반환 (최대 500자)"""
    ctx = _create_ssl_context()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
            html = resp.read().decode("utf-8")
        # hwp_editor_board_content 영역에서 본문 추출
        idx = html.find("hwp_editor_board_content")
        if idx < 0:
            return ""
        # 해당 div의 끝까지 추출 (최대 5000자 범위)
        snippet = html[idx:idx + 5000]
        # HTML 태그 제거
        text = re.sub(r"<[^>]+>", " ", snippet)
        text = re.sub(r"\s+", " ", text).strip()
        # 클래스명 부분 제거
        if text.startswith("hwp_editor_board_content"):
            text = text[len("hwp_editor_board_content"):].strip()
            # data 속성 부분 제거
            if text.startswith('"'):
                idx2 = text.find(">")
                if idx2 >= 0:
                    text = text[idx2 + 1:].strip()
        return text[:500]
    except Exception as e:
        print(f"[본문 크롤링 오류] {url}: {e}")
        return ""


def enrich_articles_with_body(articles: list[Article]):
    """새 공지들의 본문을 크롤링하여 description에 추가"""
    for a in articles:
        if a.link:
            body = fetch_article_body(a.link)
            if body:
                a.description = body


def mark_as_seen(articles: list[Article], state: dict):
    """공지 ID를 state에 기록"""
    now = datetime.now().isoformat()
    for a in articles:
        state["seen_ids"][a.id] = now


ARTICLES_CACHE_FILE = Path(__file__).parent / "articles_cache.json"


def save_articles_cache(articles: list[Article]):
    """공지 목록을 캐시 파일에 저장"""
    data = {
        "cached_at": datetime.now().isoformat(),
        "articles": [asdict(a) for a in articles],
    }
    with open(ARTICLES_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_articles_cache() -> list[Article] | None:
    """캐시된 공지 목록 로드. 없으면 None 반환"""
    if not ARTICLES_CACHE_FILE.exists():
        return None
    try:
        with open(ARTICLES_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [Article(**a) for a in data["articles"]]
    except Exception as e:
        print(f"[캐시 로드 오류] articles_cache.json 로드 실패: {e}")
        return None
