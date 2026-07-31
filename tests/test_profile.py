"""자연어 프로필 구조화와 개인정보 비저장 경계 테스트."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from ku_notice_monitor.profile import (
    _ground_snapshot,
    legacy_profile_snapshot,
    profile_document_fingerprint,
    resolve_profile_snapshot,
)
from ku_notice_monitor.profile_models import ProfileSnapshot


def _config(profile_text=""):
    return {
        "profile_text": profile_text,
        "profile": {
            "major": "컴퓨터공학부",
            "previous_major": "KU자유전공학부",
            "year": 2,
            "campus": "서울",
            "status": "재학",
        },
        "keywords": {"high": ["장학"], "medium": ["인턴"]},
        "ai": {
            "model": "gpt-5.6-luna",
            "reasoning_effort": "low",
            "request_timeout_seconds": 45,
        },
    }


def test_legacy_profile_snapshot_preserves_existing_settings():
    snapshot = legacy_profile_snapshot(_config())
    facts = {(fact.key.value, fact.value) for fact in snapshot.facts}
    assert ("major", "컴퓨터공학부") in facts
    assert ("campus", "서울") in facts
    assert any(item.statement == "장학" for item in snapshot.preferences)


def test_profile_fingerprint_changes_without_containing_plaintext():
    first = profile_document_fingerprint(_config("서울에 산다"))
    second = profile_document_fingerprint(_config("부산에 산다"))
    assert first != second
    assert len(first) == 64
    assert "서울" not in first


def test_profile_grounding_removes_unsupported_values():
    snapshot = ProfileSnapshot.model_validate(
        {
            "summary": "서울 거주 학생",
            "facts": [
                {
                    "key": "current_residence",
                    "value": "서울특별시",
                    "certainty": "explicit",
                    "source_quote": "서울특별시에 산다",
                },
                {
                    "key": "registered_residence",
                    "value": "서울특별시",
                    "certainty": "explicit",
                    "source_quote": "주민등록상 주소도 서울이다",
                },
            ],
            "preferences": [],
        }
    )
    grounded = _ground_snapshot("나는 서울특별시에 산다.", snapshot)
    assert [fact.key.value for fact in grounded.facts] == ["current_residence"]


def test_resolve_natural_profile_uses_structured_output(monkeypatch):
    document = "나는 서울특별시에 산다. 다른 지역 주민 전용 사업은 보내지 마라."
    parsed = ProfileSnapshot.model_validate(
        {
            "summary": "서울 거주 사용자",
            "facts": [
                {
                    "key": "current_residence",
                    "value": "서울특별시",
                    "certainty": "explicit",
                    "source_quote": "서울특별시에 산다",
                }
            ],
            "preferences": [
                {
                    "kind": "exclude",
                    "statement": "다른 지역 주민 전용 사업",
                    "source_quote": "다른 지역 주민 전용 사업은 보내지 마라",
                }
            ],
        }
    )
    response = SimpleNamespace(
        usage=SimpleNamespace(total_tokens=123),
        _request_id="req_profile",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    metrics = {}
    with (
        patch("ku_notice_monitor.profile.AsyncOpenAI"),
        patch(
            "ku_notice_monitor.profile._parse_profile_document",
            new_callable=AsyncMock,
            return_value=(parsed, response),
        ),
    ):
        snapshot = asyncio.run(
            resolve_profile_snapshot(_config(document), metrics=metrics)
        )
    assert snapshot.facts[0].key.value == "current_residence"
    assert metrics["source"] == "natural_language"
    assert metrics["total_tokens"] == 123

