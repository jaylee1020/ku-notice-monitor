"""Gemini API 기반 공지 관련도 분석 모듈 (텍스트 + 이미지 + 첨부파일 멀티모달)"""

import asyncio
import json
import logging
import mimetypes
import os

import aiohttp
from google import genai
from google.genai import types
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from constants import (
    ATTACHMENT_DOWNLOAD_TIMEOUT,
    GEMINI_BATCH_SIZE,
    GEMINI_EXTENSION_MIME_OVERRIDES,
    GEMINI_IMAGE_EXTENSIONS,
    GEMINI_NATIVE_EXTENSIONS,
    IMAGE_DOWNLOAD_TIMEOUT,
    MAX_ATTACHMENT_SIZE,
    MAX_CONCURRENT_ATTACHMENT_DOWNLOADS,
    MAX_CONCURRENT_IMAGE_DOWNLOADS,
    PROMPT_DESCRIPTION_MAX_LENGTH,
)
from models import Article, Attachment

logger = logging.getLogger(__name__)


def _parse_index(value: object) -> int | None:
    try:
        idx = int(value)
    except (TypeError, ValueError):
        return None
    return idx if idx >= 1 else None


def _parse_score(value: object) -> int | None:
    try:
        score = int(value)
    except (TypeError, ValueError):
        return None
    if 1 <= score <= 5:
        return score
    return None


def build_profile_text(config: dict) -> str:
    """config에서 사용자 프로필 텍스트 생성"""
    p = config["profile"]
    keywords = config.get("keywords", {})
    high = ", ".join(keywords.get("high", []))
    medium = ", ".join(keywords.get("medium", []))

    lines: list[str] = []
    if p.get("major"):
        lines.append(f"학과: {p['major']}")
    if p.get("year"):
        lines.append(f"학년: {p['year']}학년")
    if p.get("campus"):
        lines.append(f"캠퍼스: {p['campus']}")
    if p.get("status"):
        lines.append(f"재학 상태: {p['status']}")
    if high:
        lines.append(f"높은 관심 키워드: {high}")
    if medium:
        lines.append(f"일반 관심 키워드: {medium}")

    return "\n".join(lines) if lines else "프로필 미설정 (모든 공지를 일반적으로 평가)"


def build_prompt(articles: list[Article], profile_text: str) -> str:
    """Gemini에게 보낼 배치 프롬프트 생성"""
    article_list = ""
    has_media = any(a.images or a.attachments for a in articles)
    for i, a in enumerate(articles, 1):
        desc = a.description[:PROMPT_DESCRIPTION_MAX_LENGTH] if a.description else "설명 없음"
        notes: list[str] = []
        if a.images:
            notes.append(f"이미지 {len(a.images)}장")
        if a.attachments:
            filenames = ", ".join(att.filename for att in a.attachments)
            notes.append(f"첨부파일: {filenames}")
        note_str = f" ({'; '.join(notes)})" if notes else ""
        article_list += f"{i}. [{a.board_name}] {a.title} - {desc}{note_str}\n"

    media_instruction = ""
    if has_media:
        media_instruction = (
            "\n일부 공지에는 이미지나 첨부파일(PDF 등)이 포함되어 있습니다. "
            "첨부된 파일의 내용도 함께 분석하여 관련도를 평가해주세요. "
            "첨부파일명 자체도 중요한 단서입니다 (예: '장학금신청양식.hwp'는 장학 관련 공지).\n"
        )

    return f"""당신은 한국 대학생을 위한 공지사항 관련도 분류기입니다.

학생 프로필:
{profile_text}

아래 공지사항 각각에 대해 이 학생과의 관련도를 1-5점으로 평가하고, 한줄 사유를 작성해주세요.
- 5점: 반드시 확인해야 할 공지 (수강신청, 등록금 등 필수 학사 사항)
- 4점: 높은 관련도 (본인 학과/관심 분야 직접 관련)
- 3점: 관련 있을 수 있음 (일반 학생에게 유용한 정보)
- 2점: 낮은 관련도 (특정 대상만 해당)
- 1점: 관련 없음
{media_instruction}
반드시 아래 JSON 형식으로만 응답해주세요. 다른 텍스트는 포함하지 마세요:
[{{"index": 1, "score": 5, "reason": "사유"}}, ...]

공지사항 목록:
{article_list}"""


def _extension_of(name: str) -> str:
    """파일명/URL에서 소문자 확장자를 추출 (쿼리스트링 제거)"""
    clean = name.split("?")[0].split("#")[0]
    dot = clean.rfind(".")
    return clean[dot:].lower() if dot != -1 else ""


def _guess_mime_type(url: str) -> str:
    """이미지 URL에서 MIME 타입을 추측. 기본값은 image/jpeg."""
    ext = _extension_of(url)
    if ext in GEMINI_IMAGE_EXTENSIONS:
        return GEMINI_EXTENSION_MIME_OVERRIDES.get(ext, "image/jpeg")
    mime, _ = mimetypes.guess_type(url.split("?")[0])
    if mime and mime.startswith("image/"):
        return mime
    return "image/jpeg"


def _guess_attachment_mime_type(filename: str) -> str:
    """첨부파일명에서 Gemini 호환 MIME 타입을 추측.

    Gemini가 inline으로 지원하는 포맷(이미지/비디오/오디오/PDF/텍스트)은
    고정 매핑을 우선 사용해 환경별 mimetypes DB 차이를 제거한다.
    """
    ext = _extension_of(filename)
    if ext in GEMINI_EXTENSION_MIME_OVERRIDES:
        return GEMINI_EXTENSION_MIME_OVERRIDES[ext]
    mime, _ = mimetypes.guess_type(filename)
    return mime or "application/octet-stream"


async def _download_one_image(
    session: aiohttp.ClientSession,
    article_key: str,
    url: str,
    semaphore: asyncio.Semaphore,
) -> tuple[str, types.Part] | None:
    """단일 이미지를 다운로드하여 (article_key, Part) 반환. 실패 시 None."""
    async with semaphore:
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=IMAGE_DOWNLOAD_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    logger.debug("이미지 다운로드 실패 (status=%d): %s", resp.status, url)
                    return None
                data = await resp.read()

            mime_type = _guess_mime_type(url)
            part = types.Part.from_bytes(data=data, mime_type=mime_type)
            logger.debug("이미지 다운로드 성공: %s (%d bytes)", url, len(data))
            return article_key, part
        except Exception as e:
            logger.debug("이미지 다운로드 예외: %s - %s", url, e)
            return None


async def _download_images(articles: list[Article]) -> dict[str, list[types.Part]]:
    """공지별 이미지를 병렬 다운로드하여 Gemini Part 객체로 변환. 키는 article.key."""
    tasks_info: list[tuple[str, str]] = []
    for a in articles:
        for url in a.images:
            tasks_info.append((a.key, url))

    if not tasks_info:
        return {}

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_IMAGE_DOWNLOADS)

    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            *(_download_one_image(session, key, url, semaphore) for key, url in tasks_info),
            return_exceptions=True,
        )

    image_parts: dict[str, list[types.Part]] = {}
    for result in results:
        if isinstance(result, Exception) or result is None:
            continue
        article_key, part = result
        image_parts.setdefault(article_key, []).append(part)

    return image_parts


async def _download_one_attachment(
    session: aiohttp.ClientSession,
    article_key: str,
    attachment: Attachment,
    semaphore: asyncio.Semaphore,
) -> tuple[str, Attachment, bytes] | None:
    """Gemini 지원 첨부파일을 다운로드. 실패 시 None."""
    if attachment.ext not in GEMINI_NATIVE_EXTENSIONS:
        return None

    async with semaphore:
        try:
            async with session.get(
                attachment.url,
                timeout=aiohttp.ClientTimeout(total=ATTACHMENT_DOWNLOAD_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    logger.debug("첨부파일 다운로드 실패 (status=%d): %s", resp.status, attachment.filename)
                    return None

                content_length = resp.headers.get("Content-Length")
                if content_length and int(content_length) > MAX_ATTACHMENT_SIZE:
                    logger.debug("첨부파일 크기 초과 (%s bytes): %s", content_length, attachment.filename)
                    return None

                data = await resp.read()
                if len(data) > MAX_ATTACHMENT_SIZE:
                    logger.debug("첨부파일 크기 초과 (%d bytes): %s", len(data), attachment.filename)
                    return None

            logger.debug("첨부파일 다운로드 성공: %s (%d bytes)", attachment.filename, len(data))
            return article_key, attachment, data
        except Exception as e:
            logger.debug("첨부파일 다운로드 예외: %s - %s", attachment.filename, e)
            return None


async def _download_attachments(articles: list[Article]) -> dict[str, list[types.Part]]:
    """공지별 첨부파일(PDF, 이미지)을 병렬 다운로드하여 Gemini Part 객체로 변환"""
    tasks_info: list[tuple[str, Attachment]] = []
    for a in articles:
        for att in a.attachments:
            if att.ext in GEMINI_NATIVE_EXTENSIONS:
                tasks_info.append((a.key, att))

    if not tasks_info:
        return {}

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_ATTACHMENT_DOWNLOADS)

    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            *(
                _download_one_attachment(session, key, att, semaphore)
                for key, att in tasks_info
            ),
            return_exceptions=True,
        )

    attachment_parts: dict[str, list[types.Part]] = {}
    for result in results:
        if isinstance(result, Exception) or result is None:
            continue
        article_key, att, data = result
        mime_type = _guess_attachment_mime_type(att.filename)
        part = types.Part.from_bytes(data=data, mime_type=mime_type)
        attachment_parts.setdefault(article_key, []).append(part)

    return attachment_parts


def _build_multimodal_contents(
    prompt: str,
    articles: list[Article],
    image_parts: dict[str, list[types.Part]],
    attachment_parts: dict[str, list[types.Part]],
) -> list:
    """텍스트 프롬프트와 이미지/첨부파일을 결합하여 멀티모달 contents 생성"""
    if not image_parts and not attachment_parts:
        return [prompt]

    contents: list = [prompt]
    for i, a in enumerate(articles, 1):
        imgs = image_parts.get(a.key, [])
        atts = attachment_parts.get(a.key, [])
        if imgs:
            contents.append(types.Part.from_text(text=f"\n--- 공지 {i}번 첨부 이미지 ---"))
            contents.extend(imgs)
        if atts:
            contents.append(types.Part.from_text(text=f"\n--- 공지 {i}번 첨부파일 ---"))
            contents.extend(atts)

    return contents


def _parse_gemini_json(text: str) -> list[dict]:
    """Gemini 응답에서 JSON을 추출하고 필수 필드를 검증"""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    results = json.loads(text)

    if not isinstance(results, list):
        raise ValueError(f"응답이 JSON 배열이 아닙니다: {type(results)}")

    for r in results:
        if not isinstance(r, dict):
            raise ValueError(f"배열 원소가 객체가 아닙니다: {type(r)}")
        if "index" not in r or "score" not in r:
            raise ValueError(f"필수 필드(index, score) 누락: {r}")

    return results


def _is_retryable_gemini_error(exc: BaseException) -> bool:
    """네트워크/일시적 서버 오류만 재시도. 인증·권한·형식 오류는 재시도하지 않음."""
    # 네트워크/타임아웃은 항상 재시도
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
        return True
    # JSON 파싱/스키마 검증 실패 등은 재시도 불필요
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return False
    # google-genai 예외는 런타임에 상태코드 기반으로 판단 (모듈 모킹 환경 대비 지연 import)
    try:
        from google.genai import errors as genai_errors  # type: ignore
        api_error_cls = getattr(genai_errors, "APIError", None)
        if isinstance(api_error_cls, type) and isinstance(exc, api_error_cls):
            code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
            if code is None:
                return True  # 상태 불명 → 보수적으로 재시도
            if isinstance(code, int):
                # 429(rate limit), 5xx(server error)만 재시도. 4xx(auth/quota/bad request)는 즉시 실패.
                return code == 429 or 500 <= code < 600
            return False
    except Exception:
        pass
    # 그 외 알 수 없는 예외는 재시도하지 않음 (무한 재시도 방지)
    return False


@retry(
    retry=retry_if_exception(_is_retryable_gemini_error),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=16),
    reraise=True,
)
def _call_gemini_api(client: genai.Client, model_name: str, contents: list | str) -> list[dict]:
    """Gemini API 호출 (tenacity로 최대 3회 지수 백오프 재시도, 재시도 가능한 오류만)"""
    response = client.models.generate_content(
        model=model_name,
        contents=contents,
    )
    return _parse_gemini_json(response.text)


async def _analyze_batch(
    client: genai.Client,
    model_name: str,
    articles: list[Article],
    config: dict,
) -> list[dict]:
    """단일 배치의 공지를 분석 (이미지 + 첨부파일 포함)"""
    profile_text = build_profile_text(config)
    prompt = build_prompt(articles, profile_text)

    # 이미지와 첨부파일을 병렬 다운로드
    image_parts, attachment_parts = await asyncio.gather(
        _download_images(articles),
        _download_attachments(articles),
    )

    if image_parts:
        image_count = sum(len(parts) for parts in image_parts.values())
        logger.info("이미지 %d장 다운로드 완료", image_count)
    if attachment_parts:
        att_count = sum(len(parts) for parts in attachment_parts.values())
        logger.info("첨부파일 %d건 다운로드 완료, 멀티모달 분석 진행", att_count)

    contents = _build_multimodal_contents(prompt, articles, image_parts, attachment_parts)
    return await asyncio.to_thread(_call_gemini_api, client, model_name, contents)


async def analyze_with_gemini(articles: list[Article], config: dict) -> list[dict]:
    """Gemini API로 공지 관련도 분석 (배치 분할 + 이미지/첨부파일 포함 멀티모달). 실패 시 빈 리스트 반환."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        logger.warning("GEMINI_API_KEY가 설정되지 않았습니다. 키워드 매칭으로 대체됩니다.")
        return []

    client = genai.Client(api_key=api_key)
    model_name = config["gemini"]["model"]

    all_results: list[dict] = []
    for batch_start in range(0, len(articles), GEMINI_BATCH_SIZE):
        batch = articles[batch_start:batch_start + GEMINI_BATCH_SIZE]
        try:
            batch_results = await _analyze_batch(client, model_name, batch, config)
            # 배치 내 index를 전체 index로 보정
            for r in batch_results:
                idx = _parse_index(r.get("index"))
                if idx is not None:
                    r["index"] = idx + batch_start
            all_results.extend(batch_results)
            logger.info("Gemini 배치 분석 완료: %d/%d건", batch_start + len(batch), len(articles))
        except Exception as e:
            logger.error(
                "Gemini API 배치 호출 최종 실패 (offset=%d, 3회 시도): %s",
                batch_start, e, exc_info=True,
            )
            return []

    return all_results


def keyword_fallback(articles: list[Article], config: dict) -> list[dict]:
    """Gemini 실패 시 키워드 매칭으로 폴백 (첨부파일명도 포함)"""
    keywords = config.get("keywords", {})
    high_keywords: list[str] = keywords.get("high", [])
    medium_keywords: list[str] = keywords.get("medium", [])

    results: list[dict] = []
    for i, a in enumerate(articles, 1):
        # 본문 + 첨부파일명을 합쳐서 키워드 매칭
        attachment_names = " ".join(att.filename for att in a.attachments)
        text = (a.title + " " + a.description + " " + attachment_names).lower()
        score = 1
        reason = "키워드 매칭 없음"

        for kw in high_keywords:
            if kw.lower() in text:
                score = max(score, 4)
                reason = f"키워드 '{kw}' 매칭"
                break

        if score < 4:
            for kw in medium_keywords:
                if kw.lower() in text:
                    score = max(score, 3)
                    reason = f"키워드 '{kw}' 매칭"
                    break

        results.append({"index": i, "score": score, "reason": reason})
    return results


def _extract_matched(
    results: list[dict],
    articles: list[Article],
    threshold: int,
) -> tuple[list[tuple[Article, int, str]], int]:
    """분석 결과에서 threshold 이상인 공지를 추출. (매칭 리스트, 유효 결과 수) 반환."""
    matched: list[tuple[Article, int, str]] = []
    valid_count = 0

    for r in results:
        idx_raw = _parse_index(r.get("index"))
        score = _parse_score(r.get("score"))
        if idx_raw is None or score is None:
            logger.debug("잘못된 분석 결과를 건너뜁니다: %s", r)
            continue

        valid_count += 1
        idx = idx_raw - 1
        if 0 <= idx < len(articles) and score >= threshold:
            reason = str(r.get("reason", ""))
            matched.append((articles[idx], score, reason))

    return matched, valid_count


async def match_articles(articles: list[Article], config: dict) -> tuple[list[tuple[Article, int, str]], str]:
    """
    공지 관련도 분석 후 (Article, score, reason) 튜플 리스트와 분석 방법을 반환.
    threshold 이상인 공지만 포함, 점수 높은 순 정렬.
    반환: (matched_list, method) - method는 "gemini", "keyword", 또는 "none"
    """
    if not articles:
        return [], "none"

    threshold: int = config["gemini"].get("relevance_threshold", 3)

    results = await analyze_with_gemini(articles, config)
    if results:
        method = "gemini"
    else:
        logger.info("Gemini 분석 실패, 키워드 매칭으로 대체합니다.")
        results = keyword_fallback(articles, config)
        method = "keyword"

    matched, valid_count = _extract_matched(results, articles, threshold)

    # Gemini 응답이 있었지만 유효 결과가 하나도 없으면 키워드 매칭으로 재시도
    if method == "gemini" and valid_count == 0:
        logger.info("Gemini 결과 형식이 유효하지 않아 키워드 매칭으로 대체합니다.")
        results = keyword_fallback(articles, config)
        method = "keyword"
        matched, _ = _extract_matched(results, articles, threshold)

    matched.sort(key=lambda x: x[1], reverse=True)
    return matched, method
