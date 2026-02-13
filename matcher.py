"""Gemini API 기반 공지 관련도 분석 모듈"""

import json
import os
import time

from google import genai

from feeds import Article


def build_profile_text(config: dict) -> str:
    """config에서 사용자 프로필 텍스트 생성"""
    p = config["profile"]
    keywords = config.get("keywords", {})
    high = ", ".join(keywords.get("high", []))
    medium = ", ".join(keywords.get("medium", []))

    lines = []
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
    for i, a in enumerate(articles, 1):
        desc = a.description[:300] if a.description else "설명 없음"
        article_list += f"{i}. [{a.board_name}] {a.title} - {desc}\n"

    return f"""당신은 한국 대학생을 위한 공지사항 관련도 분류기입니다.

학생 프로필:
{profile_text}

아래 공지사항 각각에 대해 이 학생과의 관련도를 1-5점으로 평가하고, 한줄 사유를 작성해주세요.
- 5점: 반드시 확인해야 할 공지 (수강신청, 등록금 등 필수 학사 사항)
- 4점: 높은 관련도 (본인 학과/관심 분야 직접 관련)
- 3점: 관련 있을 수 있음 (일반 학생에게 유용한 정보)
- 2점: 낮은 관련도 (특정 대상만 해당)
- 1점: 관련 없음

반드시 아래 JSON 형식으로만 응답해주세요. 다른 텍스트는 포함하지 마세요:
[{{"index": 1, "score": 5, "reason": "사유"}}, ...]

공지사항 목록:
{article_list}"""


def analyze_with_gemini(articles: list[Article], config: dict) -> list[dict]:
    """Gemini API로 공지 관련도 분석. 실패 시 빈 리스트 반환."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return []

    client = genai.Client(api_key=api_key)
    model_name = config["gemini"]["model"]

    profile_text = build_profile_text(config)
    prompt = build_prompt(articles, profile_text)

    for attempt in range(2):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
            )
            text = response.text.strip()
            # JSON 블록이 ```json ... ``` 로 감싸져 있을 수 있음
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            return json.loads(text)
        except json.JSONDecodeError as e:
            print(f"[Gemini 응답 파싱 오류] 시도 {attempt + 1}/2: {e}")
            if attempt == 0:
                print("[재시도] 5초 후 재시도합니다...")
                time.sleep(5)
        except Exception as e:
            print(f"[Gemini API 오류] 시도 {attempt + 1}/2: {e}")
            if attempt == 0:
                print("[재시도] 5초 후 재시도합니다...")
                time.sleep(5)

    return []


def keyword_fallback(articles: list[Article], config: dict) -> list[dict]:
    """Gemini 실패 시 키워드 매칭으로 폴백"""
    keywords = config.get("keywords", {})
    high_keywords = keywords.get("high", [])
    medium_keywords = keywords.get("medium", [])

    results = []
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


def match_articles(articles: list[Article], config: dict) -> list[tuple[Article, int, str]]:
    """
    공지 관련도 분석 후 (Article, score, reason) 튜플 리스트 반환.
    threshold 이상인 공지만 포함, 점수 높은 순 정렬.
    """
    if not articles:
        return []

    threshold = config["gemini"].get("relevance_threshold", 3)

    # Gemini로 분석 시도
    results = analyze_with_gemini(articles, config)

    # 실패 시 키워드 폴백
    if not results:
        print("[INFO] Gemini 분석 실패, 키워드 매칭으로 대체합니다.")
        results = keyword_fallback(articles, config)

    # 결과를 Article과 매칭
    matched = []
    for r in results:
        idx = r.get("index", 0) - 1
        if 0 <= idx < len(articles):
            score = r.get("score", 1)
            reason = r.get("reason", "")
            if score >= threshold:
                matched.append((articles[idx], score, reason))

    matched.sort(key=lambda x: x[1], reverse=True)
    return matched
