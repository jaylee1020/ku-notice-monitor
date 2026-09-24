"""OpenAI Responses API 공통 호출 계층.

공지 분석기와 프로필 구조화기가 같은 클라이언트 생성·재시도·사용량 집계 규칙을
공유하도록 한 곳에 모은다.
"""

import asyncio
import logging
import os
from typing import Any

from openai import AsyncOpenAI
from pydantic import BaseModel
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

_USAGE_FIELDS = (
    "request_attempts",
    "successful_calls",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_input_tokens",
)


class StructuredOutputError(ValueError):
    """모델 응답에서 스키마에 맞는 결과를 얻지 못했을 때 발생한다."""


def openai_configured() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


def make_client(config: dict) -> AsyncOpenAI:
    """재시도는 tenacity가 담당하므로 SDK 자체 재시도는 끈다."""
    return AsyncOpenAI(
        api_key=os.environ["OPENAI_API_KEY"],
        timeout=config["ai"].get("request_timeout_seconds", 45),
        max_retries=0,
    )


def is_retryable_api_error(exc: BaseException) -> bool:
    """네트워크 오류·429·5xx만 재시도한다. 스키마·입력 오류는 재시도해도 같다."""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
        return True
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return False
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status == 429 or 500 <= status < 600
    # SDK의 APIConnectionError/APITimeoutError는 상태 코드가 없다.
    return type(exc).__name__ in {"APIConnectionError", "APITimeoutError"}


def empty_usage() -> dict[str, int]:
    return dict.fromkeys(_USAGE_FIELDS, 0)


def _record_usage(metrics: dict | None, response: Any) -> None:
    if metrics is None:
        return
    metrics["successful_calls"] = metrics.get("successful_calls", 0) + 1
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    input_details = getattr(usage, "input_tokens_details", None)
    for key, value in (
        ("input_tokens", getattr(usage, "input_tokens", 0)),
        ("output_tokens", getattr(usage, "output_tokens", 0)),
        ("total_tokens", getattr(usage, "total_tokens", 0)),
        ("cached_input_tokens", getattr(input_details, "cached_tokens", 0)),
    ):
        metrics[key] = metrics.get(key, 0) + (value or 0)


@retry(
    retry=retry_if_exception(is_retryable_api_error),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=16),
    reraise=True,
)
async def parse_structured[ModelT: BaseModel](
    client: AsyncOpenAI,
    *,
    config: dict,
    system_prompt: str,
    user_content: str | list[dict],
    output_type: type[ModelT],
    cache_key: str,
    metrics: dict | None = None,
) -> ModelT:
    """Structured Outputs로 한 번 호출하고 파싱된 모델을 반환한다."""
    if metrics is not None:
        metrics["request_attempts"] = metrics.get("request_attempts", 0) + 1
    request_input: Any = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    reasoning: Any = {"effort": config["ai"].get("reasoning_effort", "medium")}
    response = await client.responses.parse(
        model=config["ai"]["model"],
        input=request_input,
        reasoning=reasoning,
        text_format=output_type,
        # 같은 시스템 프롬프트·프로필 접두부를 공유하는 요청을 같은 캐시로 보낸다.
        prompt_cache_key=cache_key,
        store=False,
    )
    parsed = response.output_parsed
    if parsed is None:
        status = getattr(response, "status", None)
        raise StructuredOutputError(
            f"OpenAI 응답에 구조화된 결과가 없습니다 (status={status})."
        )
    _record_usage(metrics, response)
    request_id = getattr(response, "_request_id", None)
    if request_id:
        logger.debug("OpenAI request_id=%s", request_id)
    return parsed
