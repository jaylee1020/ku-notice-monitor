"""GPT-5.6 Luna 공지별 사실 추출기와 선택적 첨부 분석."""

import asyncio
import base64
import logging
import mimetypes
import os
from dataclasses import dataclass
from typing import Any

import aiohttp
from openai import AsyncOpenAI
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from .analysis_models import (
    ASSESSMENT_SCHEMA_VERSION,
    AttachmentNeed,
    NoticeAssessment,
)
from .constants import (
    AI_MAX_CONCURRENCY,
    ATTACHMENT_DOWNLOAD_TIMEOUT,
    IMAGE_DOWNLOAD_TIMEOUT,
    MAX_ATTACHMENT_SIZE,
    MAX_CONCURRENT_ATTACHMENT_DOWNLOADS,
    MAX_CONCURRENT_IMAGE_DOWNLOADS,
    MAX_IMAGE_SIZE,
    MAX_TOTAL_MEDIA_SIZE,
    OPENAI_EXTENSION_MIME_OVERRIDES,
    OPENAI_FILE_EXTENSIONS,
    OPENAI_HWP_EXTENSIONS,
    OPENAI_IMAGE_EXTENSIONS,
)
from .document_extract import DocumentExtractionError, extract_hwp_markdown
from .models import Article
from .net import (
    DEFAULT_HEADERS,
    allowed_hosts_from_config,
    download_bytes,
    ssl_context_from_config,
)
from .prompts import PROMPT_VERSION, SYSTEM_PROMPT, build_profile_text, build_prompt

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MediaPayload:
    filename: str
    mime_type: str
    data: bytes
    kind: str


@dataclass(frozen=True)
class MediaDownloadResult:
    payloads: list[MediaPayload]
    failed_names: list[str]


def _extension_of(name: str) -> str:
    clean = name.split("?")[0].split("#")[0]
    dot = clean.rfind(".")
    return clean[dot:].lower() if dot != -1 else ""


def _guess_mime_type(name: str, *, image: bool = False) -> str:
    extension = _extension_of(name)
    if extension in OPENAI_EXTENSION_MIME_OVERRIDES:
        return OPENAI_EXTENSION_MIME_OVERRIDES[extension]
    mime_type, _ = mimetypes.guess_type(name.split("?")[0])
    if mime_type:
        return mime_type
    return "image/jpeg" if image else "application/octet-stream"


def _media_items(article: Article) -> list[tuple[str, str, str, str]]:
    items = [
        (
            url,
            url.rsplit("/", 1)[-1] or "notice-image.jpg",
            _guess_mime_type(url, image=True),
            "image",
        )
        for url in article.images
        if _extension_of(url) in OPENAI_IMAGE_EXTENSIONS
    ]
    items.extend(
        (
            attachment.url,
            attachment.filename,
            _guess_mime_type(attachment.filename),
            "file",
        )
        for attachment in article.attachments
        if attachment.ext in OPENAI_FILE_EXTENSIONS
    )
    items.extend(
        (
            attachment.url,
            attachment.filename,
            "application/octet-stream",
            "hwp",
        )
        for attachment in article.attachments
        if attachment.ext in OPENAI_HWP_EXTENSIONS
    )
    return items


async def _download_media(
    article: Article,
    config: dict,
) -> MediaDownloadResult:
    items = _media_items(article)
    if not items:
        return MediaDownloadResult([], [])

    image_semaphore = asyncio.Semaphore(MAX_CONCURRENT_IMAGE_DOWNLOADS)
    file_semaphore = asyncio.Semaphore(MAX_CONCURRENT_ATTACHMENT_DOWNLOADS)
    ssl_context = ssl_context_from_config(config)
    allowed_hosts = allowed_hosts_from_config(config)

    async def one(
        session: aiohttp.ClientSession,
        url: str,
        filename: str,
        mime_type: str,
        kind: str,
    ) -> MediaPayload | None:
        data = await download_bytes(
            session,
            url,
            ssl_context=ssl_context,
            timeout=IMAGE_DOWNLOAD_TIMEOUT if kind == "image" else ATTACHMENT_DOWNLOAD_TIMEOUT,
            allowed_hosts=allowed_hosts,
            semaphore=image_semaphore if kind == "image" else file_semaphore,
            max_size=MAX_IMAGE_SIZE if kind == "image" else MAX_ATTACHMENT_SIZE,
            expected_content_prefix="image/" if kind == "image" else None,
        )
        if data is None:
            return None
        if kind == "hwp":
            try:
                markdown = await asyncio.to_thread(
                    extract_hwp_markdown,
                    data,
                    _extension_of(filename),
                )
            except DocumentExtractionError as exc:
                logger.warning("%s 변환 실패: %s", filename, exc)
                return None
            return MediaPayload(
                f"{filename}.md",
                "text/markdown",
                markdown.encode("utf-8"),
                "file",
            )
        return MediaPayload(filename, mime_type, data, kind)

    async with aiohttp.ClientSession(headers=DEFAULT_HEADERS) as session:
        results = await asyncio.gather(
            *(one(session, *item) for item in items),
            return_exceptions=True,
        )

    payloads: list[MediaPayload] = []
    failed_names: list[str] = []
    for item, result in zip(items, results):
        if isinstance(result, BaseException):
            logger.debug("미디어 다운로드 실패: %s", result)
            failed_names.append(item[1])
        elif result is not None:
            payloads.append(result)
        else:
            failed_names.append(item[1])
    return MediaDownloadResult(payloads, failed_names)


def _build_input_content(
    prompt: str,
    media: list[MediaPayload],
    config: dict,
) -> list[dict]:
    content: list[dict] = [{"type": "input_text", "text": prompt}]
    total_bytes = 0
    for item in media:
        if total_bytes + len(item.data) > MAX_TOTAL_MEDIA_SIZE:
            logger.warning("요청 미디어 총량 제한으로 %s 첨부를 건너뜁니다.", item.filename)
            continue
        total_bytes += len(item.data)
        encoded = base64.b64encode(item.data).decode("ascii")
        data_url = f"data:{item.mime_type};base64,{encoded}"
        if item.kind == "image":
            content.append(
                {
                    "type": "input_image",
                    "image_url": data_url,
                    "detail": config["ai"].get("image_detail", "low"),
                }
            )
            continue
        file_item = {
            "type": "input_file",
            "filename": item.filename,
            "file_data": data_url,
        }
        if item.mime_type == "application/pdf":
            file_item["detail"] = config["ai"].get("file_detail", "low")
        content.append(file_item)
    return content


def _is_retryable_openai_error(exc: BaseException) -> bool:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
        return True
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return False
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and (status == 429 or 500 <= status < 600)


@retry(
    retry=retry_if_exception(_is_retryable_openai_error),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=16),
    reraise=True,
)
async def _call_openai_api(
    client: AsyncOpenAI,
    *,
    model_name: str,
    reasoning_effort: str,
    content: list[dict],
    metrics: dict | None = None,
) -> NoticeAssessment:
    if metrics is not None:
        metrics["request_attempts"] = metrics.get("request_attempts", 0) + 1
    request_input: Any = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]
    reasoning: Any = {"effort": reasoning_effort}
    response = await client.responses.parse(
        model=model_name,
        input=request_input,
        reasoning=reasoning,
        text_format=NoticeAssessment,
        store=False,
    )
    if response.output_parsed is None:
        raise ValueError("OpenAI 응답에 구조화된 분석 결과가 없습니다.")
    usage = getattr(response, "usage", None)
    if usage is not None:
        input_tokens = getattr(usage, "input_tokens", 0) or 0
        output_tokens = getattr(usage, "output_tokens", 0) or 0
        total_tokens = getattr(usage, "total_tokens", 0) or 0
        input_details = getattr(usage, "input_tokens_details", None)
        cached_tokens = getattr(input_details, "cached_tokens", 0) or 0
        logger.info(
            "OpenAI 사용량: input=%s, output=%s, total=%s",
            input_tokens,
            output_tokens,
            total_tokens,
        )
        if metrics is not None:
            metrics["successful_calls"] = metrics.get("successful_calls", 0) + 1
            metrics["input_tokens"] = metrics.get("input_tokens", 0) + input_tokens
            metrics["output_tokens"] = metrics.get("output_tokens", 0) + output_tokens
            metrics["total_tokens"] = metrics.get("total_tokens", 0) + total_tokens
            metrics["cached_input_tokens"] = (
                metrics.get("cached_input_tokens", 0) + cached_tokens
            )
    request_id = getattr(response, "_request_id", None)
    if request_id:
        logger.info("OpenAI request_id=%s", request_id)
    return response.output_parsed


async def _analyze_article(
    client: AsyncOpenAI,
    article: Article,
    config: dict,
    *,
    include_media: bool,
    metrics: dict | None = None,
) -> NoticeAssessment:
    download = (
        await _download_media(article, config)
        if include_media
        else MediaDownloadResult([], [])
    )
    prompt = build_prompt(
        article,
        build_profile_text(config),
        attachments_included=bool(download.payloads),
        unreadable_attachments=download.failed_names,
    )
    assessment = await _call_openai_api(
        client,
        model_name=config["ai"]["model"],
        reasoning_effort=config["ai"].get("reasoning_effort", "medium"),
        content=_build_input_content(prompt, download.payloads, config),
        metrics=metrics,
    )
    if download.failed_names:
        uncertainty = (
            "다음 첨부파일을 안전하게 읽지 못함: "
            + ", ".join(download.failed_names[:3])
        )
        assessment = assessment.model_copy(
            update={
                "uncertainties": [*assessment.uncertainties, uncertainty][:5],
            }
        )
    return assessment


async def _classify_one(
    client: AsyncOpenAI,
    article: Article,
    config: dict,
    semaphore: asyncio.Semaphore,
    metrics: dict | None = None,
) -> tuple[str, dict] | None:
    async with semaphore:
        try:
            assessment = await _analyze_article(
                client,
                article,
                config,
                include_media=False,
                metrics=metrics,
            )
            if (
                assessment.attachment_need == AttachmentNeed.REQUIRED
                and _media_items(article)
            ):
                logger.info("%s: 핵심 판정에 첨부 확인이 필요해 2차 분석합니다.", article.key)
                assessment = await _analyze_article(
                    client,
                    article,
                    config,
                    include_media=True,
                    metrics=metrics,
                )
            return article.key, assessment.model_dump(mode="json")
        except Exception as exc:
            if metrics is not None:
                metrics["failed_articles"] = metrics.get("failed_articles", 0) + 1
            logger.error(
                "%s OpenAI 분석 실패. 이 공지만 규칙 기반으로 대체합니다: %s",
                article.key,
                exc,
                exc_info=True,
            )
            return None


async def analyze_with_openai(
    articles: list[Article],
    config: dict,
    *,
    metrics: dict | None = None,
) -> dict[str, dict]:
    """공지별 독립 분석 결과를 key로 반환한다."""
    if not os.environ.get("OPENAI_API_KEY"):
        logger.warning("OPENAI_API_KEY가 없어 규칙 기반 분류로 대체합니다.")
        return {}

    if metrics is not None:
        metrics.update(
            {
                "model": config["ai"]["model"],
                "prompt_version": PROMPT_VERSION,
                "schema_version": ASSESSMENT_SCHEMA_VERSION,
                "articles_requested": len(articles),
                "request_attempts": 0,
                "successful_calls": 0,
                "failed_articles": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cached_input_tokens": 0,
            }
        )
    client = AsyncOpenAI(
        api_key=os.environ["OPENAI_API_KEY"],
        timeout=config["ai"].get("request_timeout_seconds", 45),
        max_retries=0,
    )
    semaphore = asyncio.Semaphore(
        config["ai"].get("max_concurrency", AI_MAX_CONCURRENCY)
    )
    results = await asyncio.gather(
        *(
            _classify_one(
                client,
                article,
                config,
                semaphore,
                metrics=metrics,
            )
            for article in articles
        )
    )
    return dict(item for item in results if item is not None)
