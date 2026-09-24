"""건국대학교 공지 모니터링 파이프라인.

한 번의 실행은 다음 단계로 이루어진다.

1. 이전 실행에서 남은 알림 outbox를 먼저 전송한다.
2. RSS를 수집하고 신규·수정·재시도·재확인 대상 공지를 고른다.
3. 대상 공지의 상세 본문을 보강하고 AI/규칙으로 분류한다.
4. 즉시 알림과 일일 요약을 outbox에 넣고 상태를 저장한 뒤 전송한다.

모든 전송 결과는 즉시 상태 파일에 기록되므로, 실행이 중간에 실패해도 이미 보낸
알림이 다시 전송되지 않는다.
"""

import asyncio
import hashlib
import json
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

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
    TelegramDeliveryError,
    TelegramNotConfiguredError,
    build_digest_messages,
    build_first_run_message,
    build_new_boards_message,
    build_no_new_message,
    build_no_relevant_message,
    build_source_outage_message,
    build_source_recovered_message,
    build_urgent_messages,
    notify_error,
    send_telegram_part,
    split_message,
)
from .profile import (
    ProfileResolutionError,
    profile_document_fingerprint,
    resolve_profile_snapshot,
)
from .state import (
    MAX_DELIVERY_ATTEMPTS,
    clear_classification_retry,
    clear_pending_digest,
    complete_delivery,
    drop_cross_board_duplicates,
    drop_delivery,
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
    seed_new_boards,
)
from .util import KST, now_kst

logger = logging.getLogger(__name__)


class NewArticleFloodError(RuntimeError):
    """비정상적으로 많은 공지가 신규로 감지되었을 때 발생한다."""


class FeedCollectionError(RuntimeError):
    """수집 성공률이 안전 기준보다 낮을 때 발생한다."""


class SourceOutageError(FeedCollectionError):
    """학교 서버가 모든 게시판에서 응답하지 않을 때 발생한다.

    모니터 결함이 아니라 외부 장애이므로 실행을 실패로 처리하지 않고, 장애가
    이어질 때만 사용자에게 한 번 알린다.
    """

    def __init__(self, message: str, detail: str) -> None:
        super().__init__(message)
        self.detail = detail


# 워크플로가 같은 오류를 두 번 알리지 않도록, 파이썬이 오류 알림을 보냈음을 표시한다.
ERROR_NOTIFIED_MARKER = ".error-notified"


class DeliveryConfigurationError(RuntimeError):
    """텔레그램 토큰·채팅 설정 문제로 알림을 보낼 수 없을 때 발생한다.

    메시지는 outbox에 보존되지만 사용자가 알 수 있도록 실행을 실패로 표시한다.
    """


def _new_stats() -> dict[str, Any]:
    return {
        "timestamp": now_kst().isoformat(),
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
        "outbox_dropped_parts": 0,
        "classification_retry_count": 0,
        "suppressed_articles": 0,
        "analysis_metrics": {},
        "detail_refreshed": 0,
        "enrichment_failed": 0,
        "profile_changed": False,
        "profile_rechecked": 0,
        "profile_metrics": {},
        "method": "none",
        "timing": {},
    }


def _validate_feed_health(feed_batch: FeedBatch, config: dict) -> float:
    enabled_count = len(feed_batch.statuses)
    success_ratio = (
        feed_batch.successful_count / enabled_count if enabled_count else 0.0
    )
    minimum_ratio = config["settings"].get("min_feed_success_ratio", 0.7)
    if enabled_count and feed_batch.is_source_outage:
        detail = feed_batch.failure_summary()
        raise SourceOutageError(
            f"학교 서버가 응답하지 않아 게시판 {enabled_count}개를 모두 읽지 못했습니다 ({detail})",
            detail,
        )
    if enabled_count and (
        success_ratio < minimum_ratio
        or (feed_batch.successful_count > 0 and not feed_batch.articles)
    ):
        failures = feed_batch.failure_summary()
        raise FeedCollectionError(
            f"게시판 {enabled_count}개 중 {feed_batch.successful_count}개만 읽어 "
            f"안전 기준({minimum_ratio:.0%})에 못 미칩니다. "
            f"수집 공지 {len(feed_batch.articles)}건"
            + (f", 실패 원인: {failures}" if failures else "")
        )
    return success_ratio


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    # OpenAI SDK가 쓰는 httpx의 요청별 INFO 로그는 실행 요약을 가리는 소음이다.
    logging.getLogger("httpx").setLevel(logging.WARNING)


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


def _batch_key(prefix: str, values: list[str]) -> str:
    raw = "\0".join([prefix, *sorted(values)]).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _unique_by_key(*groups: list[Article]) -> list[Article]:
    return list({article.key: article for group in groups for article in group}.values())


# ---------------------------------------------------------------------------
# 대상 공지 선택
# ---------------------------------------------------------------------------


def _detail_refresh_is_due(
    state: dict,
    config: dict,
    now: datetime | None = None,
) -> bool:
    current = now or now_kst()
    interval = config["settings"].get("detail_refresh_interval_hours", 6)
    last_raw = state.get("last_detail_refresh_at")
    if not last_raw:
        return True
    try:
        last = datetime.fromisoformat(last_raw)
    except (TypeError, ValueError):
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=KST)
    return current - last >= timedelta(hours=interval)


def _select_detail_refresh_articles(
    articles: list[Article],
    state: dict,
    config: dict,
    now: datetime | None = None,
) -> list[Article]:
    """최근 공지와 고정 공지를 제한적으로 다시 읽어 본문 수정도 감지한다."""
    current = now or now_kst()
    days = config["settings"].get("detail_refresh_days", 14)
    limit = config["settings"].get("detail_refresh_max_articles", 30)
    # RSS 게시일은 시간대 없는 KST 문자열이다.
    cutoff = current.astimezone(KST).replace(tzinfo=None) - timedelta(days=days)
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


@dataclass(frozen=True)
class _Targets:
    """이번 실행에서 상세 본문을 읽을 공지 묶음."""

    candidates: list[Article]
    retries: list[Article]
    refreshes: list[Article]
    profile_rechecks: list[Article]

    @property
    def enrichment(self) -> list[Article]:
        return _unique_by_key(
            self.candidates, self.retries, self.refreshes, self.profile_rechecks
        )


def _select_targets(
    all_articles: list[Article],
    state: dict,
    config: dict,
    source_fingerprints: dict[str, str],
    *,
    refresh_due: bool,
    profile_changed: bool,
) -> _Targets:
    retry_keys = due_classification_retry_keys(state)
    recent = (
        _select_detail_refresh_articles(all_articles, state, config)
        if refresh_due or profile_changed
        else []
    )
    return _Targets(
        candidates=filter_new_articles(
            all_articles,
            state,
            source_fingerprints=source_fingerprints,
        ),
        retries=[article for article in all_articles if article.key in retry_keys],
        refreshes=recent if refresh_due else [],
        profile_rechecks=recent if profile_changed else [],
    )


# ---------------------------------------------------------------------------
# outbox
# ---------------------------------------------------------------------------


def _queue_parts(
    state: dict,
    parts: list[str],
    *,
    kind: str,
    dedup_key: str,
    metadata: dict | None = None,
) -> int:
    before = len(state.setdefault("pending_deliveries", []))
    enqueue_delivery(parts, state, kind=kind, dedup_key=dedup_key, metadata=metadata)
    return len(state["pending_deliveries"]) - before


def _queue_message(
    state: dict,
    text: str,
    *,
    kind: str,
    dedup_key: str,
    metadata: dict | None = None,
) -> int:
    return _queue_parts(
        state,
        split_message(text),
        kind=kind,
        dedup_key=dedup_key,
        metadata=metadata,
    )


def _queue_urgent_notifications(
    state: dict,
    urgent: list[ClassifiedNotice],
    source_fingerprints: dict[str, str],
) -> int:
    """즉시·검토 공지를 공지별 메시지로 나누어 중복 없이 큐에 넣는다."""
    pending_notice_tokens = {
        str(token)
        for delivery in state.get("pending_deliveries", [])
        for token in delivery.get("metadata", {}).get("notice_tokens", [])
    }
    delivered_notice_tokens = state.setdefault("urgent_notice_history", {})
    delivery_priority = {"immediate": 0, "review": 1}

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
        if token not in pending_notice_tokens and token not in delivered_notice_tokens:
            candidates.append((item, token))
    candidates.sort(
        key=lambda pair: (delivery_priority.get(pair[0].delivery, 2), pair[0].article.key)
    )

    queued_parts = 0
    for item, notice_token in candidates:
        urgent_key = _batch_key("urgent", [notice_token])
        queued_parts += _queue_parts(
            state,
            build_urgent_messages([item]),
            kind="urgent",
            dedup_key=urgent_key,
            metadata={
                "group_id": f"urgent:{urgent_key}",
                "notice_count": 1,
                "notice_tokens": [notice_token],
            },
        )
    return queued_parts


def _record_group_completion(state: dict, completed: dict, result: dict) -> None:
    """메시지 묶음의 마지막 조각이 처리되면 중복 방지 기록을 남긴다."""
    metadata = completed.get("metadata", {})
    if completed["kind"] == "digest":
        if digest_date := metadata.get("digest_date"):
            state["last_digest_sent_date"] = digest_date
        result["digest_notices_sent"] += int(metadata.get("notice_count", 0))
    elif completed["kind"] == "urgent":
        delivered_at = datetime.now().isoformat()
        history = state.setdefault("urgent_notice_history", {})
        for token in metadata.get("notice_tokens", []):
            history[str(token)] = delivered_at


async def _flush_pending_deliveries(state: dict, state_path: str) -> dict[str, Any]:
    """현재 전송 가능한 outbox를 처리하고 각 결과를 즉시 영구 저장한다."""
    result: dict[str, Any] = {
        "sent_parts": 0,
        "failed_parts": 0,
        "dropped_parts": 0,
        "digest_notices_sent": 0,
        "configuration_error": None,
    }
    blocked_groups: set[str] = set()
    due_ids = {str(item.get("id")) for item in due_deliveries(state)}

    for item in list(state.get("pending_deliveries", [])):
        group_id = str(item.get("metadata", {}).get("group_id") or item["id"])
        if group_id in blocked_groups:
            continue
        if str(item.get("id")) not in due_ids:
            # 앞 조각의 재시도 시각 전에는 같은 메시지의 뒤 조각도 보내지 않는다.
            blocked_groups.add(group_id)
            continue
        try:
            await send_telegram_part(item["text"])
        except Exception as exc:
            if isinstance(exc, TelegramNotConfiguredError):
                logger.warning("%s", exc)
                break
            if isinstance(exc, TelegramDeliveryError) and exc.configuration:
                # 설정 문제는 메시지 탓이 아니므로 시도 횟수를 늘리지 않고 전송을 멈춘다.
                result["configuration_error"] = str(exc)
                logger.error("텔레그램 설정 문제로 outbox 전송을 중단합니다: %s", exc)
                break
            permanent = isinstance(exc, TelegramDeliveryError) and exc.permanent
            retry_after = exc.retry_after if isinstance(exc, TelegramDeliveryError) else None
            attempts = record_delivery_failure(
                state,
                item["id"],
                str(exc),
                retry_after_seconds=retry_after,
            )
            if permanent or attempts >= MAX_DELIVERY_ATTEMPTS:
                dropped = drop_delivery(state, item["id"])
                result["dropped_parts"] += 1
                logger.error(
                    "outbox 항목을 포기합니다: kind=%s id=%s attempts=%s error=%s",
                    item["kind"],
                    item["id"][:12],
                    attempts,
                    exc,
                )
                if dropped and not has_delivery_group(state, group_id):
                    _record_group_completion(state, dropped, result)
            else:
                blocked_groups.add(group_id)
                result["failed_parts"] += 1
                logger.error(
                    "outbox 전송 실패: kind=%s id=%s attempts=%s error=%s",
                    item["kind"],
                    item["id"][:12],
                    attempts,
                    exc,
                )
            save_state(state, state_path)
            continue

        completed = complete_delivery(state, item["id"])
        result["sent_parts"] += 1
        if completed and not has_delivery_group(state, group_id):
            _record_group_completion(state, completed, result)
        save_state(state, state_path)

    return result


def _add_delivery_stats(stats: dict, result: dict) -> None:
    stats["outbox_sent_parts"] += result["sent_parts"]
    stats["outbox_failed_parts"] += result["failed_parts"]
    stats["outbox_dropped_parts"] += result.get("dropped_parts", 0)
    stats["digest_sent"] += result["digest_notices_sent"]
    if result.get("configuration_error"):
        stats["delivery_configuration_error"] = result["configuration_error"]


def _digest_is_due(
    config: dict,
    now: datetime | None = None,
    state: dict | None = None,
) -> bool:
    current = now or now_kst()
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
    current = now or now_kst()
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
    _queue_parts(
        state,
        build_digest_messages(pending),
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


# ---------------------------------------------------------------------------
# 분류
# ---------------------------------------------------------------------------


async def _classify_and_queue(
    state: dict,
    config: dict,
    articles: list[Article],
    *,
    total_new: int,
    source_fingerprints: dict[str, str],
    stats: dict,
) -> None:
    profile_metrics: dict[str, Any] = {}
    use_ai = True
    try:
        snapshot = await resolve_profile_snapshot(config, metrics=profile_metrics)
        config["profile_snapshot"] = snapshot.model_dump(mode="json")
    except ProfileResolutionError as exc:
        # 개인화 기준 없이 AI 판정을 확정하면 잘못 숨긴 공지를 되돌릴 수 없다.
        # 이번에는 보수적 규칙으로만 판정하고 모든 공지를 재분석 대상으로 남긴다.
        logger.error("개인화 프로필을 확보하지 못해 규칙 판정 후 재분석을 예약합니다: %s", exc)
        profile_metrics["error"] = str(exc)[:300]
        use_ai = False
    stats["profile_metrics"] = profile_metrics

    started = time.monotonic()
    result = await match_articles(articles, config, use_ai=use_ai)
    stats["timing"]["analyze"] = round(time.monotonic() - started, 2)
    stats["method"] = result.method
    stats["matched_articles"] = len(result.notices)
    stats["suppressed_articles"] = result.suppressed_count
    stats["analysis_metrics"] = result.metrics

    for article in articles:
        if article.key in result.failed_keys:
            schedule_classification_retry(state, article.key)
        else:
            clear_classification_retry(state, article.key)
    stats["classification_retry_count"] = len(state.get("classification_retries", {}))

    urgent = [item for item in result.notices if item.delivery in {"immediate", "review"}]
    digest = [item for item in result.notices if item.delivery == "digest"]
    stats["immediate_articles"] = sum(item.delivery == "immediate" for item in urgent)
    stats["review_articles"] = sum(item.delivery == "review" for item in urgent)
    stats["digest_queued"] = len(digest)

    stats["outbox_queued_parts"] += _queue_urgent_notifications(
        state,
        urgent,
        source_fingerprints,
    )
    if digest:
        enqueue_digest(digest, state)
    if not result.notices and config["notifications"].get("notify_empty_runs", False):
        stats["outbox_queued_parts"] += _queue_message(
            state,
            build_no_relevant_message(total_new),
            kind="status",
            dedup_key=_batch_key("no-relevant", list(source_fingerprints.values())),
        )


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------


async def _collect_feeds(config: dict, stats: dict) -> FeedBatch:
    started = time.monotonic()
    feed_batch = await fetch_all_feeds_detailed(config)
    retry_delay = config["settings"].get("source_outage_retry_seconds", 90)
    if feed_batch.is_source_outage and retry_delay > 0:
        # 학교 서버는 1~2분짜리 순간 장애가 잦다. 곧바로 실패하지 않고 한 번 더 본다.
        logger.warning(
            "모든 피드가 서버 무응답으로 실패했습니다 (%s). %d초 뒤 다시 수집합니다.",
            feed_batch.failure_summary(),
            retry_delay,
        )
        await asyncio.sleep(retry_delay)
        feed_batch = await fetch_all_feeds_detailed(config)
        stats["source_outage_retried"] = True
    stats["timing"]["fetch_feeds"] = round(time.monotonic() - started, 2)
    stats["feeds_collected"] = feed_batch.successful_count
    stats["feeds_failed"] = feed_batch.failed_count
    stats["feed_failures"] = [
        {"name": status.name, "error": status.error}
        for status in feed_batch.statuses
        if not status.success
    ]
    stats["articles_found"] = len(feed_batch.articles)
    stats["feed_success_ratio"] = _validate_feed_health(feed_batch, config)
    logger.info("총 %d건 수집", len(feed_batch.articles))
    return feed_batch


def _record_source_outage(state: dict, config: dict, exc: SourceOutageError) -> bool:
    """연속 장애 횟수를 기록하고, 이번에 안내를 보내야 하면 outbox에 넣는다."""
    outage = state.get("source_outage")
    if not isinstance(outage, dict):
        outage = {"since": now_kst().isoformat(), "consecutive_runs": 0, "alerted": False}
    outage["consecutive_runs"] = int(outage.get("consecutive_runs", 0)) + 1
    outage["last_error"] = exc.detail
    state["source_outage"] = outage
    threshold = config["settings"].get("source_outage_alert_after_runs", 2)
    if outage.get("alerted") or outage["consecutive_runs"] < threshold:
        return False
    _queue_message(
        state,
        build_source_outage_message(
            consecutive_runs=outage["consecutive_runs"],
            since=str(outage.get("since", "")),
            detail=exc.detail,
        ),
        kind="status",
        dedup_key=f"source-outage:{outage.get('since')}",
    )
    outage["alerted"] = True
    return True


def _clear_source_outage(state: dict) -> int:
    """장애가 끝났음을 기록하고, 장애 안내를 보냈다면 복구 안내를 큐에 넣는다."""
    outage = state.pop("source_outage", None)
    if not isinstance(outage, dict) or not outage.get("alerted"):
        return 0
    return _queue_message(
        state,
        build_source_recovered_message(int(outage.get("consecutive_runs", 0))),
        kind="status",
        dedup_key=f"source-recovered:{outage.get('since')}",
    )


async def _handle_source_outage(
    state: dict,
    state_path: str,
    config: dict,
    stats: dict,
    exc: SourceOutageError,
) -> None:
    """학교 서버 장애는 실패로 끝내지 않고 상태만 남긴 뒤 다음 실행을 기다린다."""
    alerted = _record_source_outage(state, config, exc)
    outage = state["source_outage"]
    stats["method"] = "source_outage"
    stats["source_outage_runs"] = outage["consecutive_runs"]
    logger.warning(
        "학교 서버 장애로 이번 확인을 건너뜁니다 (연속 %d회, 안내 %s): %s",
        outage["consecutive_runs"],
        "전송" if alerted else ("이미 보냄" if outage.get("alerted") else "보류"),
        exc,
    )
    state["last_run_stats"] = stats
    save_state(state, state_path)
    _add_delivery_stats(stats, await _flush_pending_deliveries(state, state_path))
    state["last_run_stats"] = stats
    save_state(state, state_path)
    _log_run_summary(stats)


async def _finish(
    state: dict,
    state_path: str,
    stats: dict,
    all_articles: list[Article],
    source_fingerprints: dict[str, str],
    enriched_fingerprints: dict[str, str] | None = None,
) -> None:
    """공지를 확인 처리해 저장한 뒤 outbox를 전송한다."""
    mark_as_seen(
        all_articles,
        state,
        fingerprints=source_fingerprints,
        enriched_fingerprints=enriched_fingerprints,
    )
    state["last_run_stats"] = stats
    save_state(state, state_path)
    _add_delivery_stats(stats, await _flush_pending_deliveries(state, state_path))
    state["last_run_stats"] = stats
    save_state(state, state_path)
    _log_run_summary(stats)


async def run() -> None:
    logger.info("=== 건국대 공지 모니터링 시작 ===")
    stats = _new_stats()
    config = load_config().model_dump()
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

    _add_delivery_stats(stats, await _flush_pending_deliveries(state, state_path))

    if not config["settings"].get("ssl_verify", True):
        await check_ssl_health(config)

    try:
        feed_batch = await _collect_feeds(config, stats)
    except SourceOutageError as exc:
        if first_run:
            # 첫 실행에 상태 파일을 만들면 다음 실행이 기존 공지를 모두 신규로 오인한다.
            raise
        await _handle_source_outage(state, state_path, config, stats, exc)
        _raise_for_delivery_configuration(stats)
        return
    stats["outbox_queued_parts"] += _clear_source_outage(state)
    all_articles = feed_batch.articles
    source_fingerprints = {article.key: article.fingerprint for article in all_articles}
    successful_boards = {status.board_id for status in feed_batch.statuses if status.success}

    if first_run and config["settings"].get("seed_on_first_run", True) and all_articles:
        state["known_boards"] = sorted(successful_boards)
        stats["method"] = "seed"
        stats["outbox_queued_parts"] += _queue_message(
            state,
            build_first_run_message(len(all_articles)),
            kind="first_run",
            dedup_key="first-run",
            metadata={"group_id": "first-run"},
        )
        state["profile_document_hash"] = current_profile_hash
        await _finish(state, state_path, stats, all_articles, source_fingerprints)
        _raise_for_delivery_configuration(stats)
        return

    seeded_boards = seed_new_boards(
        all_articles, successful_boards, state, fingerprints=source_fingerprints
    )
    if seeded_boards:
        names = {status.board_id: status.name for status in feed_batch.statuses}
        seeded_by_name = {names[board]: count for board, count in sorted(seeded_boards.items())}
        stats["seeded_boards"] = seeded_by_name
        logger.info("새 게시판의 기존 공지를 알림 없이 확인 처리했습니다: %s", seeded_by_name)
        stats["outbox_queued_parts"] += _queue_message(
            state,
            build_new_boards_message(seeded_by_name),
            kind="status",
            dedup_key=_batch_key("new-boards", [str(board) for board in seeded_boards]),
        )

    refresh_due = _detail_refresh_is_due(state, config)
    targets = _select_targets(
        all_articles,
        state,
        config,
        source_fingerprints,
        refresh_due=refresh_due,
        profile_changed=profile_changed,
    )
    stats["profile_rechecked"] = len(targets.profile_rechecks)

    enrichment_targets = targets.enrichment
    enriched_keys: set[str] = set()
    if enrichment_targets:
        started = time.monotonic()
        enriched_keys = await enrich_articles_with_body(enrichment_targets, config)
        stats["timing"]["enrich_articles"] = round(time.monotonic() - started, 2)
        stats["enrichment_failed"] = len(enrichment_targets) - len(enriched_keys)
    if refresh_due:
        state["last_detail_refresh_at"] = now_kst().isoformat()
        stats["detail_refreshed"] = len(targets.refreshes)
    # 크롤링에 실패한 공지의 지문은 RSS 요약 기준이라 이전 상세 지문과 비교하면 안 된다.
    enriched_fingerprints = {
        article.key: article.fingerprint
        for article in enrichment_targets
        if article.key in enriched_keys
    }
    new_articles = filter_new_articles(
        all_articles,
        state,
        source_fingerprints=source_fingerprints,
        enriched_fingerprints=enriched_fingerprints,
    )
    unique_articles = drop_cross_board_duplicates(
        new_articles, state, current_articles=all_articles
    )
    stats["duplicate_articles"] = len(new_articles) - len(unique_articles)
    new_articles = unique_articles
    stats["new_articles"] = len(new_articles)
    stats["updated_articles"] = sum(article.is_update for article in new_articles)
    max_new = config["settings"].get("max_new_articles_per_run", 60)
    if len(new_articles) > max_new:
        raise NewArticleFloodError(
            f"신규/수정 공지 {len(new_articles)}건이 안전 한도 {max_new}건을 "
            "초과했습니다. state와 피드 구조를 확인하세요."
        )

    classification_articles = _unique_by_key(
        new_articles, targets.retries, targets.profile_rechecks
    )
    if classification_articles:
        await _classify_and_queue(
            state,
            config,
            classification_articles,
            total_new=len(new_articles),
            source_fingerprints=source_fingerprints,
            stats=stats,
        )
    elif config["notifications"].get("notify_empty_runs", False):
        stats["outbox_queued_parts"] += _queue_message(
            state,
            build_no_new_message(),
            kind="status",
            dedup_key=f"no-new:{now_kst().date().isoformat()}",
        )

    stats["digest_queued"] += await _flush_digest_if_due(state, config)
    state["profile_document_hash"] = current_profile_hash
    await _finish(
        state,
        state_path,
        stats,
        all_articles,
        source_fingerprints,
        enriched_fingerprints,
    )
    _raise_for_delivery_configuration(stats)
    logger.info("=== 완료 ===")


def _raise_for_delivery_configuration(stats: dict) -> None:
    if error := stats.get("delivery_configuration_error"):
        raise DeliveryConfigurationError(
            f"알림을 보낼 수 없습니다. 메시지는 보존되었습니다: {error}"
        )


def _error_notice(exc: Exception) -> tuple[str, str | None]:
    """오류 종류별로 사용자가 이해할 수 있는 제목과 안내를 고른다."""
    if isinstance(exc, FeedCollectionError):
        return (
            "일부 게시판을 읽지 못해 이번 확인을 건너뛰었습니다",
            "누락을 막기 위해 공지를 확인 처리하지 않았습니다. 다음 실행에서 다시 확인합니다.",
        )
    if isinstance(exc, NewArticleFloodError):
        return (
            "새 공지가 비정상적으로 많아 알림을 멈췄습니다",
            "대량 알림을 막기 위한 안전장치입니다. 상태 파일과 게시판 구조를 확인해 주세요.",
        )
    if isinstance(exc, DeliveryConfigurationError):
        return (
            "텔레그램 설정 문제로 알림을 보내지 못했습니다",
            "보내지 못한 알림은 보관되어 있으며, 설정을 고치면 다음 실행에서 전송됩니다.",
        )
    return ("공지 확인 중 오류가 발생했습니다", None)


def _mark_error_notified() -> None:
    try:
        (PROJECT_ROOT / ERROR_NOTIFIED_MARKER).write_text(now_kst().isoformat(), encoding="utf-8")
    except OSError as exc:
        logger.warning("오류 알림 표시 파일을 쓰지 못했습니다: %s", exc)


def main() -> None:
    setup_logging()
    try:
        asyncio.run(run())
    except Exception as exc:
        logger.exception("모니터링 실행 중 치명적 오류 발생: %s", exc)
        title, guidance = _error_notice(exc)
        if asyncio.run(notify_error(str(exc), title=title, guidance=guidance)):
            _mark_error_notified()
        sys.exit(1)


if __name__ == "__main__":
    main()
