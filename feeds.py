"""건국대학교 RSS 피드 수집 및 파싱 모듈"""

import asyncio
import json
import logging
import re
import ssl
import tempfile
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp
import feedparser
import urllib.request
from bs4 import BeautifulSoup

logger = logging.getLogger("monitor.feeds")

# 건국대 서버 SSL 인증서 문제 우회용 컨텍스트
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE

# 동시 크롤링 제한 (서버 부하 방지)
_SCRAPE_CONCURRENCY = 5


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

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, context=_ssl_ctx) as resp:
            xml_data = resp.read()
        feed = feedparser.parse(xml_data)
    except Exception as e:
        logger.error("%s (%d) 피드 수집 실패: %s", board_name, board_id, e)
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
    """state.json 로드. 손상되었거나 없으면 초기 상태 반환"""
    path = Path(state_path)
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("state.json 파싱 실패, 초기화합니다: %s", e)
    return {"seen_ids": {}, "last_run": None}


def save_state(state: dict, state_path: str):
    """state.json 원자적 저장 + 90일 지난 ID 자동 정리"""
    cutoff = (datetime.now() - timedelta(days=90)).isoformat()
    state["seen_ids"] = {
        k: v for k, v in state["seen_ids"].items()
        if v > cutoff
    }
    state["last_run"] = datetime.now().isoformat()

    # 원자적 쓰기: 임시 파일에 먼저 쓴 후 이동 (중간 실패 시 기존 파일 보존)
    path = Path(state_path)
    try:
        fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with open(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        Path(tmp_path).replace(path)
    except Exception:
        # 원자적 쓰기 실패 시 직접 쓰기로 폴백
        logger.warning("원자적 쓰기 실패, 직접 저장합니다.")
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)


def filter_new_articles(articles: list[Article], state: dict) -> list[Article]:
    """이미 확인한 공지를 제외하고 새 공지만 반환"""
    seen = state.get("seen_ids", {})
    return [a for a in articles if a.id not in seen]


def _extract_body_with_soup(html: str) -> str:
    """BeautifulSoup으로 본문 텍스트를 안정적으로 추출 (최대 500자)"""
    soup = BeautifulSoup(html, "html.parser")
    content_div = soup.find(class_="hwp_editor_board_content")
    if not content_div:
        return ""
    text = content_div.get_text(separator=" ", strip=True)
    return text[:500]


async def _fetch_article_body_async(session: aiohttp.ClientSession, url: str) -> str:
    """aiohttp로 단일 공지 본문을 비동기 크롤링"""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            html = await resp.text()
        return _extract_body_with_soup(html)
    except Exception as e:
        logger.debug("본문 크롤링 실패 %s: %s", url, e)
        return ""


async def enrich_articles_with_body(articles: list[Article]):
    """새 공지들의 본문을 비동기 병렬로 크롤링하여 description에 추가"""
    sem = asyncio.Semaphore(_SCRAPE_CONCURRENCY)
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)

    async with aiohttp.ClientSession(
        connector=connector,
        headers={"User-Agent": "Mozilla/5.0"},
    ) as session:

        async def _fetch(article: Article):
            async with sem:
                if article.link:
                    body = await _fetch_article_body_async(session, article.link)
                    if body:
                        article.description = body

        await asyncio.gather(*[_fetch(a) for a in articles])


def mark_as_seen(articles: list[Article], state: dict):
    """공지 ID를 state에 기록"""
    now = datetime.now().isoformat()
    for a in articles:
        state["seen_ids"][a.id] = now
