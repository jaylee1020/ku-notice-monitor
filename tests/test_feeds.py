"""feeds.py 단위 테스트"""

import json
from datetime import datetime, timedelta
from pathlib import Path

from feeds import (
    _to_int,
    extract_article_id,
    is_empty_feed_item,
    normalize_link,
    parse_pub_date,
)
from state import (
    filter_new_articles,
    load_state,
    mark_as_seen,
    save_state,
)

# --- parse_pub_date ---


def test_parse_pub_date_with_milliseconds():
    result = parse_pub_date("2026-03-01 10:30:00.123")
    assert result == datetime(2026, 3, 1, 10, 30, 0)


def test_parse_pub_date_without_milliseconds():
    result = parse_pub_date("2026-03-01 10:30:00")
    assert result == datetime(2026, 3, 1, 10, 30, 0)


# --- extract_article_id ---


def test_extract_article_id_valid():
    assert extract_article_id("/bbs/konkuk/234/1166860/artclView.do") == "1166860"


def test_extract_article_id_different_board():
    assert extract_article_id("/bbs/konkuk/999/1234567/artclView.do") == "1234567"


def test_extract_article_id_invalid_returns_link():
    assert extract_article_id("https://example.com/page") == "https://example.com/page"


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


def test_to_int_valid():
    assert _to_int("42") == 42


def test_to_int_none():
    assert _to_int(None) == 0


def test_to_int_invalid_string():
    assert _to_int("abc") == 0


def test_to_int_custom_default():
    assert _to_int("abc", default=-1) == -1


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


# --- load_state ---


def test_load_state_missing_file(tmp_path):
    result = load_state(str(tmp_path / "nonexistent.json"))
    assert result == {"seen_ids": {}, "last_run": None}


def test_load_state_existing_file(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"seen_ids": {"1": "2026-01-01"}, "last_run": "2026-01-01"}')
    result = load_state(str(path))
    assert "1" in result["seen_ids"]


def test_load_state_corrupted_file_returns_default(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"seen_ids":', encoding="utf-8")
    result = load_state(str(path))
    assert result == {"seen_ids": {}, "last_run": None}


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


def test_filter_new_articles_migrates_legacy_id_key(make_article):
    articles = [make_article(id="1", board_id=243)]
    state = {"seen_ids": {"1": "2026-01-01T00:00:00"}}
    result = filter_new_articles(articles, state)
    assert result == []
    assert "243:1" in state["seen_ids"]
    assert "1" not in state["seen_ids"]
