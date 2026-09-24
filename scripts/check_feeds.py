"""RSS 피드 점검 도구.

설정된 피드와 인자로 받은 후보 URL을 내려받아 상태 코드, 채널 제목, 글 수,
최신 글을 출력한다. 게시판 ID가 바뀌었거나 새 게시판을 추가하기 전에 확인하는 용도다.

    uv run --no-sync python scripts/check_feeds.py [URL ...]
    uv run --no-sync python scripts/check_feeds.py --scan-konkuk 230-260
"""

import argparse
import sys
import urllib.error
import urllib.request
from pathlib import Path

import feedparser
import yaml

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


def probe(url: str) -> str:
    request = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read(5 * 1024 * 1024)
            status = response.status
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
    return "\n".join(lines)


def inspect_article(url: str) -> str:
    """공지 상세 페이지 구조를 요약해 본문 영역 선택자를 찾는 데 쓴다."""
    from collections import Counter

    from bs4 import BeautifulSoup

    request = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            final_url = response.geturl()
            html = response.read(5 * 1024 * 1024).decode("utf-8", errors="replace")
            status = response.status
    except Exception as exc:  # noqa: BLE001 - 점검 도구는 모든 실패를 보고한다.
        return f"실패 {type(exc).__name__}: {exc}"
    soup = BeautifulSoup(html, "lxml")
    lines = [f"HTTP {status}, 최종 URL {final_url}, HTML {len(html)}자"]
    lines.append(f"  title: {soup.title.get_text(strip=True) if soup.title else '(없음)'}")
    classes = Counter(
        cls
        for tag in soup.find_all(["div", "section", "article", "td"])
        for cls in (tag.get("class") or [])
    )
    lines.append("  클래스: " + ", ".join(f"{name}({count})" for name, count in classes.most_common(60)))
    candidates = sorted(
        (
            (len(tag.get_text(" ", strip=True)), tag.name, " ".join(tag.get("class") or []), tag.get("id") or "")
            for tag in soup.find_all(["div", "section", "article", "td"])
            if tag.get("class") or tag.get("id")
        ),
        reverse=True,
    )[:15]
    lines.append("  텍스트가 긴 요소:")
    lines.extend(f"    {length}자 <{name} class='{cls}' id='{ident}'>" for length, name, cls, ident in candidates)
    body = soup.body.get_text(" ", strip=True) if soup.body else html
    lines.append("  본문 앞부분: " + body[:600])
    for script in soup.find_all("script", src=False)[:8]:
        code = script.get_text(" ", strip=True)
        if any(word in code for word in ("location", "ajax", "fetch", "artcl")):
            lines.append("  스크립트: " + code[:300])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("urls", nargs="*", help="추가로 확인할 RSS URL")
    parser.add_argument(
        "--scan-konkuk",
        metavar="START-END",
        help="www.konkuk.ac.kr 게시판 ID 범위를 훑어 RSS가 있는 게시판을 찾는다",
    )
    parser.add_argument(
        "--inspect",
        action="append",
        default=[],
        metavar="ARTICLE_URL",
        help="공지 상세 페이지의 HTML 구조를 요약한다 (여러 번 지정 가능)",
    )
    args = parser.parse_args()

    if args.inspect:
        for url in args.inspect:
            print(f"== 상세 페이지 {url} ==\n{inspect_article(url)}\n")
        return 0

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
            result = probe(KONKUK_TEMPLATE.format(board_id=board_id))
            if result.startswith("HTTP 200, 채널"):
                print(f"[{board_id}] {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
