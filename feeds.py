"""건국대학교 RSS 피드 수집 및 게시물 본문 파싱 모듈"""

import asyncio
import logging
import re
import ssl
from datetime import datetime

import aiohttp
import feedparser
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from constants import (
    ARTICLE_BODY_TIMEOUT,
    BOARD_CONTENT_CLASS,
    EMPTY_FEED_SENTINEL,
    FEED_FETCH_TIMEOUT,
    MAX_ARTICLE_BODY_LENGTH,
    MAX_CONCURRENT_BODY_FETCHES,
    MAX_IMAGES_PER_ARTICLE,
    MIN_IMAGE_URL_LENGTH,
)
from models import Article, Attachment
from net import DEFAULT_HEADERS, make_ssl_context, ssl_context_from_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RSS 엔트리 파싱
# ---------------------------------------------------------------------------


def parse_pub_date(date_str: str) -> datetime:
    """건국대 RSS의 비표준 날짜 포맷 파싱: 'YYYY-MM-DD HH:MM:SS.mmm'"""
    base = date_str.split(".")[0]
    return datetime.strptime(base, "%Y-%m-%d %H:%M:%S")


def _safe_pub_date_string(entry) -> str:
    """RSS 엔트리에서 pub_date 문자열을 추출. 파싱 실패해도 원문은 보존."""
    raw = entry.get("pubdate") or entry.get("published") or ""
    if not raw:
        return ""
    try:
        parse_pub_date(raw)
    except (ValueError, TypeError):
        logger.debug("pub_date 파싱 실패(원문 유지): %r", raw)
    return raw


def extract_article_id(link: str) -> str:
    """링크 경로에서 게시물 ID 추출: /bbs/konkuk/234/1166860/artclView.do → 1166860"""
    match = re.search(r"/bbs/konkuk/\d+/(\d+)/artclView", link)
    if not match:
        logger.debug("게시물 ID 추출 실패, 링크를 ID로 사용: %s", link)
        return link
    return match.group(1)


def normalize_link(link: str, base_url: str) -> str:
    """상대 링크를 절대 URL로 변환하고 쿼리 파라미터를 제거한다."""
    link = link.split("?")[0]
    return base_url + link if link.startswith("/") else link


def is_empty_feed_item(entry) -> bool:
    """빈 피드의 센티널 값을 감지한다."""
    return EMPTY_FEED_SENTINEL in entry.get("title", "").lower()


def _to_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


_HTML_ENTITIES = {"&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"'}


def _strip_html(html: str) -> str:
    """HTML 태그 제거 후 공백 정규화 (BeautifulSoup 없이 간단 처리)"""
    if not html:
        return ""
    text = re.sub(r"<[^>]+>", " ", html)
    for entity, char in _HTML_ENTITIES.items():
        text = text.replace(entity, char)
    return re.sub(r"\s+", " ", text).strip()


def _extract_rss_content(entry) -> str:
    """RSS 엔트리에서 가장 풍부한 본문 텍스트를 선택한다.

    우선순위 후보(content:encoded → summary_detail → description → summary) 중
    HTML 제거 후 가장 긴 값을 반환한다.
    """
    candidates: list[str] = []

    content_list = entry.get("content")
    if isinstance(content_list, list):
        candidates.extend(
            item["value"] for item in content_list
            if isinstance(item, dict) and item.get("value")
        )

    for key in ("summary_detail", "description", "summary"):
        val = entry.get(key)
        if isinstance(val, dict):
            val = val.get("value", "")
        if isinstance(val, str) and val:
            candidates.append(val)

    stripped = [s for s in (_strip_html(c) for c in candidates) if s]
    return max(stripped, key=len) if stripped else ""


def _parse_entry(entry, board_name: str, board_id: int, base_url: str) -> Article | None:
    """RSS 엔트리를 Article 객체로 변환. 빈 피드 항목이면 None."""
    if is_empty_feed_item(entry):
        return None

    return Article(
        id=extract_article_id(entry.get("link", "")),
        title=entry.get("title", "").strip(),
        link=normalize_link(entry.get("link", ""), base_url),
        pub_date=_safe_pub_date_string(entry),
        author=entry.get("author", ""),
        description=_extract_rss_content(entry),
        board_name=board_name,
        board_id=board_id,
        view_count=_to_int(entry.get("viewco", 0) or 0),
        is_pinned=entry.get("topchk", "") == "FIXTOP",
        attachment_count=_to_int(entry.get("atchco", 0) or 0),
    )


# ---------------------------------------------------------------------------
# RSS 피드 수집
# ---------------------------------------------------------------------------


@retry(
    retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
async def _fetch_feed_async(
    session: aiohttp.ClientSession,
    board_name: str,
    board_id: int,
    feed_config: dict,
    config: dict,
    ssl_context: ssl.SSLContext,
) -> list[Article]:
    """단일 RSS 피드를 수집하고 Article 리스트로 반환 (최대 3회 재시도)"""
    base_url = config["settings"]["base_url"]
    url = feed_config.get("rss_url") or config["settings"]["rss_url_template"].format(board_id=board_id)

    async with session.get(
        url, ssl=ssl_context, timeout=aiohttp.ClientTimeout(total=FEED_FETCH_TIMEOUT)
    ) as resp:
        resp.raise_for_status()
        xml_data = await resp.read()

    if b"<rss" not in xml_data.lower():
        logger.warning("RSS 형식이 아닌 응답 - %s (board_id=%d, url=%s)", board_name, board_id, url)
        return []

    feed = feedparser.parse(xml_data)
    articles = [
        article
        for entry in feed.entries
        if (article := _parse_entry(entry, board_name, board_id, base_url)) is not None
    ]
    logger.debug("%s: %d건 수집", board_name, len(articles))
    return articles


async def fetch_all_feeds(config: dict) -> list[Article]:
    """모든 활성화된 피드에서 게시물을 비동기 병렬 수집한다."""
    ssl_verify = config.get("settings", {}).get("ssl_verify", True)
    if not ssl_verify:
        logger.warning("SSL 인증서 검증 비활성화 상태. ssl_verify: true로 변경하면 보안이 강화됩니다.")
    ssl_context = make_ssl_context(ssl_verify)

    async with aiohttp.ClientSession(headers=DEFAULT_HEADERS) as session:
        results = await asyncio.gather(
            *(
                _fetch_feed_async(session, name, fc["id"], fc, config, ssl_context)
                for name, fc in config["feeds"].items()
                if fc.get("enabled", True)
            ),
            return_exceptions=True,
        )

    all_articles: list[Article] = []
    for result in results:
        if isinstance(result, Exception):
            logger.error("피드 수집 중 예외 발생: %s", result, exc_info=True)
        else:
            all_articles.extend(result)
    return all_articles


# ---------------------------------------------------------------------------
# 게시물 본문/이미지/첨부파일 크롤링
# ---------------------------------------------------------------------------

_TRACKING_IMAGE_PATTERNS = re.compile(
    r"(?:^|[/_-])(spacer|blank|pixel|tracker|1x1|clear|bullet|icon|btn|button|arrow)\b",
    re.IGNORECASE,
)


def _candidate_image_urls(img_tag) -> list[str]:
    """img 태그에서 모든 src 후보를 수집한다 (lazy-load, srcset 포함)."""
    candidates: list[str] = []
    for attr in ("src", "data-src", "data-original", "data-lazy-src", "data-echo"):
        val = img_tag.get(attr, "")
        if val:
            candidates.append(val)

    srcset = img_tag.get("srcset", "")
    if srcset:
        for entry in srcset.split(","):
            url = entry.strip().split(" ")[0]
            if url:
                candidates.append(url)
    return candidates


def _is_valid_content_image(url: str) -> bool:
    """트래킹 픽셀/UI 아이콘/SVG 후보를 배제한다."""
    if len(url) < MIN_IMAGE_URL_LENGTH:
        return False
    lower = url.lower()
    if lower.endswith(".svg"):  # Gemini 미지원
        return False
    return not _TRACKING_IMAGE_PATTERNS.search(lower)


def _extract_image_urls(content_div, base_url: str, soup=None) -> list[str]:
    """콘텐츠 div에서 이미지 URL을 추출한다 (lazy-load/srcset/og:image, 트래킹 필터 적용)."""
    image_urls: list[str] = []
    seen: set[str] = set()

    def add(url: str) -> bool:
        """이미지를 등록하고, 최대치에 도달하면 True를 반환한다."""
        if not url:
            return False
        resolved = normalize_link(url, base_url) if url.startswith("/") else url
        if not resolved.startswith("http") or not _is_valid_content_image(resolved):
            return False
        if resolved not in seen:
            seen.add(resolved)
            image_urls.append(resolved)
        return len(image_urls) >= MAX_IMAGES_PER_ARTICLE

    for img in content_div.find_all("img"):
        for candidate in _candidate_image_urls(img):
            if add(candidate):
                return image_urls

    # 본문에 이미지가 부족하면 og:image / twitter:image 메타 태그를 폴백으로 사용
    if soup is not None:
        for meta in soup.find_all("meta"):
            prop = (meta.get("property") or meta.get("name") or "").lower()
            if prop in ("og:image", "twitter:image") and add(meta.get("content", "")):
                break

    return image_urls


def _extract_attachments(soup, base_url: str) -> list[Attachment]:
    """페이지의 div.attachments에서 첨부파일 목록을 추출한다."""
    attach_div = soup.find("div", class_="attachments")
    if not attach_div:
        return []

    attachments: list[Attachment] = []
    for a_tag in attach_div.find_all("a", href=True):
        href = a_tag["href"]
        if "/download.do" not in href:
            continue
        filename = a_tag.get_text(strip=True)
        if not filename:
            continue
        url = href if href.startswith("http") else base_url + href
        attachments.append(Attachment(filename=filename, url=url))
    return attachments


async def _fetch_article_body_async(
    session: aiohttp.ClientSession,
    url: str,
    ssl_context: ssl.SSLContext,
    base_url: str,
    semaphore: asyncio.Semaphore,
) -> tuple[str, list[str], list[Attachment]]:
    """게시물 페이지에서 본문 텍스트, 이미지 URL, 첨부파일 정보를 크롤링한다."""
    from bs4 import BeautifulSoup

    try:
        async with semaphore:
            async with session.get(
                url, ssl=ssl_context, timeout=aiohttp.ClientTimeout(total=ARTICLE_BODY_TIMEOUT)
            ) as resp:
                resp.raise_for_status()
                html = await resp.text(encoding="utf-8", errors="replace")

        soup = BeautifulSoup(html, "lxml")
        attachments = _extract_attachments(soup, base_url)

        content_div = soup.find("div", class_=BOARD_CONTENT_CLASS)
        if not content_div:
            return "", [], attachments

        text = re.sub(r"\s+", " ", content_div.get_text(separator=" ", strip=True)).strip()
        image_urls = _extract_image_urls(content_div, base_url, soup=soup)
        return text[:MAX_ARTICLE_BODY_LENGTH], image_urls, attachments
    except Exception as e:
        logger.warning("본문 크롤링 실패 - %s: %s", url, e)
        return "", [], []


def _merge_description(rss_body: str, crawled_body: str) -> str:
    """RSS 요약과 크롤된 본문을 병합한다 (RSS 우선, 중복 제거)."""
    if crawled_body and rss_body and crawled_body != rss_body:
        combined = crawled_body if rss_body in crawled_body else f"{rss_body}\n{crawled_body}"
        return combined[:MAX_ARTICLE_BODY_LENGTH]
    if crawled_body and not rss_body:
        return crawled_body[:MAX_ARTICLE_BODY_LENGTH]
    return rss_body  # 크롤 실패 또는 동일 → RSS 유지


async def enrich_articles_with_body(articles: list[Article], config: dict) -> None:
    """새 공지들의 본문/이미지/첨부파일을 병렬 크롤링하여 Article에 채워 넣는다."""
    if not articles:
        return

    ssl_context = ssl_context_from_config(config)
    base_url = config["settings"]["base_url"]
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_BODY_FETCHES)

    link_articles = [a for a in articles if a.link]
    async with aiohttp.ClientSession(headers=DEFAULT_HEADERS) as session:
        results = await asyncio.gather(
            *(
                _fetch_article_body_async(session, a.link, ssl_context, base_url, semaphore)
                for a in link_articles
            ),
            return_exceptions=True,
        )

    for article, result in zip(link_articles, results):
        if isinstance(result, Exception):
            logger.warning("본문 크롤링 예외 - %s: %s", article.link, result)
            continue
        body, image_urls, attachments = result

        article.description = _merge_description(article.description or "", body)
        if image_urls:
            article.images = image_urls
        if attachments:
            article.attachments = attachments
            logger.debug(
                "첨부파일 %d건 발견 - %s: %s",
                len(attachments),
                article.title,
                ", ".join(att.filename for att in attachments),
            )


async def check_ssl_health(config: dict) -> bool:
    """건국대 SSL 인증서 상태를 점검하고 결과를 로그로 기록한다."""
    base_url = config.get("settings", {}).get("base_url", "")
    if not base_url:
        return False

    ssl_context = make_ssl_context(ssl_verify=True)
    try:
        async with aiohttp.ClientSession(headers=DEFAULT_HEADERS) as session:
            async with session.get(
                base_url, ssl=ssl_context, timeout=aiohttp.ClientTimeout(total=FEED_FETCH_TIMEOUT)
            ) as resp:
                logger.info(
                    "SSL 인증서 점검 성공 (status=%d). ssl_verify: true로 전환을 권장합니다.",
                    resp.status,
                )
                return True
    except Exception as e:
        logger.info("SSL 인증서 점검 실패 (현재 설정 유지): %s", e)
        return False
