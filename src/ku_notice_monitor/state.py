"""공지 확인 상태 관리 모듈 (state.json 읽기/쓰기)"""

import hashlib
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from .constants import STATE_RETENTION_DAYS
from .models import Article, ClassifiedNotice

logger = logging.getLogger(__name__)

STATE_SCHEMA_VERSION = 5
MAX_PENDING_DIGEST = 200
MAX_PENDING_DELIVERIES = 500


class StateCorruptionError(RuntimeError):
    """상태 파일을 안전하게 신뢰할 수 없을 때 발생한다."""


class StateCapacityError(RuntimeError):
    """대기열이 안전 한도를 초과했을 때 발생한다."""


def _initial_state() -> dict:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "seen_ids": {},
        "article_fingerprints": {},
        "enriched_fingerprints": {},
        "pending_digest": [],
        "pending_deliveries": [],
        "delivery_history": {},
        "urgent_notice_history": {},
        "classification_retries": {},
        "last_digest_enqueued_date": None,
        "last_digest_sent_date": None,
        "last_detail_refresh_at": None,
        "profile_document_hash": None,
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
    """state.json을 로드한다.

    기존 상태가 손상되었는데 빈 상태로 계속 실행하면 모든 공지를 신규로
    오인할 수 있으므로, 파일이 존재하는 경우에는 조용히 초기화하지 않는다.
    """
    path = Path(state_path)
    if not path.exists():
        return _initial_state()

    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise StateCorruptionError(
            f"state 파일을 읽을 수 없습니다. 원본을 보존한 채 실행을 중단합니다: {e}"
        ) from e

    if not isinstance(state, dict):
        raise StateCorruptionError("state 파일 최상위 값은 JSON 객체여야 합니다.")

    schema_version = state.get("schema_version", 1)
    if not isinstance(schema_version, int) or schema_version < 1:
        raise StateCorruptionError("state.schema_version은 1 이상의 정수여야 합니다.")
    if schema_version > STATE_SCHEMA_VERSION:
        raise StateCorruptionError(
            f"현재 코드보다 새로운 state 스키마입니다: {schema_version} > "
            f"{STATE_SCHEMA_VERSION}"
        )

    # v1 → v2: 알림 outbox와 digest 전송일 추가
    if schema_version < 2:
        state.setdefault("pending_deliveries", [])
        state.setdefault("last_digest_enqueued_date", None)
        state.setdefault("last_digest_sent_date", None)
    # v2 → v3: 상세 본문 지문·완료 기록·AI 재시도 추가
    if schema_version < 3:
        state.setdefault("enriched_fingerprints", {})
        state.setdefault("delivery_history", {})
        state.setdefault("classification_retries", {})
        state.setdefault("last_detail_refresh_at", None)
    # v3 → v4: 자연어 프로필 내용은 저장하지 않고 변경 감지 해시만 추가
    if schema_version < 4:
        state.setdefault("profile_document_hash", None)
    # v4 → v5: 묶음이 달라져도 공지별 즉시 알림 중복을 막는 완료 기록 추가
    if schema_version < 5:
        state.setdefault("urgent_notice_history", {})

    state["schema_version"] = schema_version
    state.setdefault("seen_ids", {})
    state.setdefault("article_fingerprints", {})
    state.setdefault("enriched_fingerprints", {})
    state.setdefault("pending_digest", [])
    state.setdefault("pending_deliveries", [])
    state.setdefault("delivery_history", {})
    state.setdefault("urgent_notice_history", {})
    state.setdefault("classification_retries", {})
    state.setdefault("last_digest_enqueued_date", None)
    state.setdefault("last_digest_sent_date", None)
    state.setdefault("last_detail_refresh_at", None)
    state.setdefault("profile_document_hash", None)
    state.setdefault("last_run", None)
    if state["profile_document_hash"] is not None and not isinstance(
        state["profile_document_hash"],
        str,
    ):
        raise StateCorruptionError(
            "state.profile_document_hash의 형식이 잘못되었습니다: str 또는 null 필요"
        )
    expected_types = {
        "seen_ids": dict,
        "article_fingerprints": dict,
        "enriched_fingerprints": dict,
        "pending_digest": list,
        "pending_deliveries": list,
        "delivery_history": dict,
        "urgent_notice_history": dict,
        "classification_retries": dict,
    }
    for field, expected_type in expected_types.items():
        if not isinstance(state[field], expected_type):
            raise StateCorruptionError(
                f"state.{field}의 형식이 잘못되었습니다: {expected_type.__name__} 필요"
            )

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
    state["enriched_fingerprints"] = {
        _normalize_seen_key(str(k)): str(v)
        for k, v in state["enriched_fingerprints"].items()
        if isinstance(v, str)
    }
    state["pending_digest"] = [
        item for item in state["pending_digest"]
        if isinstance(item, dict) and isinstance(item.get("article"), dict)
    ]
    state["pending_deliveries"] = [
        item
        for item in state["pending_deliveries"]
        if (
            isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and isinstance(item.get("text"), str)
            and isinstance(item.get("kind"), str)
        )
    ]
    for item in state["pending_deliveries"]:
        item.setdefault("attempts", 0)
        item.setdefault("next_attempt_at", None)
        item.setdefault("last_error", None)
        item.setdefault("metadata", {})
    state["delivery_history"] = {
        str(k): str(v)
        for k, v in state["delivery_history"].items()
        if isinstance(v, str)
    }
    state["urgent_notice_history"] = {
        str(k): str(v)
        for k, v in state["urgent_notice_history"].items()
        if isinstance(v, str)
    }
    state["classification_retries"] = {
        _normalize_seen_key(str(k)): value
        for k, value in state["classification_retries"].items()
        if isinstance(value, dict)
    }
    state["schema_version"] = STATE_SCHEMA_VERSION

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
    state["enriched_fingerprints"] = {
        str(k): str(v)
        for k, v in state.get("enriched_fingerprints", {}).items()
        if k in state["seen_ids"] and isinstance(v, str)
    }
    state["delivery_history"] = {
        str(k): v
        for k, v in state.get("delivery_history", {}).items()
        if isinstance(v, str) and v > cutoff
    }
    state["urgent_notice_history"] = {
        str(k): v
        for k, v in state.get("urgent_notice_history", {}).items()
        if isinstance(v, str) and v > cutoff
    }
    retry_cutoff = (datetime.now() - timedelta(days=14)).isoformat()
    state["classification_retries"] = {
        str(k): v
        for k, v in state.get("classification_retries", {}).items()
        if isinstance(v, dict) and str(v.get("created_at", "")) > retry_cutoff
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


def filter_new_articles(
    articles: list[Article],
    state: dict,
    *,
    source_fingerprints: dict[str, str] | None = None,
    enriched_fingerprints: dict[str, str] | None = None,
) -> list[Article]:
    """신규 또는 내용이 바뀐 공지를 반환하고 피드 중복을 제거한다."""
    seen = state.get("seen_ids", {})
    fingerprints = state.setdefault("article_fingerprints", {})
    stored_enriched = state.setdefault("enriched_fingerprints", {})

    migrate_legacy_ids(articles, seen)

    result_by_key: dict[str, Article] = {}
    for article in articles:
        if article.key not in seen:
            result_by_key[article.key] = article
            continue
        current_source = (
            source_fingerprints.get(article.key, article.fingerprint)
            if source_fingerprints
            else article.fingerprint
        )
        previous = fingerprints.get(article.key)
        if previous and previous != current_source:
            article.is_update = True
            result_by_key[article.key] = article
            continue
        current_enriched = (
            enriched_fingerprints.get(article.key)
            if enriched_fingerprints
            else None
        )
        previous_enriched = stored_enriched.get(article.key)
        if current_enriched and previous_enriched and current_enriched != previous_enriched:
            article.is_update = True
            result_by_key[article.key] = article
    return list(result_by_key.values())


def mark_as_seen(
    articles: list[Article],
    state: dict,
    fingerprints: dict[str, str] | None = None,
    enriched_fingerprints: dict[str, str] | None = None,
) -> None:
    """공지 ID를 state에 기록"""
    now = datetime.now().isoformat()
    stored_fingerprints = state.setdefault("article_fingerprints", {})
    stored_enriched = state.setdefault("enriched_fingerprints", {})
    for a in articles:
        state["seen_ids"][a.key] = now
        stored_fingerprints[a.key] = (
            fingerprints.get(a.key, a.fingerprint) if fingerprints else a.fingerprint
        )
        if enriched_fingerprints and a.key in enriched_fingerprints:
            stored_enriched[a.key] = enriched_fingerprints[a.key]


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
    if len(pending) > MAX_PENDING_DIGEST:
        raise StateCapacityError(
            f"일일 요약 대기열이 안전 한도({MAX_PENDING_DIGEST})를 초과했습니다."
        )
    state["pending_digest"] = list(pending.values())


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


def enqueue_delivery(
    parts: list[str],
    state: dict,
    *,
    kind: str,
    dedup_key: str,
    metadata: dict | None = None,
) -> list[str]:
    """전송할 메시지 조각을 중복 없이 영구 outbox에 넣는다."""
    queue = state.setdefault("pending_deliveries", [])
    history = state.setdefault("delivery_history", {})
    existing_ids = {str(item.get("id")) for item in queue}
    new_items: list[dict] = []
    ids: list[str] = []
    created_at = datetime.now().isoformat()
    for index, text in enumerate(parts):
        raw_id = f"{kind}\0{dedup_key}\0{index}".encode("utf-8")
        delivery_id = hashlib.sha256(raw_id).hexdigest()
        legacy_raw_id = f"{kind}\0{dedup_key}\0{index}\0{text}".encode("utf-8")
        legacy_delivery_id = hashlib.sha256(legacy_raw_id).hexdigest()
        ids.append(delivery_id)
        if (
            delivery_id in existing_ids
            or delivery_id in history
            or legacy_delivery_id in existing_ids
            or legacy_delivery_id in history
        ):
            continue
        new_items.append(
            {
                "id": delivery_id,
                "kind": kind,
                "dedup_key": dedup_key,
                "part_index": index,
                "text": text,
                "created_at": created_at,
                "attempts": 0,
                "next_attempt_at": None,
                "last_error": None,
                "metadata": dict(metadata or {}),
            }
        )
    if len(queue) + len(new_items) > MAX_PENDING_DELIVERIES:
        raise StateCapacityError(
            f"알림 outbox가 안전 한도({MAX_PENDING_DELIVERIES})를 초과했습니다."
        )
    queue.extend(new_items)
    return ids


def due_deliveries(state: dict, now: datetime | None = None) -> list[dict]:
    """현재 재시도 가능한 outbox 항목을 반환한다."""
    current = now or datetime.now()
    due: list[dict] = []
    for item in state.get("pending_deliveries", []):
        retry_at = item.get("next_attempt_at")
        if not retry_at:
            due.append(item)
            continue
        try:
            if datetime.fromisoformat(retry_at) <= current:
                due.append(item)
        except (TypeError, ValueError):
            due.append(item)
    return due


def record_delivery_failure(
    state: dict,
    delivery_id: str,
    error: str,
    now: datetime | None = None,
) -> None:
    """실패 횟수와 지수 백오프 재시도 시각을 기록한다."""
    current = now or datetime.now()
    for item in state.get("pending_deliveries", []):
        if item.get("id") != delivery_id:
            continue
        attempts = int(item.get("attempts", 0)) + 1
        delay_minutes = min(5 * (2 ** (attempts - 1)), 360)
        item["attempts"] = attempts
        item["last_error"] = error[:500]
        item["next_attempt_at"] = (current + timedelta(minutes=delay_minutes)).isoformat()
        return


def complete_delivery(state: dict, delivery_id: str) -> dict | None:
    """성공한 outbox 항목 하나를 제거하고 해당 항목을 반환한다."""
    queue = state.get("pending_deliveries", [])
    for index, item in enumerate(queue):
        if item.get("id") == delivery_id:
            completed = queue.pop(index)
            state.setdefault("delivery_history", {})[delivery_id] = datetime.now().isoformat()
            return completed
    return None


def has_delivery_group(state: dict, group_id: str) -> bool:
    return any(
        item.get("metadata", {}).get("group_id") == group_id
        for item in state.get("pending_deliveries", [])
    )


def schedule_classification_retry(
    state: dict,
    article_key: str,
    now: datetime | None = None,
) -> None:
    """OpenAI 분석 실패 공지를 제한된 지수 백오프로 다시 시도하도록 기록한다."""
    current = now or datetime.now()
    retries = state.setdefault("classification_retries", {})
    previous = retries.get(article_key, {})
    attempts = int(previous.get("attempts", 0)) + 1
    delay_hours = min(2 ** (attempts - 1), 24)
    retries[article_key] = {
        "attempts": attempts,
        "created_at": previous.get("created_at") or current.isoformat(),
        "next_attempt_at": (current + timedelta(hours=delay_hours)).isoformat(),
    }


def due_classification_retry_keys(
    state: dict,
    now: datetime | None = None,
) -> set[str]:
    current = now or datetime.now()
    result: set[str] = set()
    for key, value in state.get("classification_retries", {}).items():
        try:
            if datetime.fromisoformat(value["next_attempt_at"]) <= current:
                result.add(key)
        except (KeyError, TypeError, ValueError):
            result.add(key)
    return result


def clear_classification_retry(state: dict, article_key: str) -> None:
    state.setdefault("classification_retries", {}).pop(article_key, None)
