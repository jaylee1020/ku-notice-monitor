"""Gemini API 기반 공지 관련도 분석 모듈 (텍스트 + 이미지 멀티모달)"""

import asyncio
import json
import logging
import mimetypes
import os

import aiohttp
from google import genai
from google.genai import types
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from constants import IMAGE_DOWNLOAD_TIMEOUT
from models import Article

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
    has_images = any(a.images for a in articles)
    for i, a in enumerate(articles, 1):
        desc = a.description[:300] if a.description else "설명 없음"
        image_note = f" (이미지 {len(a.images)}장 첨부)" if a.images else ""
        article_list += f"{i}. [{a.board_name}] {a.title} - {desc}{image_note}\n"

    image_instruction = ""
    if has_images:
        image_instruction = (
            "\n일부 공지에는 이미지가 첨부되어 있습니다. "
            "이미지의 내용도 함께 분석하여 관련도를 평가해주세요.\n"
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
{image_instruction}
반드시 아래 JSON 형식으로만 응답해주세요. 다른 텍스트는 포함하지 마세요:
[{{"index": 1, "score": 5, "reason": "사유"}}, ...]

공지사항 목록:
{article_list}"""


def _guess_mime_type(url: str) -> str:
    """URL에서 MIME 타입을 추측. 기본값은 image/jpeg."""
    mime, _ = mimetypes.guess_type(url.split("?")[0])
    if mime and mime.startswith("image/"):
        return mime
    return "image/jpeg"


async def _download_one_image(
    session: aiohttp.ClientSession,
    article_key: str,
    url: str,
) -> tuple[str, types.Part] | None:
    """단일 이미지를 다운로드하여 (article_key, Part) 반환. 실패 시 None."""
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

    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            *(_download_one_image(session, key, url) for key, url in tasks_info),
            return_exceptions=True,
        )

    image_parts: dict[str, list[types.Part]] = {}
    for result in results:
        if isinstance(result, Exception) or result is None:
            continue
        article_key, part = result
        image_parts.setdefault(article_key, []).append(part)

    return image_parts


def _build_multimodal_contents(
    prompt: str,
    articles: list[Article],
    image_parts: dict[str, list[types.Part]],
) -> list:
    """텍스트 프롬프트와 이미지를 결합하여 멀티모달 contents 생성"""
    if not image_parts:
        return [prompt]

    contents: list = [prompt]
    for i, a in enumerate(articles, 1):
        parts = image_parts.get(a.key, [])
        if parts:
            contents.append(types.Part.from_text(text=f"\n--- 공지 {i}번 첨부 이미지 ---"))
            contents.extend(parts)

    return contents


@retry(
    retry=retry_if_exception_type(Exception),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=16),
    reraise=True,
)
def _call_gemini_api(client: genai.Client, model_name: str, contents: list | str) -> list[dict]:
    """Gemini API 호출 (tenacity로 최대 3회 지수 백오프 재시도)"""
    response = client.models.generate_content(
        model=model_name,
        contents=contents,
    )
    text = response.text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(text)


async def analyze_with_gemini(articles: list[Article], config: dict) -> list[dict]:
    """Gemini API로 공지 관련도 분석 (이미지 포함 멀티모달). 실패 시 빈 리스트 반환."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        logger.warning("GEMINI_API_KEY가 설정되지 않았습니다. 키워드 매칭으로 대체됩니다.")
        return []

    client = genai.Client(api_key=api_key)
    model_name = config["gemini"]["model"]
    profile_text = build_profile_text(config)
    prompt = build_prompt(articles, profile_text)

    # 이미지가 있는 공지의 이미지를 다운로드
    image_parts = await _download_images(articles)
    if image_parts:
        image_count = sum(len(parts) for parts in image_parts.values())
        logger.info("이미지 %d장 다운로드 완료, 멀티모달 분석 진행", image_count)

    contents = _build_multimodal_contents(prompt, articles, image_parts)

    try:
        results = await asyncio.to_thread(_call_gemini_api, client, model_name, contents)
        logger.info("Gemini 분석 완료: %d건", len(results))
        return results
    except Exception as e:
        logger.error("Gemini API 호출 최종 실패 (3회 시도): %s", e)
        return []


def keyword_fallback(articles: list[Article], config: dict) -> list[dict]:
    """Gemini 실패 시 키워드 매칭으로 폴백"""
    keywords = config.get("keywords", {})
    high_keywords: list[str] = keywords.get("high", [])
    medium_keywords: list[str] = keywords.get("medium", [])

    results: list[dict] = []
    for i, a in enumerate(articles, 1):
        text = (a.title + " " + a.description).lower()
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
