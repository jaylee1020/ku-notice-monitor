"""자연어 사용자 프로필의 구조화 스냅샷 모델."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

PROFILE_SCHEMA_VERSION = 1


class ProfileFactKey(StrEnum):
    MAJOR = "major"
    PREVIOUS_MAJOR = "previous_major"
    ACADEMIC_YEAR = "academic_year"
    CAMPUS = "campus"
    ENROLLMENT_STATUS = "enrollment_status"
    DEGREE_LEVEL = "degree_level"
    CURRENT_RESIDENCE = "current_residence"
    REGISTERED_RESIDENCE = "registered_residence"
    FAMILY_REGISTERED_RESIDENCE = "family_registered_residence"
    AGE = "age"
    BIRTH_YEAR = "birth_year"
    NATIONALITY = "nationality"
    MILITARY_STATUS = "military_status"
    INCOME_BRACKET = "income_bracket"
    SCHOLARSHIP_USAGE = "scholarship_usage"
    OTHER = "other"


class FactCertainty(StrEnum):
    EXPLICIT = "explicit"
    UNCERTAIN = "uncertain"


class PreferenceKind(StrEnum):
    PRIORITY = "priority"
    EXCLUDE = "exclude"
    DO_NOT_INFER = "do_not_infer"


class ProfileFact(BaseModel):
    key: ProfileFactKey
    value: str = Field(min_length=1, max_length=160)
    certainty: FactCertainty
    source_quote: str = Field(min_length=1, max_length=240)


class ProfilePreference(BaseModel):
    kind: PreferenceKind
    statement: str = Field(min_length=1, max_length=200)
    source_quote: str = Field(min_length=1, max_length=240)


class ProfileSnapshot(BaseModel):
    """사용자 문서에서 직접 근거를 찾을 수 있는 사실과 알림 선호."""

    schema_version: Literal[1] = 1
    summary: str = Field(min_length=1, max_length=300)
    facts: list[ProfileFact] = Field(default_factory=list, max_length=50)
    preferences: list[ProfilePreference] = Field(default_factory=list, max_length=30)
