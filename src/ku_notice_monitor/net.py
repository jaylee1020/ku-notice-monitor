"""공통 HTTP/SSL 유틸리티

feeds.py와 matcher.py가 공유하는 네트워킹 코드를 한곳에 모아 모듈 간
결합(서로의 private 심볼을 가져다 쓰는 문제)과 다운로드 로직 중복을 제거한다.
"""

import asyncio
import ipaddress
import logging
import socket
import ssl
from urllib.parse import urljoin, urlsplit

import aiohttp
import certifi

from .constants import MAX_DOWNLOAD_REDIRECTS

logger = logging.getLogger(__name__)

# 건국대 서버는 기본 User-Agent를 차단하는 경우가 있어 브라우저 UA로 위장한다.
DEFAULT_HEADERS = {"User-Agent": "Mozilla/5.0"}


class UnsafeUrlError(ValueError):
    """허용되지 않거나 사설 네트워크를 가리키는 URL."""


def allowed_hosts_from_config(config: dict) -> set[str]:
    """설정에서 다운로드를 허용할 호스트/도메인 접미사를 만든다."""
    configured = config.get("settings", {}).get("allowed_download_hosts", [])
    hosts = {str(host).strip().lower().rstrip(".") for host in configured if host}
    for url in (
        config.get("settings", {}).get("base_url", ""),
        *(
            feed.get("rss_url", "")
            for feed in config.get("feeds", {}).values()
            if isinstance(feed, dict)
        ),
    ):
        hostname = urlsplit(str(url)).hostname
        if hostname:
            hosts.add(hostname.lower().rstrip("."))
    return hosts


def is_allowed_hostname(hostname: str, allowed_hosts: set[str]) -> bool:
    host = hostname.lower().rstrip(".")
    return any(host == allowed or host.endswith(f".{allowed}") for allowed in allowed_hosts)


def _assert_public_ip(address: str) -> None:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError as exc:
        raise UnsafeUrlError(f"유효하지 않은 IP 주소입니다: {address}") from exc
    if not parsed.is_global:
        raise UnsafeUrlError(f"사설·로컬 네트워크 주소는 접근할 수 없습니다: {address}")


async def validate_url_target(url: str, allowed_hosts: set[str]) -> str:
    """URL 스킴·호스트·DNS 해석 결과를 검증한다."""
    parsed = urlsplit(url)
    if parsed.scheme != "https":
        raise UnsafeUrlError("HTTPS URL만 다운로드할 수 있습니다.")
    if parsed.username or parsed.password:
        raise UnsafeUrlError("사용자 정보가 포함된 URL은 허용하지 않습니다.")
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if not hostname or not is_allowed_hostname(hostname, allowed_hosts):
        raise UnsafeUrlError(f"허용되지 않은 다운로드 호스트입니다: {hostname or '(없음)'}")

    try:
        _assert_public_ip(hostname)
        return url
    except UnsafeUrlError:
        # IP 리터럴이면 즉시 차단하고, 일반 호스트명만 DNS 확인으로 진행한다.
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            raise

    loop = asyncio.get_running_loop()
    try:
        addresses = await loop.getaddrinfo(
            hostname,
            parsed.port or 443,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise UnsafeUrlError(f"다운로드 호스트 DNS 확인 실패: {hostname}") from exc
    if not addresses:
        raise UnsafeUrlError(f"다운로드 호스트가 주소로 해석되지 않습니다: {hostname}")
    for info in addresses:
        _assert_public_ip(str(info[4][0]))
    return url


def make_ssl_context(ssl_verify: bool) -> ssl.SSLContext:
    """SSL 컨텍스트 생성. ssl_verify=False면 인증서 검증을 끈다."""
    if ssl_verify:
        return ssl.create_default_context(cafile=certifi.where())
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def ssl_context_from_config(config: dict) -> ssl.SSLContext:
    """config의 settings.ssl_verify(기본 True)를 따라 SSL 컨텍스트를 생성한다."""
    return make_ssl_context(config.get("settings", {}).get("ssl_verify", True))


async def download_bytes(
    session: aiohttp.ClientSession,
    url: str,
    *,
    ssl_context: ssl.SSLContext,
    timeout: int,
    allowed_hosts: set[str],
    semaphore: asyncio.Semaphore | None = None,
    max_size: int,
    expected_content_prefix: str | None = None,
) -> bytes | None:
    """단일 URL을 바이너리로 다운로드한다.

    이미지/첨부파일 다운로드의 공통 경로. 실패(비200, 크기 초과, 예외)는
    모두 None으로 흡수해 호출부가 개별 실패에 흔들리지 않게 한다.
    semaphore가 주어지면 동시 요청 수를 제한한다.
    """

    async def _request() -> bytes | None:
        current_url = url
        for redirect_count in range(MAX_DOWNLOAD_REDIRECTS + 1):
            await validate_url_target(current_url, allowed_hosts)
            async with session.get(
                current_url,
                ssl=ssl_context,
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=False,
            ) as resp:
                if 300 <= resp.status < 400:
                    location = resp.headers.get("Location")
                    if not location or redirect_count >= MAX_DOWNLOAD_REDIRECTS:
                        logger.debug("리디렉션 제한 초과 또는 Location 없음: %s", current_url)
                        return None
                    current_url = urljoin(current_url, location)
                    continue
                if resp.status != 200:
                    logger.debug("다운로드 실패 (status=%d): %s", resp.status, current_url)
                    return None
                content_type = resp.headers.get("Content-Type", "").split(";", 1)[0].lower()
                if expected_content_prefix and not content_type.startswith(expected_content_prefix):
                    logger.debug(
                        "예상하지 않은 Content-Type (%s): %s",
                        content_type or "없음",
                        current_url,
                    )
                    return None
                content_length = resp.headers.get("Content-Length")
                try:
                    declared_size = int(content_length) if content_length else None
                except ValueError:
                    declared_size = None
                if declared_size is not None and declared_size > max_size:
                    logger.debug("크기 초과 (%s bytes): %s", content_length, current_url)
                    return None
                data = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    data.extend(chunk)
                    if len(data) > max_size:
                        logger.debug("스트리밍 크기 초과 (%d bytes): %s", len(data), current_url)
                        return None
                return bytes(data)
        return None

    try:
        if semaphore is not None:
            async with semaphore:
                return await _request()
        return await _request()
    except Exception as e:
        logger.debug("다운로드 예외: %s - %s", url, e)
        return None
