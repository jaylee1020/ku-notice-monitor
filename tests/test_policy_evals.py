"""정책 회귀를 막는 고정 분류 사례."""

import json
from datetime import date
from pathlib import Path

import pytest

from ku_notice_monitor.analysis_models import NoticeAssessment
from ku_notice_monitor.classification import decide_delivery

_CASES_PATH = Path(__file__).parents[1] / "evals" / "classification_cases.jsonl"
_CASES = [
    json.loads(line)
    for line in _CASES_PATH.read_text(encoding="utf-8").splitlines()
    if line.strip()
]


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case["name"])
def test_delivery_policy_golden_cases(case):
    assessment = NoticeAssessment.model_validate(case["assessment"])
    delivery, _ = decide_delivery(
        assessment,
        today=date.fromisoformat(case["today"]),
    )
    assert delivery.value == case["expected"]
