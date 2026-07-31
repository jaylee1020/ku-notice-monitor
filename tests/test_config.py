"""config.py(설정 로딩/검증) 단위 테스트"""

import os
from unittest.mock import patch

import pytest

from ku_notice_monitor.config import _load_json_env, _load_text_env, validate_config

# --- _load_json_env ---


def test_load_json_env_empty():
    with patch.dict(os.environ, {}, clear=True):
        result = _load_json_env("TEST_VAR", {"default": True})
    assert result == {"default": True}


def test_load_json_env_valid():
    with patch.dict(os.environ, {"TEST_VAR": '{"key": "value"}'}):
        result = _load_json_env("TEST_VAR", {})
    assert result == {"key": "value"}


def test_load_json_env_invalid_json():
    with patch.dict(os.environ, {"TEST_VAR": "not json"}):
        result = _load_json_env("TEST_VAR", {"fallback": True})
    assert result == {"fallback": True}


def test_load_json_env_not_dict():
    with patch.dict(os.environ, {"TEST_VAR": '["list"]'}):
        result = _load_json_env("TEST_VAR", {"fallback": True})
    assert result == {"fallback": True}


def test_load_text_env_strips_natural_profile():
    with patch.dict(os.environ, {"PROFILE_TEXT": "  나는 서울에 산다.  "}):
        result = _load_text_env("PROFILE_TEXT")
    assert result == "나는 서울에 산다."


# --- validate_config ---


def _make_valid_config():
    return {
        "profile": {},
        "keywords": {},
        "feeds": {"테스트": {"id": 234, "enabled": True}},
        "ai": {
            "model": "gpt-5.6-luna",
            "reasoning_effort": "low",
            "max_concurrency": 4,
            "request_timeout_seconds": 45,
            "image_detail": "low",
            "file_detail": "low",
        },
        "classification": {"action_window_days": 21},
        "notifications": {"digest_hour_kst": 21, "notify_empty_runs": False},
        "settings": {
            "state_file": "state.json",
            "base_url": "https://example.com",
            "rss_url_template": "https://example.com/{board_id}",
            "allowed_download_hosts": ["example.com"],
        },
    }


def test_validate_config_valid():
    validate_config(_make_valid_config())


def test_validate_config_missing_section():
    config = _make_valid_config()
    del config["feeds"]
    with pytest.raises(ValueError, match="feeds"):
        validate_config(config)


def test_validate_config_feed_missing_id():
    config = _make_valid_config()
    config["feeds"]["bad_feed"] = {"enabled": True}
    with pytest.raises(ValueError, match="bad_feed"):
        validate_config(config)


def test_validate_config_missing_ai_model():
    config = _make_valid_config()
    del config["ai"]["model"]
    with pytest.raises(ValueError, match="model"):
        validate_config(config)


def test_validate_config_missing_settings_field():
    config = _make_valid_config()
    del config["settings"]["base_url"]
    with pytest.raises(ValueError, match="base_url"):
        validate_config(config)


def test_validate_config_invalid_concurrency():
    config = _make_valid_config()
    config["ai"]["max_concurrency"] = 21
    with pytest.raises(ValueError, match="max_concurrency"):
        validate_config(config)


def test_validate_config_invalid_request_timeout():
    config = _make_valid_config()
    config["ai"]["request_timeout_seconds"] = 121
    with pytest.raises(ValueError, match="request_timeout_seconds"):
        validate_config(config)


def test_validate_config_requires_allowed_download_hosts():
    config = _make_valid_config()
    config["settings"]["allowed_download_hosts"] = []
    with pytest.raises(ValueError, match="allowed_download_hosts"):
        validate_config(config)


def test_validate_config_rejects_invalid_feed_success_ratio():
    config = _make_valid_config()
    config["settings"]["min_feed_success_ratio"] = 0
    with pytest.raises(ValueError, match="min_feed_success_ratio"):
        validate_config(config)


def test_validate_config_invalid_action_window():
    config = _make_valid_config()
    config["classification"]["action_window_days"] = 120
    with pytest.raises(ValueError, match="action_window_days"):
        validate_config(config)


def test_validate_config_rejects_non_boolean_speculative_policy():
    config = _make_valid_config()
    config["classification"]["suppress_speculative_opportunities"] = "yes"
    with pytest.raises(ValueError, match="suppress_speculative_opportunities"):
        validate_config(config)


def test_validate_config_invalid_base_url():
    config = _make_valid_config()
    config["settings"]["base_url"] = "not-a-url"
    with pytest.raises(ValueError, match="base_url"):
        validate_config(config)


def test_validate_config_rss_template_missing_placeholder():
    config = _make_valid_config()
    config["settings"]["rss_url_template"] = "https://example.com/fixed"
    with pytest.raises(ValueError, match="board_id"):
        validate_config(config)


def test_validate_config_ssl_verify_wrong_type():
    config = _make_valid_config()
    config["settings"]["ssl_verify"] = "true"  # 문자열 → bool이어야 함
    with pytest.raises(ValueError, match="ssl_verify"):
        validate_config(config)


def test_validate_config_feed_id_must_be_int():
    config = _make_valid_config()
    config["feeds"]["bad"] = {"id": "234", "enabled": True}
    with pytest.raises(ValueError, match="정수"):
        validate_config(config)


def test_validate_config_seed_on_first_run_wrong_type():
    config = _make_valid_config()
    config["settings"]["seed_on_first_run"] = "yes"  # bool이어야 함
    with pytest.raises(ValueError, match="seed_on_first_run"):
        validate_config(config)


def test_validate_config_seed_on_first_run_bool_ok():
    config = _make_valid_config()
    config["settings"]["seed_on_first_run"] = False
    validate_config(config)  # 예외 없이 통과해야 함


def test_validate_config_invalid_reasoning_effort():
    config = _make_valid_config()
    config["ai"]["reasoning_effort"] = "ultra"
    with pytest.raises(ValueError, match="reasoning_effort"):
        validate_config(config)


def test_validate_config_invalid_digest_hour():
    config = _make_valid_config()
    config["notifications"]["digest_hour_kst"] = 24
    with pytest.raises(ValueError, match="digest_hour_kst"):
        validate_config(config)
