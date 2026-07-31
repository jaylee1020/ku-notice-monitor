"""외부 다운로드의 SSRF·크기 제한 테스트."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from ku_notice_monitor.net import (
    UnsafeUrlError,
    allowed_hosts_from_config,
    download_bytes,
    is_allowed_hostname,
    validate_url_target,
)


class _FakeContent:
    def __init__(self, chunks):
        self._chunks = chunks

    async def iter_chunked(self, _size):
        for chunk in self._chunks:
            yield chunk


class _FakeResponse:
    def __init__(self, *, status=200, headers=None, chunks=None):
        self.status = status
        self.headers = headers or {}
        self.content = _FakeContent(chunks or [])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _FakeSession:
    def __init__(self, responses):
        self._responses = iter(responses)

    def get(self, *_args, **_kwargs):
        return next(self._responses)


def test_allowed_hostname_accepts_subdomain_only():
    allowed = {"konkuk.ac.kr"}
    assert is_allowed_hostname("www.konkuk.ac.kr", allowed) is True
    assert is_allowed_hostname("konkuk.ac.kr.evil.example", allowed) is False


def test_allowed_hosts_include_configured_and_feed_hosts():
    config = {
        "settings": {
            "base_url": "https://www.konkuk.ac.kr",
            "allowed_download_hosts": ["konkuk.ac.kr"],
        },
        "feeds": {"취업": {"rss_url": "https://kuinc.konkuk.ac.kr/rss"}},
    }
    assert allowed_hosts_from_config(config) == {
        "konkuk.ac.kr",
        "www.konkuk.ac.kr",
        "kuinc.konkuk.ac.kr",
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://www.konkuk.ac.kr/file.pdf",
        "https://user:pass@www.konkuk.ac.kr/file.pdf",
        "https://127.0.0.1/file.pdf",
        "https://169.254.169.254/latest/meta-data",
    ],
)
def test_validate_url_rejects_unsafe_targets(url):
    with pytest.raises(UnsafeUrlError):
        asyncio.run(validate_url_target(url, {"konkuk.ac.kr", "127.0.0.1", "169.254.169.254"}))


def test_download_stream_stops_at_size_limit():
    session = _FakeSession(
        [
            _FakeResponse(
                headers={"Content-Type": "image/png"},
                chunks=[b"123456", b"789012"],
            )
        ]
    )
    with patch("ku_notice_monitor.net.validate_url_target", new_callable=AsyncMock):
        result = asyncio.run(
            download_bytes(
                session,
                "https://www.konkuk.ac.kr/image.png",
                ssl_context=None,
                timeout=10,
                allowed_hosts={"konkuk.ac.kr"},
                max_size=10,
                expected_content_prefix="image/",
            )
        )
    assert result is None


def test_download_accepts_bounded_content():
    session = _FakeSession(
        [
            _FakeResponse(
                headers={"Content-Type": "image/png", "Content-Length": "6"},
                chunks=[b"123", b"456"],
            )
        ]
    )
    with patch("ku_notice_monitor.net.validate_url_target", new_callable=AsyncMock):
        result = asyncio.run(
            download_bytes(
                session,
                "https://www.konkuk.ac.kr/image.png",
                ssl_context=None,
                timeout=10,
                allowed_hosts={"konkuk.ac.kr"},
                max_size=10,
                expected_content_prefix="image/",
            )
        )
    assert result == b"123456"
