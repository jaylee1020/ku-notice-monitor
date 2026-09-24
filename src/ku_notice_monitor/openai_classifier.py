"""OpenAI 공지별 사실 추출기와 선택적 첨부 분석."""

import asyncio
import base64
import logging
import mimetypes
from dataclasses import dataclass

import aiohttp
from openai import AsyncOpenAI

from .analysis_models import (
    ASSESSMENT_SCHEMA_VERSION,
    AttachmentNeed,
    NoticeAssessment,
)
from .constants import (
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
from .document_extract import (
    DocumentExtractionError,
    extract_hwp_markdown,
    extract_pdf_markdown,
)
from .llm import empty_usage, make_client, openai_configured, parse_structured
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

    @property
    def extracted_text(self) -> str:
        """로컬에서 텍스트로 변환한 첨부만 근거 검증 원문으로 쓸 수 있다."""
        if self.mime_type != "text/markdown":
            return ""
        return self.data.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class MediaDownloadResult:
    payloads: list[MediaPayload]
    failed_names: list[str]


@dataclass(frozen=True)
class AnalysisOutcome:
    """모델 판정과, 판정 근거를 검증할 때 본문 외에 참고할 첨부 추출 텍스트.

    ``partial``은 필요한 첨부 분석이 실패해 1차 판정만 있다는 뜻이다. 이번 실행에는
    1차 판정을 쓰되 호출자가 재분석을 예약해야 한다.
    """

    assessment: NoticeAssessment
    attachment_text: str = ""
    partial: bool = False


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
            "pdf" if attachment.ext == ".pdf" else "file",
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


def _prepare_pdf_payload(filename: str, data: bytes) -> MediaPayload:
    """텍스트 PDF는 Markdown으로 줄이고, 그 외에는 원본을 보존한다."""
    try:
        markdown = extract_pdf_markdown(data)
    except DocumentExtractionError as exc:
        logger.warning("%s 로컬 PDF 변환 실패, 원본 사용: %s", filename, exc)
        markdown = None
    if markdown is None:
        return MediaPayload(filename, "application/pdf", data, "file")
    logger.info("%s를 로컬 Markdown으로 변환했습니다.", filename)
    return MediaPayload(
        f"{filename}.md",
        "text/markdown",
        markdown.encode("utf-8"),
        "file",
    )


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
        if kind == "pdf":
            return await asyncio.to_thread(_prepare_pdf_payload, filename, data)
        return MediaPayload(filename, mime_type, data, kind)

    async with aiohttp.ClientSession(headers=DEFAULT_HEADERS) as session:
        results = await asyncio.gather(
            *(one(session, *item) for item in items),
            return_exceptions=True,
        )

    payloads: list[MediaPayload] = []
    failed_names: list[str] = []
    for item, result in zip(items, results, strict=True):
        if isinstance(result, BaseException):
            logger.debug("미디어 다운로드 실패: %s", result)
            failed_names.append(item[1])
        elif result is not None:
            payloads.append(result)
        else:
            failed_names.append(item[1])
    return MediaDownloadResult(payloads, failed_names)


def _within_media_budget(media: list[MediaPayload]) -> list[MediaPayload]:
    selected: list[MediaPayload] = []
    total_bytes = 0
    for item in media:
        if total_bytes + len(item.data) > MAX_TOTAL_MEDIA_SIZE:
            logger.warning("요청 미디어 총량 제한으로 %s 첨부를 건너뜁니다.", item.filename)
            continue
        total_bytes += len(item.data)
        selected.append(item)
    return selected


def _build_input_content(
    prompt: str,
    media: list[MediaPayload],
    config: dict,
) -> list[dict]:
    content: list[dict] = [{"type": "input_text", "text": prompt}]
    for item in media:
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


async def _call_openai_api(
    client: AsyncOpenAI,
    config: dict,
    content: list[dict],
    metrics: dict | None = None,
) -> NoticeAssessment:
    return await parse_structured(
        client,
        config=config,
        system_prompt=SYSTEM_PROMPT,
        user_content=content,
        output_type=NoticeAssessment,
        cache_key=f"ku-notice:{PROMPT_VERSION}",
        metrics=metrics,
    )


async def _analyze_article(
    client: AsyncOpenAI,
    article: Article,
    config: dict,
    *,
    include_media: bool,
    metrics: dict | None = None,
) -> AnalysisOutcome:
    download = (
        await _download_media(article, config)
        if include_media
        else MediaDownloadResult([], [])
    )
    media = _within_media_budget(download.payloads)
    prompt = build_prompt(
        article,
        build_profile_text(config),
        attachments_included=bool(media),
        unreadable_attachments=download.failed_names,
    )
    assessment = await _call_openai_api(
        client,
        config,
        _build_input_content(prompt, media, config),
        metrics,
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
    attachment_text = "\n".join(
        text for item in media if (text := item.extracted_text)
    )
    return AnalysisOutcome(assessment, attachment_text)


async def _classify_one(
    client: AsyncOpenAI,
    article: Article,
    config: dict,
    semaphore: asyncio.Semaphore,
    metrics: dict | None = None,
) -> tuple[str, AnalysisOutcome] | None:
    async with semaphore:
        try:
            outcome = await _analyze_article(
                client,
                article,
                config,
                include_media=False,
                metrics=metrics,
            )
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

        if not (
            outcome.assessment.attachment_need == AttachmentNeed.REQUIRED
            and _media_items(article)
        ):
            return article.key, outcome

        logger.info("%s: 핵심 판정에 첨부 확인이 필요해 2차 분석합니다.", article.key)
        try:
            return article.key, await _analyze_article(
                client,
                article,
                config,
                include_media=True,
                metrics=metrics,
            )
        except Exception as exc:
            if metrics is not None:
                metrics["partial_articles"] = metrics.get("partial_articles", 0) + 1
            logger.warning(
                "%s 첨부 2차 분석 실패. 1차 판정을 쓰고 재분석을 예약합니다: %s",
                article.key,
                exc,
            )
            assessment = outcome.assessment
            uncertainties = [*assessment.uncertainties, "첨부파일 분석에 실패해 본문만으로 판정함"]
            return article.key, AnalysisOutcome(
                assessment.model_copy(update={"uncertainties": uncertainties[-5:]}),
                partial=True,
            )


async def analyze_with_openai(
    articles: list[Article],
    config: dict,
    *,
    metrics: dict | None = None,
) -> dict[str, AnalysisOutcome]:
    """공지별 독립 분석 결과를 key로 반환한다. 실패한 공지는 결과에서 빠진다."""
    if not openai_configured():
        logger.warning("OPENAI_API_KEY가 없어 규칙 기반 분류로 대체합니다.")
        return {}

    if metrics is not None:
        metrics.update(
            {
                "model": config["ai"]["model"],
                "prompt_version": PROMPT_VERSION,
                "schema_version": ASSESSMENT_SCHEMA_VERSION,
                "articles_requested": len(articles),
                "failed_articles": 0,
                "partial_articles": 0,
                **empty_usage(),
            }
        )
    client = make_client(config)
    semaphore = asyncio.Semaphore(config["ai"].get("max_concurrency", 4))
    try:
        results = await asyncio.gather(
            *(
                _classify_one(client, article, config, semaphore, metrics=metrics)
                for article in articles
            )
        )
    finally:
        await client.close()
    return dict(item for item in results if item is not None)
