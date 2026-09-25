"""건국대학교 RSS 피드 수집 및 게시물 본문 파싱 모듈"""

import asyncio
import logging
import re
import ssl
import time
from dataclasses import dataclass
from datetime import datetime
from html import unescape
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp
import feedparser
from bs4 import BeautifulSoup
from tenacity import retry, retry_if_exception, retry_if_exception_type, stop_after_attempt, wait_exponential

from .constants import (
    ARTICLE_BODY_TIMEOUT,
    BOARD_CONTENT_CLASS,
    EMPTY_FEED_SENTINEL,
    FEED_FETCH_TIMEOUT,
    MAX_ARTICLE_BODY_LENGTH,
    MAX_ARTICLE_HTML_SIZE,
    MAX_CONCURRENT_BODY_FETCHES,
    MAX_FEED_SIZE,
    MAX_IMAGES_PER_ARTICLE,
    MIN_IMAGE_URL_LENGTH,
)
from .models import Article, Attachment
from .net import (
    DEFAULT_HEADERS,
    DownloadError,
    allowed_hosts_from_config,
    describe_network_error,
    download_bytes,
    is_allowed_hostname,
    make_ssl_context,
    ssl_context_from_config,
)
from .util import normalize_for_match

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FeedStatus:
    name: str
    board_id: int
    success: bool
    article_count: int
    elapsed_seconds: float
    error: str | None = None
    # 연결 불가·시간 초과·5xx처럼 학교 서버가 응답하지 못한 실패. 장애 판단에 쓴다.
    server_unavailable: bool = False


@dataclass(frozen=True)
class FeedBatch:
    articles: list[Article]
    statuses: list[FeedStatus]

    @property
    def successful_count(self) -> int:
        return sum(status.success for status in self.statuses)

    @property
    def failed_count(self) -> int:
        return len(self.statuses) - self.successful_count

    @property
    def is_source_outage(self) -> bool:
        """모든 피드가 서버 무응답으로 실패했다면 학교 서버 쪽 장애로 본다."""
        return bool(self.statuses) and all(
            not status.success and status.server_unavailable for status in self.statuses
        )

    def failure_summary(self) -> str:
        """실패 원인별 피드 수를 '응답 시간 초과 9개'처럼 요약한다."""
        counts: dict[str, int] = {}
        for status in self.statuses:
            if not status.success:
                reason = status.error or "알 수 없는 오류"
                counts[reason] = counts.get(reason, 0) + 1
        return ", ".join(f"{reason} {count}개" for reason, count in counts.items())


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
    """링크 경로에서 게시물 ID 추출: /bbs/{사이트}/234/1166860/artclView.do → 1166860

    www.konkuk.ac.kr(/bbs/konkuk/...)뿐 아니라 kuinc.konkuk.ac.kr(/bbs/job/...) 등
    사이트 경로가 다른 게시판도 동일하게 처리한다.
    """
    match = re.search(r"/bbs/[^/]+/\d+/(\d+)/artclView", link)
    if not match:
        logger.debug("게시물 ID 추출 실패, 링크를 ID로 사용: %s", link)
        return link
    return match.group(1)


def normalize_link(link: str, base_url: str) -> str:
    """상대 링크를 절대 URL로 변환하고 쿼리 파라미터를 제거한다."""
    resolved = urljoin(base_url.rstrip("/") + "/", link)
    parts = urlsplit(resolved)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def base_url_of(url: str) -> str:
    """절대 URL에서 'scheme://host' 부분을 추출한다. 절대 URL이 아니면 빈 문자열."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""


def is_empty_feed_item(entry) -> bool:
    """빈 피드의 센티널 값을 감지한다."""
    return EMPTY_FEED_SENTINEL in entry.get("title", "").lower()


def _to_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _strip_html(html: str) -> str:
    """HTML 태그 제거 후 엔티티 복원·공백 정규화 (BeautifulSoup 없이 간단 처리)"""
    if not html:
        return ""
    text = unescape(re.sub(r"<[^>]+>", " ", html))
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


def _is_server_unavailable(exc: BaseException) -> bool:
    """학교 서버가 응답하지 못한 실패(연결 불가·시간 초과·연결 끊김·5xx)인지 확인한다."""
    if isinstance(exc, DownloadError):
        return exc.status is not None and exc.status >= 500
    return isinstance(
        exc,
        (
            asyncio.TimeoutError,
            TimeoutError,
            aiohttp.ClientConnectionError,
            aiohttp.ClientPayloadError,
        ),
    )


def _is_retryable_feed_error(exc: BaseException) -> bool:
    """4xx 응답(429 제외)은 재시도해도 결과가 같으므로 즉시 실패시킨다."""
    status = getattr(exc, "status", None)
    if isinstance(status, int) and 400 <= status < 500:
        return status == 429
    return True


@retry(
    retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError))
    & retry_if_exception(_is_retryable_feed_error),
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
    url = feed_config.get("rss_url") or config["settings"]["rss_url_template"].format(board_id=board_id)
    # 상대 링크는 해당 피드가 서빙되는 호스트 기준으로 해석해야 한다.
    # (예: kuinc.konkuk.ac.kr 피드의 링크를 www.konkuk.ac.kr로 잘못 연결하지 않도록)
    base_url = base_url_of(url) or config["settings"]["base_url"]

    xml_data = await download_bytes(
        session,
        url,
        ssl_context=ssl_context,
        timeout=FEED_FETCH_TIMEOUT,
        allowed_hosts=allowed_hosts_from_config(config),
        max_size=MAX_FEED_SIZE,
        raise_on_error=True,
    )
    if xml_data is None:
        raise DownloadError("RSS 응답이 비어 있음")

    if b"<rss" not in xml_data.lower():
        # 점검 안내·오류 페이지를 '글 0건'으로 받아들이면 게시판을 못 읽고 있는데도
        # 성공으로 집계된다. 실패로 올려 수집 성공률과 실패 원인에 반영한다.
        logger.warning("RSS 형식이 아닌 응답 - %s (board_id=%d, url=%s)", board_name, board_id, url)
        raise DownloadError("RSS 형식이 아닌 응답")

    feed = feedparser.parse(xml_data)
    articles = [
        article
        for entry in feed.entries
        if (article := _parse_entry(entry, board_name, board_id, base_url)) is not None
    ]
    logger.debug("%s: %d건 수집", board_name, len(articles))
    return articles


async def fetch_all_feeds_detailed(config: dict) -> FeedBatch:
    """모든 피드를 병렬 수집하고 피드별 성공·실패를 함께 반환한다."""
    ssl_verify = config.get("settings", {}).get("ssl_verify", True)
    if not ssl_verify:
        logger.warning("SSL 인증서 검증 비활성화 상태. ssl_verify: true로 변경하면 보안이 강화됩니다.")
    ssl_context = make_ssl_context(ssl_verify)

    enabled = [
        (name, feed)
        for name, feed in config["feeds"].items()
        if feed.get("enabled", True)
    ]

    async def timed_fetch(session, name: str, feed: dict):
        started = time.monotonic()
        try:
            articles = await _fetch_feed_async(
                session,
                name,
                feed["id"],
                feed,
                config,
                ssl_context,
            )
            return articles, FeedStatus(
                name=name,
                board_id=feed["id"],
                success=True,
                article_count=len(articles),
                elapsed_seconds=round(time.monotonic() - started, 3),
            )
        except Exception as exc:
            reason = describe_network_error(exc)
            logger.error("%s 피드 수집 실패: %s", name, reason)
            return [], FeedStatus(
                name=name,
                board_id=feed["id"],
                success=False,
                article_count=0,
                elapsed_seconds=round(time.monotonic() - started, 3),
                error=reason,
                server_unavailable=_is_server_unavailable(exc),
            )

    async with aiohttp.ClientSession(headers=DEFAULT_HEADERS) as session:
        results = await asyncio.gather(
            *(timed_fetch(session, name, feed) for name, feed in enabled),
        )

    all_articles: list[Article] = []
    statuses: list[FeedStatus] = []
    for articles, status in results:
        all_articles.extend(articles)
        statuses.append(status)
    return FeedBatch(all_articles, statuses)


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
    if lower.endswith(".svg"):  # Responses API 이미지 입력에서 직접 지원하지 않음
        return False
    return not _TRACKING_IMAGE_PATTERNS.search(lower)


def _extract_image_urls(
    content_div,
    base_url: str,
    soup=None,
    allowed_hosts: set[str] | None = None,
) -> list[str]:
    """콘텐츠 div에서 이미지 URL을 추출한다 (lazy-load/srcset/og:image, 트래킹 필터 적용)."""
    image_urls: list[str] = []
    seen: set[str] = set()

    def add(url: str) -> bool:
        """이미지를 등록하고, 최대치에 도달하면 True를 반환한다."""
        if not url:
            return False
        resolved = urljoin(base_url.rstrip("/") + "/", url)
        parsed = urlsplit(resolved)
        if parsed.scheme != "https" or not parsed.hostname:
            return False
        if allowed_hosts and not is_allowed_hostname(parsed.hostname, allowed_hosts):
            logger.debug("허용되지 않은 이미지 호스트를 건너뜁니다: %s", parsed.hostname)
            return False
        if not _is_valid_content_image(resolved):
            return False
        if resolved not in seen:
            seen.add(resolved)
            image_urls.append(resolved)
        return len(image_urls) >= MAX_IMAGES_PER_ARTICLE

    for img in content_div.find_all("img"):
        for candidate in _candidate_image_urls(img):
            if add(candidate):
                return image_urls

    # 본문에 이미지가 전혀 없을 때만 og:image / twitter:image를 쓴다. 본문 이미지가
    # 있는데도 붙이면 대개 사이트 공통 로고라 첨부 분석 비용만 늘어난다.
    if soup is not None and not image_urls:
        for meta in soup.find_all("meta"):
            prop = (meta.get("property") or meta.get("name") or "").lower()
            if prop in ("og:image", "twitter:image") and add(meta.get("content", "")):
                break

    return image_urls


# "안내문.hwp (35KB)"처럼 파일명 뒤에 붙는 크기·설명을 떼어 낸다.
_ATTACHMENT_NAME_PATTERN = re.compile(r"^(.*?\.[A-Za-z0-9]{2,5})(?=\s*(?:[\[(].*)?$)")


def _attachment_filename(a_tag) -> str:
    text = re.sub(r"\s+", " ", a_tag.get_text(" ", strip=True)).strip()
    if not text:
        text = str(a_tag.get("title") or "").strip()
    match = _ATTACHMENT_NAME_PATTERN.match(text)
    return match.group(1).strip() if match else text


def _extract_attachments(
    soup,
    base_url: str,
    allowed_hosts: set[str] | None = None,
) -> list[Attachment]:
    """게시판 첨부 다운로드 링크(download.do)를 모은다.

    첨부 전용 영역(div.attachments)이 있으면 그 안에서, 없으면 페이지 전체에서
    찾는다. 게시판 스킨마다 첨부 목록의 마크업이 달라 전용 영역만 보면 첨부를
    하나도 찾지 못한다.
    """
    container = soup.find("div", class_="attachments") or soup

    attachments: list[Attachment] = []
    seen_urls: set[str] = set()
    for a_tag in container.find_all("a", href=True):
        href = a_tag["href"]
        if "/download.do" not in href:
            continue
        filename = _attachment_filename(a_tag)
        if not filename:
            continue
        url = urljoin(base_url.rstrip("/") + "/", href)
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname:
            continue
        if allowed_hosts and not is_allowed_hostname(parsed.hostname, allowed_hosts):
            logger.debug("허용되지 않은 첨부 호스트를 건너뜁니다: %s", parsed.hostname)
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        attachments.append(Attachment(filename=filename, url=url))
    return attachments


def _html_to_markdown(content_div) -> str:
    """공지 본문의 제목·목록·표 관계를 보존하는 제한적 Markdown 변환."""
    for table in content_div.find_all("table"):
        rows: list[list[str]] = []
        for tr in table.find_all("tr"):
            cells = [
                re.sub(r"\s+", " ", cell.get_text(" ", strip=True)).replace("|", "\\|")
                for cell in tr.find_all(["th", "td"])
            ]
            if cells:
                rows.append(cells)
        if not rows:
            table.decompose()
            continue
        width = max(len(row) for row in rows)
        normalized = [row + [""] * (width - len(row)) for row in rows]
        header = normalized[0]
        markdown_rows = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(["---"] * width) + " |",
            *("| " + " | ".join(row) + " |" for row in normalized[1:]),
        ]
        table.replace_with("\n" + "\n".join(markdown_rows) + "\n")

    for level in range(1, 7):
        for heading in content_div.find_all(f"h{level}"):
            text = heading.get_text(" ", strip=True)
            heading.replace_with(f"\n{'#' * level} {text}\n" if text else "\n")
    for item in content_div.find_all("li"):
        text = item.get_text(" ", strip=True)
        item.replace_with(f"\n- {text}" if text else "")
    for br in content_div.find_all("br"):
        br.replace_with("\n")

    text = content_div.get_text(separator="\n", strip=True)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


@dataclass(frozen=True)
class ArticlePage:
    """상세 페이지에서 읽은 본문·이미지·첨부와, 본문 영역을 찾은 방법(진단용)."""

    body: str
    images: list[str]
    attachments: list[Attachment]
    body_source: str


# 공지 본문을 감싸는 것으로 알려진 영역. 한글 웹 에디터로 쓴 글은 첫 번째 영역에 담긴다.
_BODY_SELECTORS = ("div.artclView", "div.view-con", "div.board-view-content")
# 표 칸(td)은 제외한다. 한글 문서를 옮긴 본문은 표로 되어 있어 첫 칸만 본문으로 잡힌다.
_BODY_BLOCK_TAGS = ["div", "article", "section", "main"]
_HIDDEN_MARKERS = ("share", "sns")
# RSS 요약으로 본문 위치를 찾을 때 쓰는 기준 길이(공백·구두점 제외 글자 수).
_SUMMARY_ANCHOR_MIN = 20
_SUMMARY_ANCHOR_MAX = 150
_STRUCTURE_HINT_KEYS = ("view", "artcl", "content", "cont", "board", "bbs", "attach", "file")


def _is_hidden_or_share_widget(element) -> bool:
    """숨김 영역이나 공유 위젯은 요약 문구를 되풀이할 수 있어 본문 후보에서 뺀다."""
    attrs = element.attrs
    if "hidden" in attrs or attrs.get("aria-hidden") == "true":
        return True
    if "display:none" in str(attrs.get("style", "")).replace(" ", "").lower():
        return True
    names = " ".join([*attrs.get("class", []), str(attrs.get("id", ""))]).lower()
    return any(marker in names for marker in _HIDDEN_MARKERS)


def _find_body_by_summary(soup, rss_summary: str):
    """RSS 요약 앞부분을 담은 가장 작은 블록을 본문 영역으로 본다.

    RSS 요약은 본문 앞부분을 그대로 옮긴 것이라, 게시판 스킨의 class 이름을 몰라도
    본문 위치를 찾을 수 있다. 너무 짧은 요약은 제목·메뉴와 헷갈릴 수 있어 쓰지 않는다.
    요약을 담은 가지만 따라 내려가므로 메뉴가 큰 페이지도 전체를 반복해 읽지 않는다.
    """
    anchor = normalize_for_match(rss_summary)[:_SUMMARY_ANCHOR_MAX]
    if len(anchor) < _SUMMARY_ANCHOR_MIN:
        return None
    best = None
    best_length = 0
    stack = [soup.body or soup]
    while stack:
        node = stack.pop()
        for child in node.find_all(True, recursive=False):
            if _is_hidden_or_share_widget(child):
                continue
            text = normalize_for_match(child.get_text(" "))
            if anchor not in text:
                continue
            if child.name in _BODY_BLOCK_TAGS and (best is None or len(text) < best_length):
                best, best_length = child, len(text)
            stack.append(child)
    return best


def _find_body(soup, rss_summary: str):
    content = soup.find("div", class_=BOARD_CONTENT_CLASS)
    if content is not None:
        return content, BOARD_CONTENT_CLASS
    content = _find_body_by_summary(soup, rss_summary)
    if content is not None:
        return content, "rss-summary"
    for selector in _BODY_SELECTORS:
        content = soup.select_one(selector)
        if content is not None:
            return content, selector
    return None, "none"


def page_structure_hint(soup) -> str:
    """본문을 찾지 못했을 때 로그로 마크업을 짐작할 수 있도록 제목과 주요 class를 요약한다."""
    title = soup.title.get_text(" ", strip=True)[:60] if soup.title else ""
    classes: list[str] = []
    for element in soup.find_all(["div", "dl", "ul", "section", "article"], class_=True):
        for name in element.get("class", []):
            lowered = name.lower()
            if name not in classes and any(key in lowered for key in _STRUCTURE_HINT_KEYS):
                classes.append(name)
    downloads = sum("/download.do" in a_tag["href"] for a_tag in soup.find_all("a", href=True))
    return f"title={title!r}, download 링크 {downloads}개, class={classes[:12]}"


def parse_article_page(
    html: str,
    *,
    base_url: str,
    allowed_hosts: set[str],
    rss_summary: str = "",
) -> ArticlePage | None:
    """상세 페이지 HTML에서 본문·이미지·첨부를 읽는다. 둘 다 없으면 ``None``."""
    soup = BeautifulSoup(html, "lxml")
    attachments = _extract_attachments(soup, base_url, allowed_hosts)
    content, source = _find_body(soup, rss_summary)
    if content is None:
        if not attachments:
            return None
        return ArticlePage("", [], attachments, source)
    image_urls = _extract_image_urls(
        content,
        base_url,
        soup=soup,
        allowed_hosts=allowed_hosts,
    )
    text = _html_to_markdown(content)
    return ArticlePage(text[:MAX_ARTICLE_BODY_LENGTH], image_urls, attachments, source)


async def _fetch_article_body_async(
    session: aiohttp.ClientSession,
    url: str,
    ssl_context: ssl.SSLContext,
    base_url: str,
    semaphore: asyncio.Semaphore,
    allowed_hosts: set[str],
    rss_summary: str = "",
) -> ArticlePage | None:
    """게시물 페이지에서 본문 텍스트, 이미지 URL, 첨부파일 정보를 크롤링한다.

    페이지를 받지 못했거나 본문·첨부를 모두 찾지 못하면 ``None``을 반환한다. 이 경우를
    빈 본문으로 취급하면 이전 상세 지문과 달라져 수정 공지로 오탐하게 된다.
    """
    try:
        async with semaphore:
            html_data = await download_bytes(
                session,
                url,
                ssl_context=ssl_context,
                timeout=ARTICLE_BODY_TIMEOUT,
                allowed_hosts=allowed_hosts,
                max_size=MAX_ARTICLE_HTML_SIZE,
            )
        if html_data is None:
            return None
        html = html_data.decode("utf-8", errors="replace")
        page = parse_article_page(
            html,
            base_url=base_url,
            allowed_hosts=allowed_hosts,
            rss_summary=rss_summary,
        )
        if page is None:
            # 오류 페이지처럼 본문도 첨부도 없는 응답은 실패로 본다. 게시판 스킨이 바뀌어도
            # 원인을 알 수 있게 페이지 구조를 함께 남긴다.
            logger.warning(
                "본문 영역을 찾지 못했습니다 - %s (%s)",
                url,
                page_structure_hint(BeautifulSoup(html, "lxml")),
            )
        return page
    except Exception as e:
        logger.warning("본문 크롤링 실패 - %s: %s", url, e)
        return None


def _merge_description(rss_body: str, crawled_body: str) -> str:
    """RSS 요약과 크롤된 본문을 병합하고 한쪽에 이미 들어 있는 내용은 되풀이하지 않는다.

    RSS 요약은 공백이 한 칸으로 합쳐지고 상세 본문은 줄바꿈·목록·표 기호가 있어
    문자열을 그대로 비교하면 같은 내용도 달라 보인다. 공백·구두점을 무시하고
    비교해야 같은 본문이 두 번 들어가 모델 입력을 낭비하지 않는다.
    """
    if not crawled_body:
        return rss_body  # 크롤 실패 → RSS 유지
    normalized_rss = normalize_for_match(rss_body)
    normalized_crawled = normalize_for_match(crawled_body)
    if normalized_rss in normalized_crawled:
        return crawled_body[:MAX_ARTICLE_BODY_LENGTH]
    if normalized_crawled in normalized_rss:
        return rss_body[:MAX_ARTICLE_BODY_LENGTH]
    return f"{rss_body}\n{crawled_body}"[:MAX_ARTICLE_BODY_LENGTH]


async def enrich_articles_with_body(articles: list[Article], config: dict) -> set[str]:
    """공지 본문·이미지·첨부를 병렬 크롤링해 채우고, 성공한 공지 key를 반환한다."""
    if not articles:
        return set()

    ssl_context = ssl_context_from_config(config)
    base_url = config["settings"]["base_url"]
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_BODY_FETCHES)
    allowed_hosts = allowed_hosts_from_config(config)

    link_articles = [a for a in articles if a.link]
    async with aiohttp.ClientSession(headers=DEFAULT_HEADERS) as session:
        results = await asyncio.gather(
            *(
                # 이미지/첨부파일 상대 경로는 게시물이 있는 호스트 기준으로 해석한다.
                _fetch_article_body_async(
                    session,
                    a.link,
                    ssl_context,
                    base_url_of(a.link) or base_url,
                    semaphore,
                    allowed_hosts,
                    a.description or "",
                )
                for a in link_articles
            ),
            return_exceptions=True,
        )

    enriched: set[str] = set()
    sources: dict[str, int] = {}
    for article, result in zip(link_articles, results, strict=True):
        if isinstance(result, BaseException):
            logger.warning("본문 크롤링 예외 - %s: %s", article.link, result)
            continue
        if result is None:
            continue
        enriched.add(article.key)
        sources[result.body_source] = sources.get(result.body_source, 0) + 1
        article.description = _merge_description(article.description or "", result.body)
        article.images = result.images
        article.attachments = result.attachments
        if article.attachment_count and not result.attachments:
            logger.warning(
                "RSS에는 첨부 %d개가 있지만 상세 페이지에서 찾지 못했습니다 - %s",
                article.attachment_count,
                article.link,
            )
        if result.attachments:
            logger.debug(
                "첨부파일 %d건 발견 - %s: %s",
                len(result.attachments),
                article.title,
                ", ".join(att.filename for att in result.attachments),
            )
    if sources:
        logger.info("상세 본문 %d/%d건 확인 (본문 영역: %s)", len(enriched), len(link_articles), sources)
    return enriched


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
