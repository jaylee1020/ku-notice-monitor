"""구조화 프로필과 공지 자격 경로의 결정론적 비교 테스트."""

from datetime import date

from ku_notice_monitor.analysis_models import NoticeAssessment
from ku_notice_monitor.classification import Delivery, decide_delivery
from ku_notice_monitor.eligibility import apply_profile_eligibility
from ku_notice_monitor.profile_models import ProfileSnapshot


def _snapshot(*facts):
    return ProfileSnapshot.model_validate(
        {
            "summary": "테스트 프로필",
            "facts": [
                {
                    "key": key,
                    "value": value,
                    "certainty": "explicit",
                    "source_quote": quote,
                }
                for key, value, quote in facts
            ],
            "preferences": [],
        }
    )


def _ulsan_assessment():
    return NoticeAssessment.model_validate(
        {
            "category": "scholarship",
            "summary": "울산광역시 대학생 학자금대출 이자지원",
            "audience_fit": "possibly_eligible",
            "audience_reason": "본인 또는 직계존속 주소 확인 필요",
            "eligibility_paths": [
                {
                    "label": "학생 본인 주소",
                    "conditions": [
                        {
                            "fact_key": "registered_residence",
                            "operator": "equals",
                            "expected_values": ["울산광역시"],
                            "evidence": "본인의 주민등록상 주소가 울산광역시",
                        }
                    ],
                },
                {
                    "label": "직계존속 주소",
                    "conditions": [
                        {
                            "fact_key": "family_registered_residence",
                            "operator": "equals",
                            "expected_values": ["울산광역시"],
                            "evidence": "직계존속의 주민등록상 주소가 울산광역시",
                        }
                    ],
                },
            ],
            "interest_fit": "high",
            "interest_reason": "장학 관심",
            "obligation": "optional",
            "consequence": "financial_loss",
            "dates": [],
            "actions": [],
            "benefits": ["대출 이자 지원"],
            "evidence": ["울산광역시 대학생 학자금대출 이자지원"],
            "uncertainties": [],
            "attachment_need": "not_needed",
        }
    )


def test_current_residence_does_not_imply_registered_residence():
    assessment = apply_profile_eligibility(
        _ulsan_assessment(),
        _snapshot(
            ("current_residence", "서울특별시", "서울특별시에 산다"),
        ),
    )
    assert assessment.audience_fit.value == "unknown"
    assert assessment.eligibility_match.value == "unknown"


def test_unknown_optional_regional_opportunity_is_suppressed():
    assessment = apply_profile_eligibility(
        _ulsan_assessment(),
        _snapshot(
            ("current_residence", "서울특별시", "서울특별시에 산다"),
        ),
    )
    delivery, reason = decide_delivery(
        assessment,
        today=date(2026, 7, 31),
        suppress_speculative_opportunities=True,
    )
    assert delivery == Delivery.SUPPRESS
    assert "자격 경로" in reason


def test_all_alternative_paths_conflicting_is_ineligible():
    assessment = apply_profile_eligibility(
        _ulsan_assessment(),
        _snapshot(
            (
                "registered_residence",
                "서울특별시",
                "주민등록상 주소는 서울특별시",
            ),
            (
                "family_registered_residence",
                "경기도",
                "부모님 주민등록상 주소는 경기도",
            ),
        ),
    )
    assert assessment.audience_fit.value == "ineligible"
    assert assessment.eligibility_match.value == "conflict"


def test_one_alternative_path_matching_is_eligible():
    assessment = apply_profile_eligibility(
        _ulsan_assessment(),
        _snapshot(
            (
                "registered_residence",
                "서울특별시",
                "주민등록상 주소는 서울특별시",
            ),
            (
                "family_registered_residence",
                "울산광역시",
                "부모님 주민등록상 주소는 울산광역시",
            ),
        ),
    )
    assert assessment.audience_fit.value == "eligible"
    assert assessment.eligibility_match.value == "match"


def test_city_district_residence_satisfies_broader_city_condition():
    assessment = apply_profile_eligibility(
        _ulsan_assessment(),
        _snapshot(
            (
                "registered_residence",
                "울산광역시 남구",
                "주민등록상 주소는 울산광역시 남구",
            ),
        ),
    )
    assert assessment.audience_fit.value == "eligible"
