"""모델이 추출한 사실을 알림 결정으로 바꾸는 결정론적 정책 엔진."""

from datetime import date, datetime
from enum import StrEnum
from typing import Literal
from zoneinfo import ZoneInfo

from .analysis_models import (
    AudienceFit,
    Consequence,
    DateKind,
    InterestFit,
    NoticeAssessment,
    Obligation,
)
from .models import Article, ClassifiedNotice


class Delivery(StrEnum):
    IMMEDIATE = "immediate"
    DIGEST = "digest"
    REVIEW = "review"
    SUPPRESS = "suppress"


_HIGH_IMPACT = {
    Consequence.ACADEMIC_RISK,
    Consequence.FINANCIAL_LOSS,
    Consequence.ADMINISTRATIVE_BLOCK,
}
_DEADLINE_KINDS = {
    DateKind.APPLICATION_DEADLINE,
    DateKind.DOCUMENT_DEADLINE,
    DateKind.PAYMENT_DEADLINE,
}
_KST = ZoneInfo("Asia/Seoul")


def _nearest_deadline(assessment: NoticeAssessment) -> str | None:
    candidates = [
        notice_date.date
        for notice_date in assessment.dates
        if notice_date.kind in _DEADLINE_KINDS
    ]
    candidates.extend(action.deadline for action in assessment.actions if action.deadline)
    return min(candidates) if candidates else None


def _days_until(value: str | None, today: date) -> int | None:
    return (date.fromisoformat(value) - today).days if value else None


def decide_delivery(
    assessment: NoticeAssessment,
    *,
    today: date | None = None,
    action_window_days: int = 21,
) -> tuple[Delivery, str]:
    """축별 판정을 비대칭 손실 정책으로 결합한다.

    중요한 공지를 숨기는 비용이 불필요한 알림 한 건보다 크므로, 고위험·불명확
    조합은 suppress 대신 review로 보낸다.
    """
    current_date = today or datetime.now(_KST).date()
    high_impact = assessment.consequence in _HIGH_IMPACT
    deadline = _nearest_deadline(assessment)
    days_left = _days_until(deadline, current_date)
    active_deadline = days_left is not None and days_left >= 0
    deadline_close = (
        days_left is not None and active_deadline and days_left <= action_window_days
    )
    has_required_action = (
        assessment.obligation == Obligation.REQUIRED
        or any(action.required for action in assessment.actions)
    )

    if assessment.audience_fit == AudienceFit.INELIGIBLE:
        return Delivery.SUPPRESS, f"명시된 대상 조건과 불일치: {assessment.audience_reason}"

    if assessment.audience_fit == AudienceFit.UNKNOWN and (
        high_impact or has_required_action
    ):
        return Delivery.REVIEW, "대상 여부가 불명확하지만 놓쳤을 때 손실 가능성이 큼"

    if assessment.uncertainties and high_impact:
        return Delivery.REVIEW, "고위험 공지의 핵심 조건이 불명확하여 직접 확인 필요"

    if high_impact and assessment.audience_fit in {
        AudienceFit.ELIGIBLE,
        AudienceFit.POSSIBLY_ELIGIBLE,
    }:
        return Delivery.IMMEDIATE, "학사·금전·행정상 직접 손실 가능성"

    if has_required_action and assessment.audience_fit == AudienceFit.ELIGIBLE:
        if deadline_close:
            return Delivery.IMMEDIATE, f"필수 행동 마감까지 {days_left}일"
        if deadline is None:
            return Delivery.REVIEW, "필수 행동이 있으나 마감일이 확인되지 않음"
        if days_left is not None and days_left < 0:
            return Delivery.REVIEW, "필수 행동의 명시된 마감이 지났으므로 연장·변경 여부 확인 필요"
        if active_deadline:
            return Delivery.DIGEST, "필수 행동이 있으나 마감까지 여유가 있음"

    if deadline_close and assessment.audience_fit != AudienceFit.UNKNOWN:
        return Delivery.IMMEDIATE, f"관련 행동 마감까지 {days_left}일"

    if assessment.interest_fit in {InterestFit.HIGH, InterestFit.MEDIUM}:
        return Delivery.DIGEST, f"관심사 일치: {assessment.interest_reason}"

    if (
        assessment.consequence == Consequence.MISSED_OPPORTUNITY
        and assessment.audience_fit in {
            AudienceFit.ELIGIBLE,
            AudienceFit.POSSIBLY_ELIGIBLE,
        }
    ):
        return Delivery.DIGEST, "지원 가능한 선택적 기회"

    return Delivery.SUPPRESS, "직접 의무·손실·관심사 일치가 없음"


def classify_assessment(
    article: Article,
    assessment: NoticeAssessment,
    *,
    source: Literal["openai", "rules", "legacy"],
    today: date | None = None,
    action_window_days: int = 21,
) -> ClassifiedNotice:
    delivery, reason = decide_delivery(
        assessment,
        today=today,
        action_window_days=action_window_days,
    )
    deadline = _nearest_deadline(assessment)
    return ClassifiedNotice(
        article=article,
        delivery=delivery.value,
        category=assessment.category.value,
        summary=assessment.summary,
        reason=reason,
        audience_fit=assessment.audience_fit.value,
        interest_fit=assessment.interest_fit.value,
        obligation=assessment.obligation.value,
        consequence=assessment.consequence.value,
        deadline=deadline,
        dates=[item.model_dump(mode="json") for item in assessment.dates],
        actions=[item.label for item in assessment.actions],
        benefits=assessment.benefits,
        evidence=assessment.evidence,
        uncertainties=assessment.uncertainties,
        source=source,
    )
