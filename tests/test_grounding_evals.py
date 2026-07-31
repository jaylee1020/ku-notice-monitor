"""실제 대학 공지 형태를 익명화한 근거 검증 회귀 사례."""

import json
from datetime import date
from pathlib import Path

import pytest

from ku_notice_monitor.analysis_models import NoticeAssessment
from ku_notice_monitor.classification import classify_assessment
from ku_notice_monitor.matcher import validate_assessment_grounding

_CASES_PATH = Path(__file__).parents[1] / "evals" / "notice_grounding_cases.jsonl"
_CASES = [
    json.loads(line)
    for line in _CASES_PATH.read_text(encoding="utf-8").splitlines()
    if line.strip()
]


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case["name"])
def test_grounding_and_delivery_cases(case, make_article):
    article = make_article(**case["article"])
    assessment = NoticeAssessment.model_validate(case["assessment"])
    grounded = validate_assessment_grounding(article, assessment)
    result = classify_assessment(
        article,
        grounded,
        source="openai",
        today=date.fromisoformat(case["today"]),
    )
    assert result.delivery == case["expected"]
