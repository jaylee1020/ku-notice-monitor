"""건국대학교 RSS 피드 수집 및 파싱 모듈"""

import asyncio
import logging
import re
import ssl
from datetime import datetime

import aiohttp
import certifi
import feedparser

from constants import (
    ARTICLE_BODY_TIMEOUT,
    BOARD_CONTENT_CLASS,
    EMPTY_FEED_SENTINEL,
    FEED_FETCH_TIMEOUT,
    MAX_ARTICLE_BODY_LENGTH,
)
from models import Article

logger = logging.getLogger(__name__)

_DEFAULT_HEADERS = {"User-Agent": "Mozilla/5.0"}


def _make_ssl_context(ssl_verify: bool) -> ssl.SSLContext:
    if ssl_verify:
        return ssl.create_default_context(cafile=certifi.where())
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def parse_pub_date(date_str: str) -> datetime:
    """건국대 RSS의 비표준 날짜 포맷 파싱: 'YYYY-MM-DD HH:MM:SS.mmm'"""
    base = date_str.split(".")[0]
    return datetime.strptime(base, "%Y-%m-%d %H:%M:%S")


def extract_article_id(link: str) -> str:
    """링크 경로에서 게시물 ID 추출: /bbs/konkuk/234/1166860/artclView.do"""
    match = re.search(r"/bbs/konkuk/\d+/(\d+)/artclView", link)
    if not match:
        logger.debug("게시물 ID 추출 실패, 링크를 ID로 사용: %s", link)
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
    return EMPTY_FEED_SENTINEL in title.lower()


def _to_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_entry(entry, board_name: str, board_id: int, base_url: str) -> Article | None:
    """RSS 엔트리를 Article 객체로 변환. 빈 피드 항목이면 None 반환."""
    if is_empty_feed_item(entry):
        return None

    link = normalize_link(entry.get("link", ""), base_url)
    article_id = extract_article_id(entry.get("link", ""))

    return Article(
        id=article_id,
        title=entry.get("title", "").strip(),
        link=link,
        pub_date=entry.get("pubdate", entry.get("published", "")),
        author=entry.get("author", ""),
        description=entry.get("description", "").strip(),
        board_name=board_name,
        board_id=board_id,
        view_count=_to_int(entry.get("viewco", 0) or 0),
        is_pinned=entry.get("topchk", "") == "FIXTOP",
        attachment_count=_to_int(entry.get("atchco", 0) or 0),
    )


async def _fetch_feed_async(
    session: aiohttp.ClientSession,
    board_name: str,
    board_id: int,
    feed_config: dict,
    config: dict,
    ssl_context: ssl.SSLContext,
) -> list[Article]:
    """단일 RSS 피드를 비동기로 수집하고 Article 리스트로 반환"""
    base_url = config["settings"]["base_url"]
    url = feed_config.get("rss_url") or config["settings"]["rss_url_template"].format(board_id=board_id)

    try:
        async with session.get(url, ssl=ssl_context, timeout=aiohttp.ClientTimeout(total=FEED_FETCH_TIMEOUT)) as resp:
            resp.raise_for_status()
            xml_data = await resp.read()

        if b"<rss" not in xml_data.lower():
            logger.warning("RSS 형식이 아닌 응답 - %s (board_id=%d, url=%s)", board_name, board_id, url)
            return []

        feed = feedparser.parse(xml_data)
    except Exception as e:
        logger.error("피드 수집 실패 - %s (board_id=%d): %s", board_name, board_id, e)
        return []

    articles = [
        article
        for entry in feed.entries
        if (article := _parse_entry(entry, board_name, board_id, base_url)) is not None
    ]
    logger.debug("%s: %d건 수집", board_name, len(articles))
    return articles


async def fetch_all_feeds(config: dict) -> list[Article]:
    """모든 활성화된 피드에서 게시물을 비동기로 병렬 수집"""
    ssl_verify = config.get("settings", {}).get("ssl_verify", False)
    if not ssl_verify:
        logger.warning("SSL 인증서 검증 비활성화 상태. ssl_verify: true로 변경하면 보안이 강화됩니다.")
    ssl_context = _make_ssl_context(ssl_verify)

    async with aiohttp.ClientSession(headers=_DEFAULT_HEADERS) as session:
        tasks = [
            _fetch_feed_async(session, board_name, feed_config["id"], feed_config, config, ssl_context)
            for board_name, feed_config in config["feeds"].items()
            if feed_config.get("enabled", True)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    all_articles: list[Article] = []
    for result in results:
        if isinstance(result, Exception):
            logger.error("피드 수집 중 예외 발생: %s", result)
        else:
            all_articles.extend(result)

    return all_articles


async def _fetch_article_body_async(
    session: aiohttp.ClientSession,
    url: str,
    ssl_context: ssl.SSLContext,
) -> str:
    """게시물 웹페이지에서 본문 텍스트를 비동기로 크롤링 (최대 500자)"""
    from bs4 import BeautifulSoup

    try:
        async with session.get(url, ssl=ssl_context, timeout=aiohttp.ClientTimeout(total=ARTICLE_BODY_TIMEOUT)) as resp:
            resp.raise_for_status()
            html = await resp.text(encoding="utf-8", errors="replace")

        soup = BeautifulSoup(html, "lxml")
        content_div = soup.find("div", class_=BOARD_CONTENT_CLASS)
        if not content_div:
            return ""

        text = content_div.get_text(separator=" ", strip=True)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:MAX_ARTICLE_BODY_LENGTH]
    except Exception as e:
        logger.warning("본문 크롤링 실패 - %s: %s", url, e)
        return ""


async def enrich_articles_with_body(articles: list[Article], config: dict) -> None:
    """새 공지들의 본문을 비동기 병렬로 크롤링하여 description에 추가"""
    if not articles:
        return

    ssl_verify = config.get("settings", {}).get("ssl_verify", False)
    ssl_context = _make_ssl_context(ssl_verify)

    async with aiohttp.ClientSession(headers=_DEFAULT_HEADERS) as session:
        tasks = [
            _fetch_article_body_async(session, a.link, ssl_context)
            for a in articles
            if a.link
        ]
        bodies = await asyncio.gather(*tasks, return_exceptions=True)

    link_articles = [a for a in articles if a.link]
    for article, body in zip(link_articles, bodies):
        if isinstance(body, Exception):
            logger.warning("본문 크롤링 예외 - %s: %s", article.link, body)
        elif body:
            article.description = body


async def check_ssl_health(config: dict) -> bool:
    """건국대 SSL 인증서 상태를 점검하고 결과를 로그로 기록"""
    base_url = config.get("settings", {}).get("base_url", "")
    if not base_url:
        return False

    ssl_context = _make_ssl_context(ssl_verify=True)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                base_url,
                ssl=ssl_context,
                timeout=aiohttp.ClientTimeout(total=FEED_FETCH_TIMEOUT),
            ) as resp:
                logger.info(
                    "SSL 인증서 점검 성공 (status=%d). ssl_verify: true로 전환을 권장합니다.",
                    resp.status,
                )
                return True
    except Exception as e:
        logger.info("SSL 인증서 점검 실패 (현재 설정 유지): %s", e)
        return False
