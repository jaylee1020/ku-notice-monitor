"""공지 확인 상태 관리 모듈 (state.json 읽기/쓰기)"""

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from constants import STATE_RETENTION_DAYS
from models import Article, ClassifiedNotice

logger = logging.getLogger(__name__)


def _initial_state() -> dict:
    return {
        "seen_ids": {},
        "article_fingerprints": {},
        "pending_digest": [],
        "last_run": None,
    }


def _normalize_seen_key(key: str) -> str:
    """게시물 ID 대신 링크가 저장된 구형 키를 'board_id:artcl_id' 형식으로 정규화한다.

    과거 extract_article_id()가 www.konkuk.ac.kr 외 게시판(예: kuinc)의 링크에서
    ID를 추출하지 못해 '4083:/bbs/job/4083/1168188/artclView.do?...' 형태로
    저장된 키를 '4083:1168188'로 변환한다. 변환하지 않으면 ID 추출 수정 후
    해당 공지들이 전부 신규로 재인식되어 중복 알림이 발생한다.
    """
    board, sep, rest = key.partition(":")
    if not sep:
        return key
    match = re.search(r"/(\d+)/artclView", rest)
    return f"{board}:{match.group(1)}" if match else key


def load_state(state_path: str) -> dict:
    """state.json 로드. 없거나 손상되면 초기 상태 반환"""
    path = Path(state_path)
    if not path.exists():
        return _initial_state()

    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("state 파일 로드 실패, 초기 상태로 복구합니다: %s", e)
        return _initial_state()

    if not isinstance(state, dict):
        logger.warning("state 파일 형식 오류(객체 아님). 초기 상태로 복구합니다.")
        return _initial_state()

    state.setdefault("seen_ids", {})
    state.setdefault("article_fingerprints", {})
    state.setdefault("pending_digest", [])
    state.setdefault("last_run", None)
    if not isinstance(state["seen_ids"], dict):
        state["seen_ids"] = {}
    if not isinstance(state["article_fingerprints"], dict):
        state["article_fingerprints"] = {}
    if not isinstance(state["pending_digest"], list):
        state["pending_digest"] = []

    normalized_seen: dict[str, str] = {}
    for k, v in state["seen_ids"].items():
        if isinstance(v, str):
            normalized_seen[_normalize_seen_key(str(k))] = v
    state["seen_ids"] = normalized_seen
    state["article_fingerprints"] = {
        _normalize_seen_key(str(k)): str(v)
        for k, v in state["article_fingerprints"].items()
        if isinstance(v, str)
    }
    state["pending_digest"] = [
        item for item in state["pending_digest"]
        if isinstance(item, dict) and isinstance(item.get("article"), dict)
    ]

    return state


def save_state(state: dict, state_path: str) -> None:
    """state.json 저장 + 90일 지난 ID 자동 정리 (원자적 쓰기)"""
    cutoff = (datetime.now() - timedelta(days=STATE_RETENTION_DAYS)).isoformat()
    state["seen_ids"] = {
        str(k): v for k, v in state["seen_ids"].items()
        if isinstance(v, str) and v > cutoff
    }
    state["article_fingerprints"] = {
        str(k): str(v)
        for k, v in state.get("article_fingerprints", {}).items()
        if k in state["seen_ids"] and isinstance(v, str)
    }
    state["last_run"] = datetime.now().isoformat()

    target = Path(state_path)
    fd, tmp_path = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, state_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    logger.debug("상태 저장 완료: %s", state_path)


def migrate_legacy_ids(articles: list[Article], seen: dict[str, str]) -> None:
    """구형 포맷(id 단독) → 신규 포맷(board_id:id)으로 마이그레이션"""
    for article in articles:
        if article.id in seen and article.key not in seen:
            seen[article.key] = seen.pop(article.id)


def filter_new_articles(articles: list[Article], state: dict) -> list[Article]:
    """신규 또는 내용이 바뀐 공지를 반환하고 피드 중복을 제거한다."""
    seen = state.get("seen_ids", {})
    fingerprints = state.setdefault("article_fingerprints", {})

    migrate_legacy_ids(articles, seen)

    result_by_key: dict[str, Article] = {}
    for article in articles:
        if article.key not in seen:
            result_by_key[article.key] = article
            continue
        previous = fingerprints.get(article.key)
        if previous and previous != article.fingerprint:
            article.is_update = True
            result_by_key[article.key] = article
    return list(result_by_key.values())


def mark_as_seen(
    articles: list[Article],
    state: dict,
    fingerprints: dict[str, str] | None = None,
) -> None:
    """공지 ID를 state에 기록"""
    now = datetime.now().isoformat()
    stored_fingerprints = state.setdefault("article_fingerprints", {})
    for a in articles:
        state["seen_ids"][a.key] = now
        stored_fingerprints[a.key] = (
            fingerprints.get(a.key, a.fingerprint) if fingerprints else a.fingerprint
        )


def enqueue_digest(matches: list[ClassifiedNotice], state: dict) -> None:
    """일반 공지를 다음 일일 요약까지 중복 없이 보관한다."""
    pending: dict[str, dict] = {}
    for item in state.setdefault("pending_digest", []):
        try:
            pending[str(item["article"]["board_id"]) + ":" + str(item["article"]["id"])] = item
        except (KeyError, TypeError):
            continue
    for match in matches:
        pending[match.article.key] = match.to_dict()
    state["pending_digest"] = list(pending.values())[-200:]


def get_pending_digest(state: dict) -> list[ClassifiedNotice]:
    results: list[ClassifiedNotice] = []
    for item in state.get("pending_digest", []):
        try:
            results.append(ClassifiedNotice.from_dict(item))
        except (KeyError, TypeError, ValueError):
            logger.warning("손상된 요약 대기 항목을 건너뜁니다.")
    return results


def clear_pending_digest(state: dict) -> None:
    state["pending_digest"] = []
