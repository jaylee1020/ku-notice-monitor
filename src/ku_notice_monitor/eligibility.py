"""공지의 구조화 자격 경로와 사용자 프로필을 결정론적으로 비교한다."""

import re
from dataclasses import dataclass
from enum import StrEnum

from .analysis_models import (
    AudienceFit,
    EligibilityCondition,
    EligibilityMatch,
    EligibilityOperator,
    NoticeAssessment,
)
from .profile_models import (
    FactCertainty,
    ProfileFact,
    ProfileFactKey,
    ProfileSnapshot,
)


class ConditionMatch(StrEnum):
    MATCH = "match"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class EligibilityResolution:
    audience_fit: AudienceFit
    match: EligibilityMatch
    reason: str


_LOCATION_KEYS = {
    ProfileFactKey.CURRENT_RESIDENCE,
    ProfileFactKey.REGISTERED_RESIDENCE,
    ProfileFactKey.FAMILY_REGISTERED_RESIDENCE,
}
_REGION_SUFFIXES = (
    "특별자치도",
    "특별자치시",
    "특별시",
    "광역시",
    "자치도",
    "도",
    "시",
)


def _normalize(value: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]+", "", value.lower())


def _normalize_location(value: str) -> str:
    normalized = _normalize(value)
    for suffix in _REGION_SUFFIXES:
        normalized = normalized.replace(suffix, "")
    return normalized


def _equivalent(key: ProfileFactKey, actual: str, expected: str) -> bool:
    if key in _LOCATION_KEYS:
        left = _normalize_location(actual)
        right = _normalize_location(expected)
        nested_region = (
            len(left) >= 3
            and len(right) >= 3
            and (left in right or right in left)
        )
        return bool(
            left
            and right
            and (
                left == right
                or left.startswith(right)
                or right.startswith(left)
                or nested_region
            )
        )
    return _normalize(actual) == _normalize(expected)


def _number(value: str) -> float | None:
    match = re.search(r"-?\d+(?:\.\d+)?", value.replace(",", ""))
    return float(match.group(0)) if match else None


def _condition_match(
    condition: EligibilityCondition,
    facts: list[ProfileFact],
) -> ConditionMatch:
    known = [
        fact
        for fact in facts
        if fact.key == condition.fact_key
        and fact.certainty == FactCertainty.EXPLICIT
    ]
    if not known:
        return ConditionMatch.UNKNOWN

    expected = condition.expected_values
    if condition.operator in {
        EligibilityOperator.EQUALS,
        EligibilityOperator.ONE_OF,
    }:
        return (
            ConditionMatch.MATCH
            if any(
                _equivalent(condition.fact_key, fact.value, value)
                for fact in known
                for value in expected
            )
            else ConditionMatch.CONFLICT
        )
    if condition.operator == EligibilityOperator.NOT_ONE_OF:
        return (
            ConditionMatch.CONFLICT
            if any(
                _equivalent(condition.fact_key, fact.value, value)
                for fact in known
                for value in expected
            )
            else ConditionMatch.MATCH
        )
    if condition.operator == EligibilityOperator.CONTAINS:
        return (
            ConditionMatch.MATCH
            if any(
                _normalize(value) in _normalize(fact.value)
                for fact in known
                for value in expected
            )
            else ConditionMatch.CONFLICT
        )
    threshold = _number(expected[0])
    actual_values = [_number(fact.value) for fact in known]
    actual_numbers = [value for value in actual_values if value is not None]
    if threshold is None or not actual_numbers:
        return ConditionMatch.UNKNOWN
    if condition.operator == EligibilityOperator.AT_LEAST:
        return (
            ConditionMatch.MATCH
            if any(value >= threshold for value in actual_numbers)
            else ConditionMatch.CONFLICT
        )
    return (
        ConditionMatch.MATCH
        if any(value <= threshold for value in actual_numbers)
        else ConditionMatch.CONFLICT
    )


def resolve_eligibility(
    assessment: NoticeAssessment,
    snapshot: ProfileSnapshot,
) -> EligibilityResolution | None:
    """OR 경로 안의 AND 조건을 3값 논리로 평가한다."""
    if not assessment.eligibility_paths:
        return None

    path_matches: list[list[ConditionMatch]] = [
        [_condition_match(condition, snapshot.facts) for condition in path.conditions]
        for path in assessment.eligibility_paths
        if path.conditions
    ]
    if not path_matches:
        return None

    if any(all(item == ConditionMatch.MATCH for item in path) for path in path_matches):
        return EligibilityResolution(
            AudienceFit.ELIGIBLE,
            EligibilityMatch.MATCH,
            "구조화된 프로필이 공지의 자격 경로 하나를 충족함",
        )
    if all(ConditionMatch.CONFLICT in path for path in path_matches):
        return EligibilityResolution(
            AudienceFit.INELIGIBLE,
            EligibilityMatch.CONFLICT,
            "구조화된 프로필과 공지의 모든 자격 경로가 충돌함",
        )
    if any(
        ConditionMatch.MATCH in path
        and ConditionMatch.CONFLICT not in path
        for path in path_matches
    ):
        return EligibilityResolution(
            AudienceFit.POSSIBLY_ELIGIBLE,
            EligibilityMatch.PARTIAL,
            "자격 조건 일부는 일치하지만 확인되지 않은 조건이 남아 있음",
        )
    return EligibilityResolution(
        AudienceFit.UNKNOWN,
        EligibilityMatch.UNKNOWN,
        "프로필로 뒷받침되는 자격 경로가 없고 필요한 사실이 확인되지 않음",
    )


def apply_profile_eligibility(
    assessment: NoticeAssessment,
    snapshot: ProfileSnapshot,
) -> NoticeAssessment:
    resolution = resolve_eligibility(assessment, snapshot)
    if resolution is None:
        return assessment
    return assessment.model_copy(
        update={
            "audience_fit": resolution.audience_fit,
            "audience_reason": resolution.reason,
            "eligibility_match": resolution.match,
        }
    )
