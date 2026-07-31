"""공지에서 추출할 사실과 개인화 판정의 Structured Outputs 스키마."""

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

ASSESSMENT_SCHEMA_VERSION = 1


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


class NoticeDate(BaseModel):
    kind: DateKind
    date: str
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
    deadline: str | None = None

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
