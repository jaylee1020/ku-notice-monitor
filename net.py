"""공통 HTTP/SSL 유틸리티

feeds.py와 matcher.py가 공유하는 네트워킹 코드를 한곳에 모아 모듈 간
결합(서로의 private 심볼을 가져다 쓰는 문제)과 다운로드 로직 중복을 제거한다.
"""

import asyncio
import logging
import ssl

import aiohttp
import certifi

logger = logging.getLogger(__name__)

# 건국대 서버는 기본 User-Agent를 차단하는 경우가 있어 브라우저 UA로 위장한다.
DEFAULT_HEADERS = {"User-Agent": "Mozilla/5.0"}


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
    semaphore: asyncio.Semaphore | None = None,
    max_size: int | None = None,
) -> bytes | None:
    """단일 URL을 바이너리로 다운로드한다.

    이미지/첨부파일 다운로드의 공통 경로. 실패(비200, 크기 초과, 예외)는
    모두 None으로 흡수해 호출부가 개별 실패에 흔들리지 않게 한다.
    semaphore가 주어지면 동시 요청 수를 제한한다.
    """

    async def _request() -> bytes | None:
        async with session.get(
            url, ssl=ssl_context, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as resp:
            if resp.status != 200:
                logger.debug("다운로드 실패 (status=%d): %s", resp.status, url)
                return None
            if max_size is not None:
                content_length = resp.headers.get("Content-Length")
                if content_length and int(content_length) > max_size:
                    logger.debug("크기 초과 (%s bytes): %s", content_length, url)
                    return None
            data = await resp.read()
            if max_size is not None and len(data) > max_size:
                logger.debug("크기 초과 (%d bytes): %s", len(data), url)
                return None
            return data

    try:
        if semaphore is not None:
            async with semaphore:
                return await _request()
        return await _request()
    except Exception as e:
        logger.debug("다운로드 예외: %s - %s", url, e)
        return None
