"""공지에서 추출할 사실과 개인화 판정의 Structured Outputs 스키마."""

import re
from datetime import date
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, Field, field_validator, model_validator

from .profile_models import ProfileFactKey

ASSESSMENT_SCHEMA_VERSION = 3

_ISO_DATE_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])-([0-2]\d|3[01])$"
_ISO_DATETIME_PATTERN = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[ T]\d{2}:\d{2}"
    r"(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?$"
)
_KOREAN_DATE_PATTERN = re.compile(
    r"^(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일$"
)
IsoDate = Annotated[
    str,
    Field(pattern=_ISO_DATE_PATTERN, json_schema_extra={"format": "date"}),
]


def _normalize_exact_date(value: str) -> str | None:
    """정확한 연·월·일이 모두 있는 표현만 ISO 날짜로 정규화한다."""
    candidate = value.strip()
    datetime_match = _ISO_DATETIME_PATTERN.fullmatch(candidate)
    if datetime_match:
        candidate = datetime_match.group(1)
    else:
        korean_match = _KOREAN_DATE_PATTERN.fullmatch(candidate)
        if korean_match:
            candidate = "-".join(
                [
                    korean_match.group(1),
                    korean_match.group(2).zfill(2),
                    korean_match.group(3).zfill(2),
                ]
            )

    try:
        parsed = date.fromisoformat(candidate)
    except ValueError:
        return None
    return parsed.isoformat() if parsed.isoformat() == candidate else None


class NoticeCategory(StrEnum):
    ACADEMIC = "academic"
    TUITION = "tuition"
    SCHOLARSHIP = "scholarship"
    CAREER = "career"
    INTERNATIONAL = "international"
    EVENT = "event"
    CAMPUS_LIFE = "campus_life"
    ADMINISTRATIVE = "administrative"
    OTHER = "other"


class AudienceFit(StrEnum):
    ELIGIBLE = "eligible"
    POSSIBLY_ELIGIBLE = "possibly_eligible"
    INELIGIBLE = "ineligible"
    UNKNOWN = "unknown"


class InterestFit(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Obligation(StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"
    NONE = "none"


class Consequence(StrEnum):
    ACADEMIC_RISK = "academic_risk"
    FINANCIAL_LOSS = "financial_loss"
    ADMINISTRATIVE_BLOCK = "administrative_block"
    MISSED_OPPORTUNITY = "missed_opportunity"
    NONE = "none"


class DateKind(StrEnum):
    APPLICATION_OPEN = "application_open"
    APPLICATION_DEADLINE = "application_deadline"
    DOCUMENT_DEADLINE = "document_deadline"
    PAYMENT_DEADLINE = "payment_deadline"
    EVENT_START = "event_start"
    EVENT_END = "event_end"
    OTHER = "other"


class AttachmentNeed(StrEnum):
    NOT_NEEDED = "not_needed"
    USEFUL = "useful"
    REQUIRED = "required"


class EligibilityOperator(StrEnum):
    EQUALS = "equals"
    ONE_OF = "one_of"
    NOT_ONE_OF = "not_one_of"
    CONTAINS = "contains"
    AT_LEAST = "at_least"
    AT_MOST = "at_most"


class EligibilityMatch(StrEnum):
    NOT_EVALUATED = "not_evaluated"
    MATCH = "match"
    PARTIAL = "partial"
    UNKNOWN = "unknown"
    CONFLICT = "conflict"


class EligibilityCondition(BaseModel):
    """프로필 사실 하나와 비교할 수 있는 공지의 원자적 자격 조건."""

    fact_key: ProfileFactKey
    operator: EligibilityOperator
    expected_values: list[str] = Field(min_length=1, max_length=10)
    evidence: str = Field(min_length=1, max_length=200)


class EligibilityPath(BaseModel):
    """모두 충족해야 하는 AND 조건 묶음. 여러 path는 서로 OR 관계."""

    label: str = Field(min_length=1, max_length=120)
    conditions: list[EligibilityCondition] = Field(min_length=1, max_length=12)


class NoticeDate(BaseModel):
    kind: DateKind
    date: IsoDate
    label: str = Field(min_length=1, max_length=80)

    @field_validator("date")
    @classmethod
    def validate_date(cls, value: str) -> str:
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise ValueError("date must use YYYY-MM-DD")
        if parsed.isoformat() != value:
            raise ValueError("date must use YYYY-MM-DD")
        return parsed.isoformat()


class NoticeAction(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    required: bool
    deadline: IsoDate | None = None

    @field_validator("deadline")
    @classmethod
    def validate_deadline(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise ValueError("deadline must use YYYY-MM-DD")
        if parsed.isoformat() != value:
            raise ValueError("deadline must use YYYY-MM-DD")
        return parsed.isoformat()


class NoticeAssessment(BaseModel):
    """한 공지에 대한 사실 추출 및 사용자 적격성 평가."""

    category: NoticeCategory
    summary: str = Field(min_length=1, max_length=240)
    audience_fit: AudienceFit
    audience_reason: str = Field(min_length=1, max_length=200)
    eligibility_paths: list[EligibilityPath] = Field(default_factory=list, max_length=8)
    eligibility_match: EligibilityMatch = EligibilityMatch.NOT_EVALUATED
    interest_fit: InterestFit
    interest_reason: str = Field(min_length=1, max_length=160)
    obligation: Obligation
    consequence: Consequence
    dates: list[NoticeDate] = Field(default_factory=list, max_length=8)
    actions: list[NoticeAction] = Field(default_factory=list, max_length=8)
    benefits: list[str] = Field(default_factory=list, max_length=5)
    evidence: list[str] = Field(default_factory=list, max_length=5)
    uncertainties: list[str] = Field(default_factory=list, max_length=5)
    attachment_need: AttachmentNeed

    @model_validator(mode="before")
    @classmethod
    def normalize_exact_dates(cls, value: Any) -> Any:
        """날짜 하나 때문에 공지 전체가 폐기되지 않도록 안전하게 정리한다.

        시각이 붙은 정확한 날짜와 완전한 한국어 날짜는 ISO로 바꾼다. 연·월·일이
        하나라도 불명확한 표현은 추측하지 않고 제외하며 사용자용 불확실성으로 남긴다.
        날짜 외 구조 오류는 그대로 두어 정상 검증을 우회하지 않는다.
        """
        if not isinstance(value, dict):
            return value

        cleaned = dict(value)
        removed_imprecise_date = False

        raw_dates = value.get("dates")
        if isinstance(raw_dates, list):
            normalized_dates: list[Any] = []
            valid_date_kinds = {kind.value for kind in DateKind}
            for item in raw_dates:
                if not isinstance(item, dict) or not isinstance(item.get("date"), str):
                    normalized_dates.append(item)
                    continue
                normalized = _normalize_exact_date(item["date"])
                if normalized is None:
                    label = item.get("label")
                    if (
                        item.get("kind") in valid_date_kinds
                        and isinstance(label, str)
                        and 1 <= len(label) <= 80
                    ):
                        removed_imprecise_date = True
                        continue
                    # 날짜가 아닌 필드까지 잘못된 항목은 그대로 검증해 구조 오류를 숨기지 않는다.
                    normalized_dates.append(item)
                    continue
                normalized_dates.append({**item, "date": normalized})
            cleaned["dates"] = normalized_dates

        raw_actions = value.get("actions")
        if isinstance(raw_actions, list):
            normalized_actions: list[Any] = []
            for item in raw_actions:
                if not isinstance(item, dict) or not isinstance(item.get("deadline"), str):
                    normalized_actions.append(item)
                    continue
                normalized = _normalize_exact_date(item["deadline"])
                if normalized is None:
                    removed_imprecise_date = True
                    normalized_actions.append({**item, "deadline": None})
                else:
                    normalized_actions.append({**item, "deadline": normalized})
            cleaned["actions"] = normalized_actions

        raw_uncertainties = value.get("uncertainties", [])
        if removed_imprecise_date and isinstance(raw_uncertainties, list):
            uncertainty = "정확한 날짜로 확정할 수 없는 일정 표현은 제외함"
            if len(raw_uncertainties) < 5 and uncertainty not in raw_uncertainties:
                cleaned["uncertainties"] = [*raw_uncertainties, uncertainty]
        return cleaned
