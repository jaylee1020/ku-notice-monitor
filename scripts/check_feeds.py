"""RSS 피드 점검 도구.

설정된 피드와 인자로 받은 후보 URL을 내려받아 상태 코드, 채널 제목, 글 수,
최신 글을 출력한다. 게시판 ID가 바뀌었거나 새 게시판을 추가하기 전에 확인하는 용도다.
최신 글의 상세 페이지도 모니터와 같은 방식으로 읽어 본문·첨부를 찾는지 보여 준다.

    uv run --no-sync python scripts/check_feeds.py [URL ...]
    uv run --no-sync python scripts/check_feeds.py --scan-konkuk 230-260
"""

import argparse
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import feedparser
import yaml
from bs4 import BeautifulSoup

from ku_notice_monitor.feeds import base_url_of, page_structure_hint, parse_article_page

ROOT = Path(__file__).resolve().parent.parent
HEADERS = {"User-Agent": "Mozilla/5.0"}
KONKUK_TEMPLATE = "https://www.konkuk.ac.kr/bbs/konkuk/{board_id}/rssList.do"


def configured_feeds() -> list[tuple[str, str, bool]]:
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    template = config["settings"]["rss_url_template"]
    return [
        (
            name,
            feed.get("rss_url") or template.format(board_id=feed["id"]),
            bool(feed.get("enabled", True)),
        )
        for name, feed in config["feeds"].items()
    ]


def fetch(url: str) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status, response.read(5 * 1024 * 1024)


def probe_article(link: str, rss_summary: str) -> str:
    """상세 페이지에서 모니터가 본문·첨부를 찾는지 확인한다."""
    try:
        _, body = fetch(link)
    except urllib.error.HTTPError as exc:
        return f"상세 페이지 HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - 점검 도구는 모든 실패를 보고한다.
        return f"상세 페이지 실패 {type(exc).__name__}: {exc}"
    html = body.decode("utf-8", errors="replace")
    page = parse_article_page(
        html,
        base_url=base_url_of(link),
        allowed_hosts={"konkuk.ac.kr"},
        rss_summary=rss_summary,
    )
    if page is None:
        return f"상세: 본문·첨부를 찾지 못함 ({page_structure_hint(BeautifulSoup(html, 'lxml'))})"
    files = ", ".join(att.filename for att in page.attachments[:3])
    return (
        f"상세: 본문 {len(page.body)}자(영역 {page.body_source}), "
        f"이미지 {len(page.images)}개, 첨부 {len(page.attachments)}개"
        + (f" [{files}]" if files else "")
    )


def probe(url: str, *, check_article: bool = True) -> str:
    try:
        status, body = fetch(url)
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - 점검 도구는 모든 실패를 보고한다.
        return f"실패 {type(exc).__name__}: {exc}"
    if b"<rss" not in body.lower():
        return f"HTTP {status}, RSS 아님 ({len(body)} bytes)"
    feed = feedparser.parse(body)
    entries = [e for e in feed.entries if "no exist data" not in e.get("title", "").lower()]
    title = feed.feed.get("title", "(제목 없음)")
    lines = [f"HTTP {status}, 채널 '{title}', 글 {len(entries)}건"]
    for entry in entries[:3]:
        date = entry.get("pubdate") or entry.get("published") or ""
        lines.append(f"    - {date[:10]} {entry.get('title', '').strip()[:70]}")
        lines.append(f"      {entry.get('link', '')}")
    if check_article and entries:
        latest = entries[0]
        link = urllib.parse.urljoin(url, latest.get("link", ""))
        summary = BeautifulSoup(latest.get("description", ""), "lxml").get_text(" ", strip=True)
        lines.append(f"    {probe_article(link, summary)}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("urls", nargs="*", help="추가로 확인할 RSS URL")
    parser.add_argument(
        "--scan-konkuk",
        metavar="START-END",
        help="www.konkuk.ac.kr 게시판 ID 범위를 훑어 RSS가 있는 게시판을 찾는다",
    )
    args = parser.parse_args()

    print("== 설정된 피드 ==")
    for name, url, enabled in configured_feeds():
        state = "" if enabled else " (비활성)"
        print(f"[{name}{state}] {url}\n  {probe(url)}")

    if args.urls:
        print("\n== 후보 URL ==")
        for url in args.urls:
            print(f"{url}\n  {probe(url)}")

    if args.scan_konkuk:
        start, _, end = args.scan_konkuk.partition("-")
        print(f"\n== www.konkuk.ac.kr 게시판 {start}~{end} ==")
        for board_id in range(int(start), int(end or start) + 1):
            result = probe(KONKUK_TEMPLATE.format(board_id=board_id), check_article=False)
            if result.startswith("HTTP 200, 채널"):
                print(f"[{board_id}] {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
