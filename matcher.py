"""Gemini API 기반 공지 관련도 분석 모듈 (텍스트 + 이미지 + 첨부파일 멀티모달)"""

import asyncio
import json
import logging
import mimetypes
import os
import re
import ssl
from datetime import datetime

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
from feeds import parse_pub_date
from models import Article
from net import DEFAULT_HEADERS, download_bytes, ssl_context_from_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 결과 파싱 헬퍼
# ---------------------------------------------------------------------------


def _sort_date(article: Article) -> datetime:
    """정렬용 발행일시. 파싱 불가/누락 시 가장 오래된 값으로 취급."""
    if not article.pub_date:
        return datetime.min
    try:
        return parse_pub_date(article.pub_date)
    except (ValueError, TypeError):
        return datetime.min


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
    return score if 1 <= score <= 5 else None


# ---------------------------------------------------------------------------
# 프롬프트 생성
# ---------------------------------------------------------------------------


def build_profile_text(config: dict) -> str:
    """config에서 사용자 프로필 텍스트를 생성한다."""
    p = config["profile"]
    keywords = config.get("keywords", {})

    lines: list[str] = []
    if p.get("major"):
        lines.append(f"학과: {p['major']}")
    if p.get("year"):
        lines.append(f"학년: {p['year']}학년")
    if p.get("campus"):
        lines.append(f"캠퍼스: {p['campus']}")
    if p.get("status"):
        lines.append(f"재학 상태: {p['status']}")
    if keywords.get("high"):
        lines.append(f"높은 관심 키워드: {', '.join(keywords['high'])}")
    if keywords.get("medium"):
        lines.append(f"일반 관심 키워드: {', '.join(keywords['medium'])}")

    return "\n".join(lines) if lines else "프로필 미설정 (모든 공지를 일반적으로 평가)"


def build_prompt(articles: list[Article], profile_text: str) -> str:
    """Gemini에게 보낼 배치 프롬프트를 생성한다."""
    has_media = any(a.images or a.attachments for a in articles)

    article_lines: list[str] = []
    for i, a in enumerate(articles, 1):
        desc = a.description[:PROMPT_DESCRIPTION_MAX_LENGTH] if a.description else "설명 없음"
        notes: list[str] = []
        if a.images:
            notes.append(f"이미지 {len(a.images)}장")
        if a.attachments:
            notes.append(f"첨부파일: {', '.join(att.filename for att in a.attachments)}")
        note_str = f" ({'; '.join(notes)})" if notes else ""
        article_lines.append(f"{i}. [{a.board_name}] {a.title} - {desc}{note_str}")

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
{chr(10).join(article_lines)}
"""


# ---------------------------------------------------------------------------
# MIME 타입 추론
# ---------------------------------------------------------------------------


def _extension_of(name: str) -> str:
    """파일명/URL에서 소문자 확장자를 추출한다 (쿼리/프래그먼트 제거)."""
    clean = name.split("?")[0].split("#")[0]
    dot = clean.rfind(".")
    return clean[dot:].lower() if dot != -1 else ""


def _guess_mime_type(url: str) -> str:
    """이미지 URL에서 MIME 타입을 추측한다. 기본값은 image/jpeg."""
    ext = _extension_of(url)
    if ext in GEMINI_IMAGE_EXTENSIONS:
        return GEMINI_EXTENSION_MIME_OVERRIDES.get(ext, "image/jpeg")
    mime, _ = mimetypes.guess_type(url.split("?")[0])
    return mime if mime and mime.startswith("image/") else "image/jpeg"


def _guess_attachment_mime_type(filename: str) -> str:
    """첨부파일명에서 Gemini 호환 MIME 타입을 추측한다.

    Gemini가 inline으로 지원하는 포맷은 고정 매핑을 우선 사용해
    환경별 mimetypes DB 차이를 제거한다.
    """
    ext = _extension_of(filename)
    if ext in GEMINI_EXTENSION_MIME_OVERRIDES:
        return GEMINI_EXTENSION_MIME_OVERRIDES[ext]
    mime, _ = mimetypes.guess_type(filename)
    return mime or "application/octet-stream"


# ---------------------------------------------------------------------------
# 멀티모달 입력 다운로드
# ---------------------------------------------------------------------------


async def _download_media(
    items: list[tuple[str, str, str]],
    ssl_context: ssl.SSLContext,
    *,
    max_concurrent: int,
    timeout: int,
    max_size: int | None = None,
) -> dict[str, list[types.Part]]:
    """(article_key, url, mime_type) 목록을 병렬 다운로드해 공지별 Part 리스트로 묶는다."""
    if not items:
        return {}

    semaphore = asyncio.Semaphore(max_concurrent)

    async def one(session, key: str, url: str, mime: str) -> tuple[str, types.Part] | None:
        data = await download_bytes(
            session, url, ssl_context=ssl_context, timeout=timeout,
            semaphore=semaphore, max_size=max_size,
        )
        if data is None:
            return None
        return key, types.Part.from_bytes(data=data, mime_type=mime)

    async with aiohttp.ClientSession(headers=DEFAULT_HEADERS) as session:
        results = await asyncio.gather(
            *(one(session, key, url, mime) for key, url, mime in items),
            return_exceptions=True,
        )

    parts: dict[str, list[types.Part]] = {}
    for result in results:
        if isinstance(result, Exception) or result is None:
            continue
        key, part = result
        parts.setdefault(key, []).append(part)
    return parts


def _image_items(articles: list[Article]) -> list[tuple[str, str, str]]:
    return [(a.key, url, _guess_mime_type(url)) for a in articles for url in a.images]


def _attachment_items(articles: list[Article]) -> list[tuple[str, str, str]]:
    return [
        (a.key, att.url, _guess_attachment_mime_type(att.filename))
        for a in articles
        for att in a.attachments
        if att.ext in GEMINI_NATIVE_EXTENSIONS
    ]


def _build_multimodal_contents(
    prompt: str,
    articles: list[Article],
    image_parts: dict[str, list[types.Part]],
    attachment_parts: dict[str, list[types.Part]],
) -> list:
    """텍스트 프롬프트와 이미지/첨부파일을 결합하여 멀티모달 contents를 생성한다."""
    if not image_parts and not attachment_parts:
        return [prompt]

    contents: list = [prompt]
    for i, a in enumerate(articles, 1):
        if imgs := image_parts.get(a.key):
            contents.append(types.Part.from_text(text=f"\n--- 공지 {i}번 첨부 이미지 ---"))
            contents.extend(imgs)
        if atts := attachment_parts.get(a.key):
            contents.append(types.Part.from_text(text=f"\n--- 공지 {i}번 첨부파일 ---"))
            contents.extend(atts)
    return contents


# ---------------------------------------------------------------------------
# Gemini 호출
# ---------------------------------------------------------------------------


def _parse_gemini_json(text: str | None) -> list[dict]:
    """Gemini 응답에서 JSON을 추출하고 필수 필드를 검증한다."""
    if not text:
        raise ValueError("Gemini 응답이 비어 있습니다 (None 또는 빈 문자열).")

    text = text.strip()
    if text.startswith("```"):
        # ```json\n...\n``` 와 한 줄짜리 ```[...]``` 를 모두 처리.
        text = text.strip("`").strip()
        text = re.sub(r"^[a-zA-Z]+\s+", "", text, count=1).strip()

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
    """네트워크/일시적 서버 오류만 재시도. 인증·권한·형식 오류는 재시도하지 않는다."""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
        return True
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
                # 429(rate limit), 5xx(server error)만 재시도. 4xx는 즉시 실패.
                return code == 429 or 500 <= code < 600
            return False
    except Exception:
        pass
    return False  # 알 수 없는 예외는 무한 재시도 방지를 위해 재시도하지 않음


@retry(
    retry=retry_if_exception(_is_retryable_gemini_error),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=16),
    reraise=True,
)
def _call_gemini_api(client: genai.Client, model_name: str, contents: list | str) -> list[dict]:
    """Gemini API 호출 (tenacity로 최대 3회 지수 백오프 재시도, 재시도 가능한 오류만)"""
    response = client.models.generate_content(model=model_name, contents=contents)
    return _parse_gemini_json(response.text)


async def _analyze_batch(
    client: genai.Client,
    model_name: str,
    articles: list[Article],
    config: dict,
) -> list[dict]:
    """단일 배치의 공지를 분석한다 (이미지 + 첨부파일 포함)."""
    prompt = build_prompt(articles, build_profile_text(config))

    # 본문 크롤링과 동일한 SSL 정책으로 다운로드해야
    # ssl_verify=false 환경에서도 멀티모달 입력이 누락되지 않는다.
    ssl_context = ssl_context_from_config(config)

    image_parts, attachment_parts = await asyncio.gather(
        _download_media(
            _image_items(articles), ssl_context,
            max_concurrent=MAX_CONCURRENT_IMAGE_DOWNLOADS, timeout=IMAGE_DOWNLOAD_TIMEOUT,
        ),
        _download_media(
            _attachment_items(articles), ssl_context,
            max_concurrent=MAX_CONCURRENT_ATTACHMENT_DOWNLOADS, timeout=ATTACHMENT_DOWNLOAD_TIMEOUT,
            max_size=MAX_ATTACHMENT_SIZE,
        ),
    )

    if image_parts:
        logger.info("이미지 %d장 다운로드 완료", sum(len(p) for p in image_parts.values()))
    if attachment_parts:
        logger.info(
            "첨부파일 %d건 다운로드 완료, 멀티모달 분석 진행",
            sum(len(p) for p in attachment_parts.values()),
        )

    contents = _build_multimodal_contents(prompt, articles, image_parts, attachment_parts)
    return await asyncio.to_thread(_call_gemini_api, client, model_name, contents)


async def analyze_with_gemini(articles: list[Article], config: dict) -> list[dict]:
    """Gemini API로 공지 관련도를 분석한다 (배치 분할 + 멀티모달). 전체 실패 시 빈 리스트."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        logger.warning("GEMINI_API_KEY가 설정되지 않았습니다. 키워드 매칭으로 대체됩니다.")
        return []

    client = genai.Client(api_key=api_key)
    model_name = config["gemini"]["model"]

    all_results: list[dict] = []
    any_success = False
    for batch_start in range(0, len(articles), GEMINI_BATCH_SIZE):
        batch = articles[batch_start:batch_start + GEMINI_BATCH_SIZE]
        try:
            batch_results = await _analyze_batch(client, model_name, batch, config)
            any_success = True
            logger.info("Gemini 배치 분석 완료: %d/%d건", batch_start + len(batch), len(articles))
        except Exception as e:
            # 한 배치가 실패해도 성공한 배치는 유지하고, 실패한 배치만 키워드 매칭으로 대체해
            # 해당 공지가 누락되지 않게 한다.
            logger.error(
                "Gemini API 배치 호출 최종 실패 (offset=%d, 3회 시도). 키워드 매칭으로 대체: %s",
                batch_start, e, exc_info=True,
            )
            batch_results = keyword_fallback(batch, config)

        # 배치 내 index를 전체 index로 보정 (Gemini/키워드 결과 모두 1-based).
        # 배치 범위를 벗어난 index(환각)는 다른 배치의 공지로 잘못 매핑되므로 버린다.
        for r in batch_results:
            idx = _parse_index(r.get("index"))
            if idx is None or idx > len(batch):
                logger.debug("배치 범위를 벗어난 분석 결과를 건너뜁니다 (offset=%d): %s", batch_start, r)
                continue
            r["index"] = idx + batch_start
            all_results.append(r)

    # 모든 배치가 실패했다면 빈 리스트를 반환해 호출부가 전체 키워드 폴백으로 처리하게 한다.
    return all_results if any_success else []


def keyword_fallback(articles: list[Article], config: dict) -> list[dict]:
    """Gemini 실패 시 키워드 매칭으로 폴백한다 (첨부파일명도 포함)."""
    keywords = config.get("keywords", {})
    high_keywords: list[str] = keywords.get("high", [])
    medium_keywords: list[str] = keywords.get("medium", [])

    results: list[dict] = []
    for i, a in enumerate(articles, 1):
        attachment_names = " ".join(att.filename for att in a.attachments)
        text = f"{a.title} {a.description} {attachment_names}".lower()
        score, reason = 1, "키워드 매칭 없음"

        for kw in high_keywords:
            if kw.lower() in text:
                score, reason = 4, f"키워드 '{kw}' 매칭"
                break
        else:
            for kw in medium_keywords:
                if kw.lower() in text:
                    score, reason = 3, f"키워드 '{kw}' 매칭"
                    break

        results.append({"index": i, "score": score, "reason": reason})
    return results


# ---------------------------------------------------------------------------
# 매칭 (분석 + 폴백 조율)
# ---------------------------------------------------------------------------


def _extract_matched(
    results: list[dict],
    articles: list[Article],
    threshold: int,
) -> tuple[list[tuple[Article, int, str]], int]:
    """분석 결과에서 threshold 이상인 공지를 추출한다. (매칭 리스트, 유효 결과 수) 반환."""
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
            matched.append((articles[idx], score, str(r.get("reason", ""))))

    return matched, valid_count


async def match_articles(
    articles: list[Article], config: dict
) -> tuple[list[tuple[Article, int, str]], str]:
    """공지 관련도를 분석해 (Article, score, reason) 리스트와 분석 방법을 반환한다.

    threshold 이상인 공지만, 점수 내림차순(동점이면 최신 우선)으로 정렬한다.
    method는 "gemini", "keyword", 또는 "none".
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

    # Gemini 응답은 있었지만 유효 결과가 하나도 없으면 키워드 매칭으로 재시도
    if method == "gemini" and valid_count == 0:
        logger.info("Gemini 결과 형식이 유효하지 않아 키워드 매칭으로 대체합니다.")
        results = keyword_fallback(articles, config)
        method = "keyword"
        matched, _ = _extract_matched(results, articles, threshold)

    matched.sort(key=lambda x: (x[1], _sort_date(x[0])), reverse=True)
    return matched, method
