"""Structured Output 날짜 복구와 스키마 회귀 테스트."""

import re

import pytest
from pydantic import ValidationError

from ku_notice_monitor.analysis_models import NoticeAssessment


def _assessment_payload(**overrides) -> dict:
    payload = {
        "category": "career",
        "summary": "테스트 공지",
        "audience_fit": "eligible",
        "audience_reason": "재학생 대상",
        "interest_fit": "medium",
        "interest_reason": "관심 분야",
        "obligation": "optional",
        "consequence": "missed_opportunity",
        "dates": [],
        "actions": [],
        "benefits": [],
        "evidence": ["재학생 대상"],
        "uncertainties": [],
        "attachment_need": "not_needed",
    }
    payload.update(overrides)
    return payload


def _dated_payload(raw_date: str) -> dict:
    return _assessment_payload(
        dates=[
            {
                "kind": "application_deadline",
                "date": raw_date,
                "label": "신청 마감",
            }
        ],
        actions=[
            {
                "label": "지원서 제출",
                "required": False,
                "deadline": raw_date,
            }
        ],
    )


def test_iso_datetime_is_normalized_to_iso_date():
    assessment = NoticeAssessment.model_validate(
        _dated_payload("2026-08-27 17:00")
    )

    assert assessment.dates[0].date == "2026-08-27"
    assert assessment.actions[0].deadline == "2026-08-27"
    assert assessment.uncertainties == []


@pytest.mark.parametrize(
    "raw_date",
    [
        "2026년 8월 또는 9월 중 협의일",
        "9월 1일",
    ],
)
def test_ambiguous_or_yearless_dates_are_removed_with_uncertainty(raw_date):
    assessment = NoticeAssessment.model_validate(_dated_payload(raw_date))

    assert assessment.dates == []
    assert assessment.actions[0].deadline is None
    assert assessment.uncertainties
    assert any("날짜" in uncertainty for uncertainty in assessment.uncertainties)


def test_complete_korean_date_is_normalized():
    assessment = NoticeAssessment.model_validate(
        _dated_payload("2026년 9월 1일")
    )

    assert assessment.dates[0].date == "2026-09-01"
    assert assessment.actions[0].deadline == "2026-09-01"
    assert assessment.uncertainties == []


def test_generated_json_schema_requires_iso_dates():
    schema = NoticeAssessment.model_json_schema()
    date_schema = schema["$defs"]["NoticeDate"]["properties"]["date"]
    deadline_schema = schema["$defs"]["NoticeAction"]["properties"]["deadline"]

    date_pattern = date_schema["pattern"]
    deadline_pattern = next(
        option["pattern"]
        for option in deadline_schema["anyOf"]
        if option.get("type") == "string"
    )
    for pattern in (date_pattern, deadline_pattern):
        assert re.fullmatch(pattern, "2026-09-01")
        assert re.fullmatch(pattern, "2026-09-01T00:00:00") is None
    assert date_schema["format"] == "date"
    assert next(
        option["format"]
        for option in deadline_schema["anyOf"]
        if option.get("type") == "string"
    ) == "date"


def test_non_date_enum_errors_are_not_silently_recovered():
    payload = _dated_payload("2026년 8월 또는 9월 중 협의일")
    payload["category"] = "not-a-real-category"

    with pytest.raises(ValidationError):
        NoticeAssessment.model_validate(payload)


@pytest.mark.parametrize(
    "broken_date_item",
    [
        {
            "kind": "not-a-real-kind",
            "date": "9월 1일",
            "label": "신청 마감",
        },
        {
            "kind": "application_deadline",
            "date": "9월 1일",
        },
    ],
)
def test_imprecise_date_does_not_hide_other_date_item_errors(broken_date_item):
    payload = _assessment_payload(dates=[broken_date_item])

    with pytest.raises(ValidationError):
        NoticeAssessment.model_validate(payload)


def test_date_recovery_does_not_hide_too_many_uncertainties():
    payload = _dated_payload("9월 1일")
    payload["uncertainties"] = [f"불확실성 {index}" for index in range(6)]

    with pytest.raises(ValidationError):
        NoticeAssessment.model_validate(payload)
