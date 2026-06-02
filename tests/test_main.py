"""main.py 단위 테스트"""

import os
from unittest.mock import patch

import pytest

from main import _load_json_env, validate_config

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


# --- validate_config ---


def _make_valid_config():
    return {
        "profile": {},
        "keywords": {},
        "feeds": {"테스트": {"id": 234, "enabled": True}},
        "gemini": {"model": "gemini-flash-latest", "relevance_threshold": 3},
        "settings": {
            "state_file": "state.json",
            "base_url": "https://example.com",
            "rss_url_template": "https://example.com/{board_id}",
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


def test_validate_config_missing_gemini_model():
    config = _make_valid_config()
    del config["gemini"]["model"]
    with pytest.raises(ValueError, match="model"):
        validate_config(config)


def test_validate_config_missing_settings_field():
    config = _make_valid_config()
    del config["settings"]["base_url"]
    with pytest.raises(ValueError, match="base_url"):
        validate_config(config)


def test_validate_config_invalid_threshold():
    config = _make_valid_config()
    config["gemini"]["relevance_threshold"] = 10
    with pytest.raises(ValueError, match="relevance_threshold"):
        validate_config(config)


def test_validate_config_threshold_wrong_type():
    config = _make_valid_config()
    config["gemini"]["relevance_threshold"] = "3"
    with pytest.raises(ValueError, match="relevance_threshold"):
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
