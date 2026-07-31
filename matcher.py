"""공지 사실 추출과 결정론적 전달 정책 조율."""

import logging
import re
from datetime import date, datetime

from analysis_models import NoticeAssessment
from classification import Delivery, classify_assessment
from feeds import parse_pub_date
from models import Article, ClassifiedNotice
from openai_classifier import analyze_with_openai
from prompts import build_profile_text, build_prompt

logger = logging.getLogger(__name__)

_REQUIRED_TERMS = {"필수", "의무", "수강신청", "등록금", "휴학", "복학", "졸업"}
_ACADEMIC_RISK_TERMS = {"수강신청", "학사경고", "졸업", "휴학", "복학", "학점"}
_FINANCIAL_TERMS = {"등록금", "납부", "환불"}
_SCHOLARSHIP_TERMS = {"장학", "학자금"}
_CAREER_TERMS = {"채용", "인턴", "취업", "현장실습"}
_INTERNATIONAL_TERMS = {"교환학생", "국제교류", "어학연수", "유학"}
_DATE_PATTERN = re.compile(r"(20\d{2})[.\-/년]\s*(\d{1,2})[.\-/월]\s*(\d{1,2})일?")


def _sort_date(article: Article) -> datetime:
    if not article.pub_date:
        return datetime.min
    try:
        return parse_pub_date(article.pub_date)
    except (ValueError, TypeError):
        return datetime.min


def _text(article: Article) -> str:
    attachments = " ".join(item.filename for item in article.attachments)
    return f"{article.board_name} {article.title} {article.description} {attachments}"


def _find_keywords(text: str, keywords: list[str]) -> list[str]:
    lowered = text.lower()
    return [keyword for keyword in keywords if keyword.lower() in lowered]


def _fallback_category(text: str) -> str:
    if any(term in text for term in _SCHOLARSHIP_TERMS):
        return "scholarship"
    if any(term in text for term in _CAREER_TERMS):
        return "career"
    if any(term in text for term in _INTERNATIONAL_TERMS):
        return "international"
    if any(term in text for term in _FINANCIAL_TERMS):
        return "tuition"
    if any(term in text for term in _ACADEMIC_RISK_TERMS | {"학사", "수업"}):
        return "academic"
    if "행사" in text or "특강" in text:
        return "event"
    return "other"


def _fallback_dates(text: str) -> list[dict]:
    dates: list[dict] = []
    for match in _DATE_PATTERN.finditer(text):
        try:
            parsed = date(
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
            )
        except ValueError:
            continue
        context = text[max(0, match.start() - 20):match.end() + 20]
        kind = (
            "payment_deadline"
            if "납부" in context
            else "application_deadline"
            if any(term in context for term in ("마감", "까지", "신청"))
            else "other"
        )
        dates.append(
            {
                "kind": kind,
                "date": parsed.isoformat(),
                "label": context.strip()[:80] or "공지에 명시된 날짜",
            }
        )
    return dates[:8]


def keyword_fallback(article: Article, config: dict) -> NoticeAssessment:
    """API 장애 시 false negative를 줄이는 보수적 단일 공지 추출."""
    text = _text(article)
    high = _find_keywords(text, config.get("keywords", {}).get("high", []))
    medium = _find_keywords(text, config.get("keywords", {}).get("medium", []))
    profile = config.get("profile", {})
    profile_terms = [
        str(value)
        for key, value in profile.items()
        if key in {"major", "previous_major", "campus", "status"} and value
    ]
    audience_matches = _find_keywords(text, profile_terms)
    required = any(term in text for term in _REQUIRED_TERMS)

    consequence = "none"
    if any(term in text for term in _ACADEMIC_RISK_TERMS):
        consequence = "academic_risk"
    elif any(term in text for term in _FINANCIAL_TERMS):
        consequence = "financial_loss"
    elif any(term in text for term in _SCHOLARSHIP_TERMS | _CAREER_TERMS | _INTERNATIONAL_TERMS):
        consequence = "missed_opportunity"

    matched = high or medium
    evidence = [f"키워드 일치: {item}" for item in (high + medium + audience_matches)[:5]]
    dates = _fallback_dates(text)
    actions = []
    if required:
        actions.append(
            {
                "label": "공지 원문에서 본인 대상 여부와 필요한 절차 확인",
                "required": True,
                "deadline": next(
                    (
                        item["date"]
                        for item in dates
                        if item["kind"].endswith("deadline")
                    ),
                    None,
                ),
            }
        )

    return NoticeAssessment.model_validate(
        {
            "category": _fallback_category(text),
            "summary": article.title,
            "audience_fit": "eligible" if audience_matches else "unknown",
            "audience_reason": (
                f"프로필 표현 일치: {', '.join(audience_matches)}"
                if audience_matches
                else "규칙만으로 대상 조건을 확인할 수 없음"
            ),
            "interest_fit": "high" if high else ("medium" if medium else "low"),
            "interest_reason": (
                f"관심 키워드 일치: {', '.join(matched)}"
                if matched
                else "설정된 관심 키워드와 일치하지 않음"
            ),
            "obligation": "required" if required else "optional" if matched else "none",
            "consequence": consequence,
            "dates": dates,
            "actions": actions,
            "benefits": [],
            "evidence": evidence,
            "uncertainties": ["OpenAI 분석 실패로 대상 조건을 완전히 확인하지 못함"],
            "attachment_need": "useful" if article.attachments else "not_needed",
        }
    )


async def match_articles(
    articles: list[Article],
    config: dict,
) -> tuple[list[ClassifiedNotice], str]:
    """모델 추출과 정책 엔진을 결합하고 숨김 결과는 반환하지 않는다."""
    if not articles:
        return [], "none"

    openai_results = await analyze_with_openai(articles, config)
    used_openai = False
    used_rules = False
    classified: list[ClassifiedNotice] = []
    action_window_days = config.get("classification", {}).get("action_window_days", 21)

    for article in articles:
        raw = openai_results.get(article.key)
        if raw is None:
            assessment = keyword_fallback(article, config)
            source = "rules"
            used_rules = True
        else:
            try:
                assessment = NoticeAssessment.model_validate(raw)
                source = "openai"
                used_openai = True
            except (TypeError, ValueError) as exc:
                logger.warning("%s 구조화 결과 검증 실패, 규칙으로 대체: %s", article.key, exc)
                assessment = keyword_fallback(article, config)
                source = "rules"
                used_rules = True

        result = classify_assessment(
            article,
            assessment,
            source=source,
            action_window_days=action_window_days,
        )
        if result.delivery != Delivery.SUPPRESS:
            classified.append(result)

    method = (
        "openai+rules"
        if used_openai and used_rules
        else "openai"
        if used_openai
        else "rules"
    )
    priority = {"immediate": 3, "review": 2, "digest": 1}
    classified.sort(
        key=lambda item: (
            priority.get(item.delivery, 0),
            _sort_date(item.article),
        ),
        reverse=True,
    )
    return classified, method


__all__ = [
    "analyze_with_openai",
    "build_profile_text",
    "build_prompt",
    "keyword_fallback",
    "match_articles",
]
