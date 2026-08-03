"""건국대학교 공지 모니터링 파이프라인."""

import asyncio
import hashlib
import json
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import PROJECT_ROOT, load_config
from .feeds import (
    FeedBatch,
    check_ssl_health,
    enrich_articles_with_body,
    fetch_all_feeds_detailed,
    parse_pub_date,
)
from .matcher import match_articles
from .models import Article, ClassifiedNotice
from .notifier import (
    build_digest_message,
    build_first_run_message,
    build_no_new_message,
    build_no_relevant_message,
    build_urgent_messages,
    notify_error,
    send_telegram_part,
    split_message,
)
from .profile import profile_document_fingerprint, resolve_profile_snapshot
from .state import (
    clear_classification_retry,
    clear_pending_digest,
    complete_delivery,
    due_classification_retry_keys,
    due_deliveries,
    enqueue_delivery,
    enqueue_digest,
    filter_new_articles,
    get_pending_digest,
    has_delivery_group,
    load_state,
    mark_as_seen,
    record_delivery_failure,
    save_state,
    schedule_classification_retry,
)

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")


class NewArticleFloodError(RuntimeError):
    """비정상적으로 많은 공지가 신규로 감지되었을 때 발생한다."""


class FeedCollectionError(RuntimeError):
    """수집 성공률이 안전 기준보다 낮을 때 발생한다."""


def _validate_feed_health(feed_batch: FeedBatch, config: dict) -> float:
    enabled_count = len(feed_batch.statuses)
    success_ratio = (
        feed_batch.successful_count / enabled_count if enabled_count else 0.0
    )
    minimum_ratio = config["settings"].get("min_feed_success_ratio", 0.7)
    if enabled_count and (
        success_ratio < minimum_ratio
        or (feed_batch.successful_count > 0 and not feed_batch.articles)
    ):
        raise FeedCollectionError(
            f"피드 수집 상태가 안전 기준 미달입니다: "
            f"{feed_batch.successful_count}/{enabled_count} 성공, "
            f"공지 {len(feed_batch.articles)}건"
        )
    return success_ratio


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def _log_run_summary(stats: dict) -> None:
    logger.info(
        "실행 요약: 피드 %d개, 수집 %d건, 신규/수정 %d건, 즉시 %d건, 검토 %d건, 요약대기 %d건, 분석=%s",
        stats["feeds_collected"],
        stats["articles_found"],
        stats["new_articles"],
        stats["immediate_articles"],
        stats["review_articles"],
        stats["digest_queued"],
        stats["method"],
    )
    logger.info("run_summary_json=%s", json.dumps({"event": "run_summary", **stats}, ensure_ascii=False))


def _finalize_state(
    state: dict,
    state_path: str,
    all_articles: list[Article],
    stats: dict,
    source_fingerprints: dict[str, str],
    enriched_fingerprints: dict[str, str] | None = None,
) -> None:
    mark_as_seen(
        all_articles,
        state,
        fingerprints=source_fingerprints,
        enriched_fingerprints=enriched_fingerprints,
    )
    state["last_run_stats"] = stats
    save_state(state, state_path)


def _batch_key(prefix: str, values: list[str]) -> str:
    raw = "\0".join([prefix, *sorted(values)]).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _detail_refresh_is_due(
    state: dict,
    config: dict,
    now: datetime | None = None,
) -> bool:
    current = now or datetime.now(_KST)
    interval = config["settings"].get("detail_refresh_interval_hours", 6)
    last_raw = state.get("last_detail_refresh_at")
    if not last_raw:
        return True
    try:
        last = datetime.fromisoformat(last_raw)
    except (TypeError, ValueError):
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=_KST)
    return current - last >= timedelta(hours=interval)


def _select_detail_refresh_articles(
    articles: list[Article],
    state: dict,
    config: dict,
    now: datetime | None = None,
) -> list[Article]:
    """최근 공지와 고정 공지를 제한적으로 다시 읽어 본문 수정도 감지한다."""
    current = now or datetime.now(_KST)
    days = config["settings"].get("detail_refresh_days", 14)
    limit = config["settings"].get("detail_refresh_max_articles", 30)
    cutoff = current.replace(tzinfo=None) - timedelta(days=days)
    seen = state.get("seen_ids", {})
    candidates: list[tuple[datetime, Article]] = []
    for article in articles:
        if article.key not in seen:
            continue
        try:
            published = parse_pub_date(article.pub_date)
        except (TypeError, ValueError):
            published = datetime.min
        if article.is_pinned or published >= cutoff:
            candidates.append((published, article))
    candidates.sort(key=lambda item: (item[1].is_pinned, item[0]), reverse=True)
    return [article for _, article in candidates[:limit]]


def _queue_message(
    state: dict,
    text: str,
    *,
    kind: str,
    dedup_key: str,
    metadata: dict | None = None,
) -> int:
    before = len(state.setdefault("pending_deliveries", []))
    enqueue_delivery(
        split_message(text),
        state,
        kind=kind,
        dedup_key=dedup_key,
        metadata=metadata,
    )
    return len(state["pending_deliveries"]) - before


def _queue_urgent_notifications(
    state: dict,
    urgent: list[ClassifiedNotice],
    total_new: int,
    source_fingerprints: dict[str, str],
) -> int:
    """한 실행에서 나온 즉시·검토 공지를 하나의 읽기 쉬운 알림으로 묶는다."""
    if not urgent:
        return 0
    delivery_priority = {"immediate": 0, "review": 1}
    pending_notice_tokens = {
        str(token)
        for delivery in state.get("pending_deliveries", [])
        for token in delivery.get("metadata", {}).get("notice_tokens", [])
    }
    delivered_notice_tokens = state.setdefault("urgent_notice_history", {})

    candidates: list[tuple[ClassifiedNotice, str]] = []
    for item in urgent:
        token = _batch_key(
            "urgent-notice",
            [
                item.article.key,
                source_fingerprints.get(item.article.key, item.article.fingerprint),
                item.delivery,
            ],
        )
        if token in pending_notice_tokens or token in delivered_notice_tokens:
            continue
        candidates.append((item, token))

    ordered_candidates = sorted(
        candidates,
        key=lambda item: (
            delivery_priority.get(item[0].delivery, 2),
            item[0].article.key,
        ),
    )
    if not ordered_candidates:
        return 0

    ordered_urgent = [item for item, _ in ordered_candidates]
    notice_tokens = [token for _, token in ordered_candidates]
    urgent_key = _batch_key("urgent", notice_tokens)
    before = len(state.setdefault("pending_deliveries", []))
    enqueue_delivery(
        build_urgent_messages(ordered_urgent, total_new),
        state,
        kind="urgent",
        dedup_key=urgent_key,
        metadata={
            "group_id": f"urgent:{urgent_key}",
            "notice_count": len(ordered_urgent),
            "notice_tokens": notice_tokens,
        },
    )
    return len(state["pending_deliveries"]) - before


async def _flush_pending_deliveries(state: dict, state_path: str) -> dict[str, int]:
    """현재 전송 가능한 outbox를 처리하고 각 결과를 즉시 영구 저장한다."""
    result = {"sent_parts": 0, "failed_parts": 0, "digest_notices_sent": 0}
    blocked_groups: set[str] = set()
    due_ids = {str(item.get("id")) for item in due_deliveries(state)}

    for item in list(state.get("pending_deliveries", [])):
        metadata = item.get("metadata", {})
        group_id = str(metadata.get("group_id") or item["id"])
        if group_id in blocked_groups:
            continue
        if str(item.get("id")) not in due_ids:
            # 앞 조각의 재시도 시각 전에는 같은 메시지의 뒤 조각도 보내지 않는다.
            blocked_groups.add(group_id)
            continue
        try:
            await send_telegram_part(item["text"])
        except Exception as exc:
            blocked_groups.add(group_id)
            record_delivery_failure(state, item["id"], str(exc))
            save_state(state, state_path)
            result["failed_parts"] += 1
            logger.error(
                "outbox 전송 실패: kind=%s id=%s attempts=%s error=%s",
                item["kind"],
                item["id"][:12],
                int(item.get("attempts", 0)) + 1,
                exc,
            )
            continue

        completed = complete_delivery(state, item["id"])
        result["sent_parts"] += 1
        if completed and not has_delivery_group(state, group_id):
            completed_meta = completed.get("metadata", {})
            if completed["kind"] == "digest":
                digest_date = completed_meta.get("digest_date")
                if digest_date:
                    state["last_digest_sent_date"] = digest_date
                result["digest_notices_sent"] += int(
                    completed_meta.get("notice_count", 0)
                )
            elif completed["kind"] == "urgent":
                delivered_at = datetime.now().isoformat()
                history = state.setdefault("urgent_notice_history", {})
                for token in completed_meta.get("notice_tokens", []):
                    history[str(token)] = delivered_at
        save_state(state, state_path)

    return result


def _digest_is_due(
    config: dict,
    now: datetime | None = None,
    state: dict | None = None,
) -> bool:
    current = now or datetime.now(_KST)
    digest_hour = config["notifications"].get("digest_hour_kst", 21)
    if current.hour < digest_hour:
        return False
    return not state or state.get("last_digest_sent_date") != current.date().isoformat()


async def _flush_digest_if_due(
    state: dict,
    config: dict,
    now: datetime | None = None,
) -> int:
    """전송 시각이 지난 요약을 outbox로 원자적으로 이동한다."""
    current = now or datetime.now(_KST)
    digest_date = current.date().isoformat()
    if not _digest_is_due(config, current, state):
        return 0
    if state.get("last_digest_enqueued_date") == digest_date:
        return 0
    pending = get_pending_digest(state)
    if not pending:
        state["last_digest_sent_date"] = digest_date
        return 0
    group_id = f"digest:{digest_date}"
    _queue_message(
        state,
        build_digest_message(pending),
        kind="digest",
        dedup_key=group_id,
        metadata={
            "group_id": group_id,
            "digest_date": digest_date,
            "notice_count": len(pending),
        },
    )
    clear_pending_digest(state)
    state["last_digest_enqueued_date"] = digest_date
    return len(pending)


async def run() -> None:
    logger.info("=== 건국대 공지 모니터링 시작 ===")
    stats: dict[str, Any] = {
        "timestamp": datetime.now(_KST).isoformat(),
        "feeds_collected": 0,
        "feeds_failed": 0,
        "feed_failures": [],
        "articles_found": 0,
        "new_articles": 0,
        "updated_articles": 0,
        "matched_articles": 0,
        "immediate_articles": 0,
        "review_articles": 0,
        "digest_queued": 0,
        "digest_sent": 0,
        "outbox_queued_parts": 0,
        "outbox_sent_parts": 0,
        "outbox_failed_parts": 0,
        "classification_retry_count": 0,
        "suppressed_articles": 0,
        "analysis_metrics": {},
        "detail_refreshed": 0,
        "profile_changed": False,
        "profile_rechecked": 0,
        "profile_metrics": {},
        "method": "none",
        "timing": {},
    }

    typed_config = load_config()
    config = typed_config.model_dump()
    state_path = str(PROJECT_ROOT / config["settings"]["state_file"])
    first_run = not Path(state_path).exists()
    state = load_state(state_path)
    current_profile_hash = profile_document_fingerprint(config)
    previous_profile_hash = state.get("profile_document_hash")
    profile_changed = (
        isinstance(previous_profile_hash, str)
        and previous_profile_hash != current_profile_hash
    )
    stats["profile_changed"] = profile_changed

    retry_result = await _flush_pending_deliveries(state, state_path)
    stats["outbox_sent_parts"] += retry_result["sent_parts"]
    stats["outbox_failed_parts"] += retry_result["failed_parts"]
    stats["digest_sent"] += retry_result["digest_notices_sent"]

    if not config["settings"].get("ssl_verify", True):
        await check_ssl_health(config)

    started = time.monotonic()
    feed_batch = await fetch_all_feeds_detailed(config)
    all_articles = feed_batch.articles
    stats["timing"]["fetch_feeds"] = round(time.monotonic() - started, 2)
    stats["feeds_collected"] = feed_batch.successful_count
    stats["feeds_failed"] = feed_batch.failed_count
    stats["feed_failures"] = [
        {"name": status.name, "error": status.error}
        for status in feed_batch.statuses
        if not status.success
    ]
    stats["articles_found"] = len(all_articles)
    stats["feed_success_ratio"] = _validate_feed_health(feed_batch, config)
    logger.info("총 %d건 수집", len(all_articles))
    source_fingerprints = {article.key: article.fingerprint for article in all_articles}

    if first_run and config["settings"].get("seed_on_first_run", True) and all_articles:
        stats["method"] = "seed"
        stats["outbox_queued_parts"] += _queue_message(
            state,
            build_first_run_message(len(all_articles)),
            kind="first_run",
            dedup_key="first-run",
            metadata={"group_id": "first-run"},
        )
        state["profile_document_hash"] = current_profile_hash
        _finalize_state(state, state_path, all_articles, stats, source_fingerprints)
        delivery_result = await _flush_pending_deliveries(state, state_path)
        stats["outbox_sent_parts"] += delivery_result["sent_parts"]
        stats["outbox_failed_parts"] += delivery_result["failed_parts"]
        state["last_run_stats"] = stats
        save_state(state, state_path)
        _log_run_summary(stats)
        return

    preliminary_new = filter_new_articles(
        all_articles,
        state,
        source_fingerprints=source_fingerprints,
    )
    retry_keys = due_classification_retry_keys(state)
    retry_articles = [article for article in all_articles if article.key in retry_keys]
    refresh_due = _detail_refresh_is_due(state, config)
    refresh_articles = (
        _select_detail_refresh_articles(all_articles, state, config)
        if refresh_due
        else []
    )
    profile_recheck_articles = (
        _select_detail_refresh_articles(all_articles, state, config)
        if profile_changed
        else []
    )
    stats["profile_rechecked"] = len(profile_recheck_articles)
    enrichment_by_key = {
        article.key: article
        for article in [
            *preliminary_new,
            *retry_articles,
            *refresh_articles,
            *profile_recheck_articles,
        ]
    }
    enrichment_targets = list(enrichment_by_key.values())
    if enrichment_targets:
        started = time.monotonic()
        await enrich_articles_with_body(enrichment_targets, config)
        stats["timing"]["enrich_articles"] = round(time.monotonic() - started, 2)
    if refresh_due:
        state["last_detail_refresh_at"] = datetime.now(_KST).isoformat()
        stats["detail_refreshed"] = len(refresh_articles)
    enriched_fingerprints = {
        article.key: article.fingerprint for article in enrichment_targets
    }
    new_articles = filter_new_articles(
        all_articles,
        state,
        source_fingerprints=source_fingerprints,
        enriched_fingerprints=enriched_fingerprints,
    )
    stats["new_articles"] = len(new_articles)
    stats["updated_articles"] = sum(article.is_update for article in new_articles)
    max_new = config["settings"].get("max_new_articles_per_run", 60)
    if len(new_articles) > max_new:
        raise NewArticleFloodError(
            f"신규/수정 공지 {len(new_articles)}건이 안전 한도 {max_new}건을 "
            "초과했습니다. state와 피드 구조를 확인하세요."
        )

    classification_by_key = {
        article.key: article
        for article in [
            *new_articles,
            *retry_articles,
            *profile_recheck_articles,
        ]
    }
    classification_articles = list(classification_by_key.values())

    if classification_articles:
        profile_metrics: dict[str, Any] = {}
        profile_snapshot = await resolve_profile_snapshot(
            config,
            metrics=profile_metrics,
        )
        config["profile_snapshot"] = profile_snapshot.model_dump(mode="json")
        stats["profile_metrics"] = profile_metrics
        started = time.monotonic()
        match_result = await match_articles(classification_articles, config)
        matched, method = match_result
        stats["timing"]["analyze"] = round(time.monotonic() - started, 2)
        stats["method"] = method
        stats["matched_articles"] = len(matched)
        stats["suppressed_articles"] = match_result.suppressed_count
        stats["analysis_metrics"] = match_result.metrics
        for article in classification_articles:
            if article.key in match_result.failed_keys:
                schedule_classification_retry(state, article.key)
            else:
                clear_classification_retry(state, article.key)
        stats["classification_retry_count"] = len(
            state.get("classification_retries", {})
        )

        urgent = [
            item for item in matched if item.delivery in {"immediate", "review"}
        ]
        digest = [item for item in matched if item.delivery == "digest"]
        stats["immediate_articles"] = sum(
            item.delivery == "immediate" for item in urgent
        )
        stats["review_articles"] = sum(item.delivery == "review" for item in urgent)
        stats["digest_queued"] = len(digest)

        if urgent:
            stats["outbox_queued_parts"] += _queue_urgent_notifications(
                state,
                urgent,
                len(new_articles),
                source_fingerprints,
            )
        if digest:
            enqueue_digest(digest, state)
        if not matched and config["notifications"].get("notify_empty_runs", False):
            stats["outbox_queued_parts"] += _queue_message(
                state,
                build_no_relevant_message(len(new_articles)),
                kind="status",
                dedup_key=_batch_key("no-relevant", list(source_fingerprints.values())),
            )
    elif config["notifications"].get("notify_empty_runs", False):
        stats["outbox_queued_parts"] += _queue_message(
            state,
            build_no_new_message(),
            kind="status",
            dedup_key=f"no-new:{datetime.now(_KST).date().isoformat()}",
        )

    stats["digest_queued"] += await _flush_digest_if_due(state, config)
    state["profile_document_hash"] = current_profile_hash
    _finalize_state(
        state,
        state_path,
        all_articles,
        stats,
        source_fingerprints,
        enriched_fingerprints,
    )
    delivery_result = await _flush_pending_deliveries(state, state_path)
    stats["outbox_sent_parts"] += delivery_result["sent_parts"]
    stats["outbox_failed_parts"] += delivery_result["failed_parts"]
    stats["digest_sent"] += delivery_result["digest_notices_sent"]
    state["last_run_stats"] = stats
    save_state(state, state_path)
    _log_run_summary(stats)
    logger.info("=== 완료 ===")


def main() -> None:
    setup_logging()
    try:
        asyncio.run(run())
    except Exception as exc:
        logger.exception("모니터링 실행 중 치명적 오류 발생: %s", exc)
        asyncio.run(notify_error(str(exc)))
        sys.exit(1)


if __name__ == "__main__":
    main()
