"""feeds.py 단위 테스트"""

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from tenacity import stop_after_attempt

from ku_notice_monitor.feeds import (
    _extract_attachments,
    _extract_image_urls,
    _extract_rss_content,
    _fetch_feed_async,
    _html_to_markdown,
    _is_retryable_feed_error,
    _is_server_unavailable,
    _merge_description,
    _safe_pub_date_string,
    _strip_html,
    _to_int,
    base_url_of,
    enrich_articles_with_body,
    extract_article_id,
    fetch_all_feeds_detailed,
    is_empty_feed_item,
    normalize_link,
    page_structure_hint,
    parse_article_page,
    parse_pub_date,
)
from ku_notice_monitor.net import DownloadError
from ku_notice_monitor.state import (
    StateCorruptionError,
    filter_new_articles,
    load_state,
    mark_as_seen,
    migrate_legacy_ids,
    save_state,
)

# --- parse_pub_date ---


@pytest.mark.parametrize("date_str,expected", [
    ("2026-03-01 10:30:00.123", datetime(2026, 3, 1, 10, 30, 0)),
    ("2026-03-01 10:30:00", datetime(2026, 3, 1, 10, 30, 0)),
])
def test_parse_pub_date(date_str, expected):
    assert parse_pub_date(date_str) == expected


# --- _safe_pub_date_string ---


def test_safe_pub_date_string_valid():
    entry = {"pubdate": "2026-03-01 10:30:00.123"}
    assert _safe_pub_date_string(entry) == "2026-03-01 10:30:00.123"


def test_safe_pub_date_string_published_fallback():
    entry = {"published": "2026-03-01 10:30:00"}
    assert _safe_pub_date_string(entry) == "2026-03-01 10:30:00"


def test_safe_pub_date_string_empty():
    assert _safe_pub_date_string({}) == ""
    assert _safe_pub_date_string({"pubdate": ""}) == ""


def test_safe_pub_date_string_invalid_preserves_raw():
    # 파싱 실패해도 원문은 보존 (로그만 남김)
    entry = {"pubdate": "invalid-date"}
    assert _safe_pub_date_string(entry) == "invalid-date"


# --- extract_article_id ---


@pytest.mark.parametrize("link,expected", [
    ("/bbs/konkuk/234/1166860/artclView.do", "1166860"),
    ("/bbs/konkuk/999/1234567/artclView.do", "1234567"),
    # kuinc 등 사이트 경로가 konkuk이 아닌 게시판도 ID를 추출해야 한다
    ("/bbs/job/4083/1168188/artclView.do?layout=unknown", "1168188"),
    ("https://kuinc.konkuk.ac.kr/bbs/job/4083/1168188/artclView.do", "1168188"),
    ("https://example.com/page", "https://example.com/page"),
])
def test_extract_article_id(link, expected):
    assert extract_article_id(link) == expected


# --- base_url_of ---


@pytest.mark.parametrize("url,expected", [
    ("https://kuinc.konkuk.ac.kr/bbs/job/4083/rssList.do", "https://kuinc.konkuk.ac.kr"),
    ("https://www.konkuk.ac.kr/bbs/konkuk/234/rssList.do", "https://www.konkuk.ac.kr"),
    ("/bbs/job/4083/rssList.do", ""),
    ("", ""),
])
def test_base_url_of(url, expected):
    assert base_url_of(url) == expected


# --- _is_retryable_feed_error ---


def test_is_retryable_feed_error_4xx_not_retried():
    class FakeResponseError(Exception):
        def __init__(self, status):
            self.status = status

    assert _is_retryable_feed_error(FakeResponseError(404)) is False
    assert _is_retryable_feed_error(FakeResponseError(403)) is False
    assert _is_retryable_feed_error(FakeResponseError(429)) is True  # rate limit은 재시도
    assert _is_retryable_feed_error(FakeResponseError(500)) is True
    assert _is_retryable_feed_error(FakeResponseError(503)) is True


def test_is_retryable_feed_error_network_errors_retried():
    # 상태코드가 없는 네트워크/타임아웃 오류는 재시도
    assert _is_retryable_feed_error(TimeoutError()) is True
    assert _is_retryable_feed_error(ConnectionError()) is True


# --- normalize_link ---


def test_normalize_link_relative():
    result = normalize_link("/bbs/konkuk/234/1166860/artclView.do?param=1", "https://www.konkuk.ac.kr")
    assert result == "https://www.konkuk.ac.kr/bbs/konkuk/234/1166860/artclView.do"


def test_normalize_link_absolute():
    result = normalize_link("https://www.konkuk.ac.kr/page", "https://www.konkuk.ac.kr")
    assert result == "https://www.konkuk.ac.kr/page"


def test_normalize_link_strips_query():
    result = normalize_link("https://www.konkuk.ac.kr/page?a=1", "https://www.konkuk.ac.kr")
    assert result == "https://www.konkuk.ac.kr/page"


# --- is_empty_feed_item ---


def test_is_empty_feed_item_true():
    assert is_empty_feed_item({"title": "No Exist Data Available"}) is True


def test_is_empty_feed_item_false():
    assert is_empty_feed_item({"title": "학사 공지"}) is False


# --- _to_int ---


@pytest.mark.parametrize("value,default,expected", [
    ("42", 0, 42),
    (None, 0, 0),
    ("abc", 0, 0),
    ("abc", -1, -1),
])
def test_to_int(value, default, expected):
    assert _to_int(value, default) == expected


# --- filter_new_articles ---


def test_filter_new_articles_filters_seen(make_article):
    articles = [make_article(id="1"), make_article(id="2"), make_article(id="3")]
    state = {"seen_ids": {"234:1": "2026-01-01T00:00:00", "234:3": "2026-01-01T00:00:00"}}
    result = filter_new_articles(articles, state)
    assert len(result) == 1
    assert result[0].id == "2"


def test_filter_new_articles_empty_state(make_article):
    articles = [make_article(id="1")]
    state = {"seen_ids": {}}
    assert len(filter_new_articles(articles, state)) == 1


# --- mark_as_seen ---


def test_mark_as_seen(make_article):
    articles = [make_article(id="10"), make_article(id="20")]
    state = {"seen_ids": {}}
    mark_as_seen(articles, state)
    assert "234:10" in state["seen_ids"]
    assert "234:20" in state["seen_ids"]


# --- migrate_legacy_ids ---


def test_migrate_legacy_ids(make_article):
    articles = [make_article(id="1", board_id=243)]
    seen = {"1": "2026-01-01T00:00:00"}
    migrate_legacy_ids(articles, seen)
    assert "243:1" in seen
    assert "1" not in seen


def test_migrate_legacy_ids_skips_already_migrated(make_article):
    articles = [make_article(id="1", board_id=243)]
    seen = {"243:1": "2026-01-01T00:00:00"}
    migrate_legacy_ids(articles, seen)
    assert "243:1" in seen


# --- load_state ---


def test_load_state_missing_file(tmp_path):
    result = load_state(str(tmp_path / "nonexistent.json"))
    assert result["seen_ids"] == {}
    assert result["article_fingerprints"] == {}
    assert result["pending_digest"] == []
    assert result["last_run"] is None


def test_load_state_existing_file(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"seen_ids": {"1": "2026-01-01"}, "last_run": "2026-01-01"}')
    result = load_state(str(path))
    assert "1" in result["seen_ids"]


def test_load_state_migrates_link_style_keys(tmp_path):
    """ID 추출 실패로 링크가 키에 저장된 구형 항목을 'board_id:artcl_id'로 정규화한다."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "seen_ids": {
            "4083:/bbs/job/4083/1168188/artclView.do?layout=unknown": "2026-01-01T00:00:00",
            "234:1166860": "2026-01-01T00:00:00",
        },
        "last_run": None,
    }), encoding="utf-8")
    result = load_state(str(path))
    assert "4083:1168188" in result["seen_ids"]
    assert "4083:/bbs/job/4083/1168188/artclView.do?layout=unknown" not in result["seen_ids"]
    assert "234:1166860" in result["seen_ids"]  # 정상 키는 그대로 유지


def test_load_state_corrupted_file_stops_safely(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"seen_ids":', encoding="utf-8")
    with pytest.raises(StateCorruptionError):
        load_state(str(path))


def test_load_state_rejects_future_schema(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"schema_version": 999}', encoding="utf-8")
    with pytest.raises(StateCorruptionError, match="새로운 state 스키마"):
        load_state(str(path))


# --- save_state ---


def test_save_state_creates_file(tmp_path):
    path = str(tmp_path / "state.json")
    state = {"seen_ids": {"1": datetime.now().isoformat()}, "last_run": None}
    save_state(state, path)
    loaded = json.loads(Path(path).read_text())
    assert "1" in loaded["seen_ids"]
    assert loaded["last_run"] is not None


def test_save_state_cleans_old_ids(tmp_path):
    path = str(tmp_path / "state.json")
    old_date = (datetime.now() - timedelta(days=100)).isoformat()
    recent_date = datetime.now().isoformat()
    state = {"seen_ids": {"old": old_date, "recent": recent_date}, "last_run": None}
    save_state(state, path)
    loaded = json.loads(Path(path).read_text())
    assert "old" not in loaded["seen_ids"]
    assert "recent" in loaded["seen_ids"]


def test_save_state_no_tmp_left(tmp_path):
    """atomic write 후 임시 파일이 남지 않는지 확인"""
    path = str(tmp_path / "state.json")
    state = {"seen_ids": {}, "last_run": None}
    save_state(state, path)
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert len(tmp_files) == 0


# --- _extract_image_urls ---


def test_extract_image_urls_basic():
    from bs4 import BeautifulSoup

    html = '<div><img src="/images/notice.jpg"><img src="https://example.com/photo.png"></div>'
    div = BeautifulSoup(html, "html.parser").find("div")
    urls = _extract_image_urls(div, "https://www.konkuk.ac.kr")
    assert urls == ["https://www.konkuk.ac.kr/images/notice.jpg", "https://example.com/photo.png"]


def test_extract_image_urls_empty():
    from bs4 import BeautifulSoup

    html = "<div><p>텍스트만 있는 공지</p></div>"
    div = BeautifulSoup(html, "html.parser").find("div")
    urls = _extract_image_urls(div, "https://www.konkuk.ac.kr")
    assert urls == []


def test_html_to_markdown_preserves_table_relationships():
    from bs4 import BeautifulSoup

    html = """
    <div>
      <h2>지원 자격</h2>
      <table>
        <tr><th>학년</th><th>마감</th></tr>
        <tr><td>2학년</td><td>8월 10일</td></tr>
      </table>
      <ul><li>서류 제출</li></ul>
    </div>
    """
    content = BeautifulSoup(html, "html.parser").find("div")
    markdown = _html_to_markdown(content)
    assert "## 지원 자격" in markdown
    assert "| 학년 | 마감 |" in markdown
    assert "| 2학년 | 8월 10일 |" in markdown
    assert "- 서류 제출" in markdown


def test_extract_image_urls_respects_max_limit():
    from bs4 import BeautifulSoup

    from ku_notice_monitor.constants import MAX_IMAGES_PER_ARTICLE

    imgs = "".join(f'<img src="https://example.com/img{i}.jpg">' for i in range(MAX_IMAGES_PER_ARTICLE + 5))
    html = f"<div>{imgs}</div>"
    div = BeautifulSoup(html, "html.parser").find("div")
    urls = _extract_image_urls(div, "https://www.konkuk.ac.kr")
    assert len(urls) == MAX_IMAGES_PER_ARTICLE


def test_extract_image_urls_skips_empty_src():
    from bs4 import BeautifulSoup

    html = '<div><img src=""><img><img src="https://example.com/valid.jpg"></div>'
    div = BeautifulSoup(html, "html.parser").find("div")
    urls = _extract_image_urls(div, "https://www.konkuk.ac.kr")
    assert urls == ["https://example.com/valid.jpg"]


def test_extract_image_urls_lazy_loaded():
    from bs4 import BeautifulSoup

    html = (
        '<div>'
        '<img data-src="https://example.com/lazy1.jpg">'
        '<img data-original="https://example.com/lazy2.png">'
        '<img src="data:image/gif;base64,R0" data-lazy-src="https://example.com/lazy3.webp">'
        "</div>"
    )
    div = BeautifulSoup(html, "html.parser").find("div")
    urls = _extract_image_urls(div, "https://www.konkuk.ac.kr")
    assert "https://example.com/lazy1.jpg" in urls
    assert "https://example.com/lazy2.png" in urls
    assert "https://example.com/lazy3.webp" in urls


def test_extract_image_urls_srcset():
    from bs4 import BeautifulSoup

    html = (
        '<div>'
        '<img srcset="https://example.com/small.jpg 480w, https://example.com/large.jpg 1024w">'
        "</div>"
    )
    div = BeautifulSoup(html, "html.parser").find("div")
    urls = _extract_image_urls(div, "https://www.konkuk.ac.kr")
    assert "https://example.com/small.jpg" in urls
    assert "https://example.com/large.jpg" in urls


def test_extract_image_urls_filters_tracking_and_svg():
    from bs4 import BeautifulSoup

    html = (
        '<div>'
        '<img src="https://example.com/spacer.gif">'
        '<img src="https://example.com/1x1-pixel.png">'
        '<img src="https://example.com/icon-arrow.png">'
        '<img src="https://example.com/logo.svg">'
        '<img src="https://example.com/real-photo.jpg">'
        "</div>"
    )
    div = BeautifulSoup(html, "html.parser").find("div")
    urls = _extract_image_urls(div, "https://www.konkuk.ac.kr")
    assert urls == ["https://example.com/real-photo.jpg"]


def test_extract_image_urls_deduplicates():
    from bs4 import BeautifulSoup

    html = (
        '<div>'
        '<img src="https://example.com/same.jpg">'
        '<img src="https://example.com/same.jpg">'
        '<img data-src="https://example.com/same.jpg">'
        "</div>"
    )
    div = BeautifulSoup(html, "html.parser").find("div")
    urls = _extract_image_urls(div, "https://www.konkuk.ac.kr")
    assert urls == ["https://example.com/same.jpg"]


def test_extract_image_urls_og_image_fallback():
    from bs4 import BeautifulSoup

    html = (
        '<html><head>'
        '<meta property="og:image" content="https://example.com/og.jpg">'
        "</head><body>"
        '<div class="hwp_editor_board_content"><p>텍스트만 있는 공지</p></div>'
        "</body></html>"
    )
    soup = BeautifulSoup(html, "html.parser")
    div = soup.find("div")
    urls = _extract_image_urls(div, "https://www.konkuk.ac.kr", soup=soup)
    assert urls == ["https://example.com/og.jpg"]


def test_og_image_is_not_added_when_body_has_images():
    from bs4 import BeautifulSoup

    html = (
        '<html><head>'
        '<meta property="og:image" content="https://example.com/site-logo.jpg">'
        "</head><body>"
        '<div class="hwp_editor_board_content"><img src="https://example.com/poster.jpg"></div>'
        "</body></html>"
    )
    soup = BeautifulSoup(html, "html.parser")
    div = soup.find("div")
    urls = _extract_image_urls(div, "https://www.konkuk.ac.kr", soup=soup)
    assert urls == ["https://example.com/poster.jpg"]


# --- _merge_description ---


def test_merge_description_does_not_repeat_rss_text_already_in_body():
    rss = "신청 기간: 9. 22. ~ 10. 1. 제출 서류: 신청서 1부"
    crawled = "신청 기간: 9. 22. ~ 10. 1.\n\n- 제출 서류: 신청서 1부"
    assert _merge_description(rss, crawled) == crawled


def test_merge_description_keeps_rss_when_it_already_has_body():
    rss = "안내 본문 전체와 문의처 02-450-0000"
    assert _merge_description(rss, "안내 본문 전체") == rss


def test_merge_description_joins_different_texts_and_keeps_rss_on_crawl_failure():
    assert _merge_description("RSS 요약", "상세 본문") == "RSS 요약\n상세 본문"
    assert _merge_description("RSS 요약", "") == "RSS 요약"
    assert _merge_description("", "상세 본문") == "상세 본문"


# --- _strip_html / _extract_rss_content ---


def test_strip_html_basic():
    assert _strip_html("<p>안녕<br>하세요</p>") == "안녕 하세요"
    assert _strip_html("A &amp; B &lt;tag&gt;") == "A & B <tag>"
    assert _strip_html("") == ""


def test_strip_html_numeric_and_named_entities():
    # 수치 참조(&#39;)와 &nbsp; 같은 명명 엔티티도 복원/정규화되어야 한다
    assert _strip_html("Kim&#39;s&nbsp;공지") == "Kim's 공지"


def test_extract_rss_content_prefers_longest():
    entry = {
        "description": "짧은 요약",
        "content": [{"value": "<p>훨씬 더 긴 본문 내용입니다. 여기에 중요한 정보가 있습니다.</p>"}],
    }
    result = _extract_rss_content(entry)
    assert "훨씬 더 긴" in result
    assert "<p>" not in result


def test_extract_rss_content_description_only():
    entry = {"description": "<b>설명 본문</b>"}
    assert _extract_rss_content(entry) == "설명 본문"


def test_extract_rss_content_empty():
    assert _extract_rss_content({}) == ""
    assert _extract_rss_content({"description": ""}) == ""


def test_extract_rss_content_summary_detail_dict():
    entry = {"summary_detail": {"value": "<p>요약 상세</p>"}}
    assert _extract_rss_content(entry) == "요약 상세"


# --- _extract_attachments ---


def test_extract_attachments_basic():
    from bs4 import BeautifulSoup

    html = """
    <div class="attachments">
      <ul>
        <li><a href="/bbs/konkuk/234/1209265/download.do">안내문.hwp</a> 미리보기</li>
        <li><a href="/bbs/konkuk/234/1209266/download.do">양식.pdf</a> 미리보기</li>
      </ul>
    </div>
    """
    soup = BeautifulSoup(html, "html.parser")
    attachments = _extract_attachments(soup, "https://www.konkuk.ac.kr")
    assert len(attachments) == 2
    assert attachments[0].filename == "안내문.hwp"
    assert attachments[0].url == "https://www.konkuk.ac.kr/bbs/konkuk/234/1209265/download.do"
    assert attachments[1].filename == "양식.pdf"


def test_extract_attachments_scans_page_when_skin_has_no_attachment_section():
    """게시판 스킨마다 첨부 목록 마크업이 달라 전용 영역이 없어도 첨부를 찾는다."""
    from bs4 import BeautifulSoup

    html = """
    <dl class="file-list"><dd><ul>
      <li><a href="/bbs/konkuk/235/555/download.do">2026 장학 안내문.hwp (35KB)</a>
          <a href="/bbs/konkuk/235/555/download.do"><img alt="다운로드"></a>
          <a href="/synap/preview.do?id=555">미리보기</a></li>
      <li><a href="/bbs/konkuk/235/556/download.do" title="신청서.docx"></a></li>
    </ul></dd></dl>
    """
    soup = BeautifulSoup(html, "html.parser")
    attachments = _extract_attachments(soup, "https://www.konkuk.ac.kr")
    assert [(att.filename, att.ext) for att in attachments] == [
        ("2026 장학 안내문.hwp", ".hwp"),
        ("신청서.docx", ".docx"),
    ]


def test_extract_attachments_no_section():
    from bs4 import BeautifulSoup

    html = "<div><p>첨부파일 없음</p></div>"
    soup = BeautifulSoup(html, "html.parser")
    attachments = _extract_attachments(soup, "https://www.konkuk.ac.kr")
    assert attachments == []


def test_extract_attachments_skips_non_download_links():
    from bs4 import BeautifulSoup

    html = """
    <div class="attachments">
      <ul>
        <li><a href="/bbs/konkuk/234/1209265/download.do">파일.pdf</a></li>
        <li><a href="https://example.com/other">기타 링크</a></li>
      </ul>
    </div>
    """
    soup = BeautifulSoup(html, "html.parser")
    attachments = _extract_attachments(soup, "https://www.konkuk.ac.kr")
    assert len(attachments) == 1
    assert attachments[0].filename == "파일.pdf"


def test_extract_attachments_absolute_url():
    from bs4 import BeautifulSoup

    html = """
    <div class="attachments">
      <ul>
        <li><a href="https://www.konkuk.ac.kr/bbs/konkuk/234/123/download.do">파일.xlsx</a></li>
      </ul>
    </div>
    """
    soup = BeautifulSoup(html, "html.parser")
    attachments = _extract_attachments(soup, "https://www.konkuk.ac.kr")
    assert len(attachments) == 1
    assert attachments[0].url == "https://www.konkuk.ac.kr/bbs/konkuk/234/123/download.do"


def test_filter_new_articles_migrates_legacy_id_key(make_article):
    articles = [make_article(id="1", board_id=243)]
    state = {"seen_ids": {"1": "2026-01-01T00:00:00"}}
    result = filter_new_articles(articles, state)
    assert result == []
    assert "243:1" in state["seen_ids"]
    assert "1" not in state["seen_ids"]


def test_fetch_all_feeds_reports_partial_failure(make_article):
    config = {
        "feeds": {
            "학사": {"id": 234, "enabled": True},
            "장학": {"id": 235, "enabled": True},
        },
        "settings": {
            "ssl_verify": True,
            "base_url": "https://www.konkuk.ac.kr",
            "rss_url_template": "https://www.konkuk.ac.kr/{board_id}",
            "allowed_download_hosts": ["konkuk.ac.kr"],
        },
    }

    async def fake_fetch(_session, name, *_args):
        if name == "장학":
            raise TimeoutError("feed timeout")
        return [make_article()]

    with (
        patch("ku_notice_monitor.feeds.make_ssl_context", return_value=None),
        patch("ku_notice_monitor.feeds._fetch_feed_async", side_effect=fake_fetch),
    ):
        batch = asyncio.run(fetch_all_feeds_detailed(config))
    assert len(batch.articles) == 1
    assert batch.successful_count == 1
    assert batch.failed_count == 1
    assert batch.statuses[1].error == "응답 시간 초과"
    assert batch.statuses[1].server_unavailable is True
    assert batch.is_source_outage is False


def test_non_rss_response_is_a_failed_feed_not_an_empty_one():
    """점검 안내 같은 HTML 응답을 '글 0건 성공'으로 집계하지 않는다."""
    config = {
        "feeds": {},
        "settings": {
            "base_url": "https://www.konkuk.ac.kr",
            "rss_url_template": "https://www.konkuk.ac.kr/bbs/konkuk/{board_id}/rssList.do",
            "allowed_download_hosts": ["konkuk.ac.kr"],
        },
    }

    async def fake_download(*_args, **_kwargs):
        return b"<html><body>system maintenance</body></html>"

    fetch_once = _fetch_feed_async.retry_with(stop=stop_after_attempt(1))
    with patch("ku_notice_monitor.feeds.download_bytes", side_effect=fake_download):
        with pytest.raises(DownloadError) as caught:
            asyncio.run(fetch_once(None, "학사", 234, {"id": 234}, config, None))

    assert "RSS" in str(caught.value)
    # 게시판 주소 변경일 수도 있으므로 학교 서버 장애로 조용히 넘기지 않는다.
    assert _is_server_unavailable(caught.value) is False


# --- 상세 본문 보강 ---

_ARTICLE_HTML = """
<html><body>
<div class="hwp_editor_board_content"><p>2026-09-30까지 신청</p></div>
<div class="attachments"><a href="/bbs/konkuk/234/1/download.do">안내.pdf</a></div>
</body></html>
""".encode()


def _enrich_config():
    return {
        "feeds": {},
        "settings": {
            "ssl_verify": True,
            "base_url": "https://www.konkuk.ac.kr",
            "allowed_download_hosts": ["konkuk.ac.kr"],
        },
    }


def test_enrich_reports_only_successfully_crawled_articles(make_article):
    ok = make_article(id="1", description="RSS", link="https://www.konkuk.ac.kr/notice/1")
    failed = make_article(id="2", description="RSS", link="https://www.konkuk.ac.kr/notice/2")

    async def fake_download(_session, url, **_kwargs):
        return _ARTICLE_HTML if url.endswith("/1") else None

    with patch("ku_notice_monitor.feeds.download_bytes", side_effect=fake_download):
        enriched = asyncio.run(enrich_articles_with_body([ok, failed], _enrich_config()))

    assert enriched == {ok.key}
    assert "2026-09-30까지 신청" in ok.description
    assert [att.filename for att in ok.attachments] == ["안내.pdf"]
    # 실패한 공지는 RSS 내용 그대로 남아야 한다.
    assert failed.description == "RSS"
    assert failed.attachments == []


_RSS_SUMMARY = (
    "2026학년도 2학기 교내 근로장학생을 아래와 같이 모집하오니 희망하는 학생은 기간 내 "
    "신청하시기 바랍니다. 신청 기간: 9. 22.(월) ~ 10. 1.(수) 18:00까지"
)

# 기존 선택자(hwp_editor_board_content, attachments)가 하나도 없는 게시판 스킨.
_UNKNOWN_SKIN_HTML = f"""
<html><head><title>교내 근로장학생 모집 | 건국대학교</title>
<meta property="og:image" content="https://www.konkuk.ac.kr/logo.png"></head>
<body>
<div id="gnb"><ul><li>장학공지</li><li>수강신청</li><li>등록금</li></ul></div>
<div class="sns-share" style="display:none"><div>{_RSS_SUMMARY}</div></div>
<div class="board-wrap">
  <h2 class="view-title">교내 근로장학생 모집</h2>
  <div class="view-info">작성일 2026-09-20 조회수 120</div>
  <div class="view-body">
    <p>2026학년도 2학기 교내 근로장학생을 아래와 같이 모집하오니</p>
    <p>희망하는 학생은 기간 내 신청하시기 바랍니다.</p>
    <table><tr><th>신청 기간</th><td>9. 22.(월) ~ 10. 1.(수) 18:00까지</td></tr>
           <tr><th>제출 서류</th><td>신청서 1부</td></tr></table>
    <p><img src="/_attach/image/2026/09/poster.png"></p>
  </div>
  <dl class="file-list"><dd><ul>
    <li><a href="/bbs/konkuk/235/777/download.do">근로장학 신청서.hwp (20KB)</a></li>
  </ul></dd></dl>
</div>
<div id="footer">개인정보처리방침 장학 안내</div>
</body></html>
"""


def test_parse_article_page_finds_body_from_rss_summary_on_unknown_skin():
    page = parse_article_page(
        _UNKNOWN_SKIN_HTML,
        base_url="https://www.konkuk.ac.kr",
        allowed_hosts={"konkuk.ac.kr"},
        rss_summary=_RSS_SUMMARY[:120] + "...",
    )

    assert page is not None
    assert page.body_source == "rss-summary"
    assert "제출 서류 | 신청서 1부" in page.body
    # 메뉴·숨은 공유 영역·꼬리말과 작성 정보는 본문에 섞이지 않는다.
    assert "수강신청" not in page.body
    assert "개인정보처리방침" not in page.body
    assert "조회수" not in page.body
    assert page.images == ["https://www.konkuk.ac.kr/_attach/image/2026/09/poster.png"]
    assert [att.filename for att in page.attachments] == ["근로장학 신청서.hwp"]


def test_parse_article_page_uses_known_skin_selector_without_rss_summary():
    html = """
    <html><body><div id="menu">학사 장학</div>
    <div class="artclView"><p>포스터 이미지로 안내합니다.</p></div></body></html>
    """
    page = parse_article_page(
        html,
        base_url="https://www.konkuk.ac.kr",
        allowed_hosts={"konkuk.ac.kr"},
    )

    assert page is not None
    assert page.body_source == "div.artclView"
    assert page.body == "포스터 이미지로 안내합니다."


def test_parse_article_page_ignores_too_short_summary_anchor():
    html = "<html><body><div id='title'><div>장학 안내</div></div></body></html>"
    assert parse_article_page(
        html,
        base_url="https://www.konkuk.ac.kr",
        allowed_hosts={"konkuk.ac.kr"},
        rss_summary="장학 안내",
    ) is None


def test_page_structure_hint_summarizes_markup_for_logs():
    from bs4 import BeautifulSoup

    hint = page_structure_hint(BeautifulSoup(_UNKNOWN_SKIN_HTML, "lxml"))

    assert "교내 근로장학생 모집" in hint
    assert "download 링크 1개" in hint
    assert "view-body" in hint and "file-list" in hint


def test_enrich_uses_rss_summary_to_read_unknown_skin(make_article):
    article = make_article(
        description=_RSS_SUMMARY,
        link="https://www.konkuk.ac.kr/bbs/konkuk/235/1/artclView.do",
        attachment_count=1,
    )

    async def fake_download(*_args, **_kwargs):
        return _UNKNOWN_SKIN_HTML.encode()

    with patch("ku_notice_monitor.feeds.download_bytes", side_effect=fake_download):
        enriched = asyncio.run(enrich_articles_with_body([article], _enrich_config()))

    assert enriched == {article.key}
    # RSS 요약이 본문 앞부분과 같으므로 두 번 넣지 않는다.
    assert article.description.count("교내 근로장학생을") == 1
    assert "신청서 1부" in article.description
    assert [att.filename for att in article.attachments] == ["근로장학 신청서.hwp"]


def test_enrich_treats_page_without_body_or_attachments_as_failure(make_article):
    article = make_article(link="https://www.konkuk.ac.kr/notice/1")

    async def fake_download(*_args, **_kwargs):
        return b"<html><body>error</body></html>"

    with patch("ku_notice_monitor.feeds.download_bytes", side_effect=fake_download):
        enriched = asyncio.run(enrich_articles_with_body([article], _enrich_config()))

    assert enriched == set()
