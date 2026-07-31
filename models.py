"""공지사항과 분석 결과 데이터 모델."""

import hashlib
import json
from dataclasses import asdict, dataclass, field


@dataclass
class Attachment:
    filename: str
    url: str

    @property
    def ext(self) -> str:
        """파일 확장자 (소문자, 점 포함). 예: '.hwp', '.pdf'"""
        dot = self.filename.rfind(".")
        return self.filename[dot:].lower() if dot != -1 else ""


@dataclass
class Article:
    id: str
    title: str
    link: str
    pub_date: str
    author: str
    description: str
    board_name: str
    board_id: int
    view_count: int
    is_pinned: bool
    attachment_count: int
    images: list[str] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    is_update: bool = False

    @property
    def key(self) -> str:
        """보드 간 ID 충돌 방지를 위한 고유 키"""
        return f"{self.board_id}:{self.id}"

    @property
    def fingerprint(self) -> str:
        """수정 공지 감지를 위한 안정적인 내용 해시."""
        payload = {
            "title": self.title.strip(),
            "description": self.description.strip(),
            "attachments": sorted(att.filename.strip() for att in self.attachments),
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> "Article":
        data = dict(value)
        data["attachments"] = [
            item if isinstance(item, Attachment) else Attachment(**item)
            for item in data.get("attachments", [])
        ]
        return cls(**data)


@dataclass
class ClassifiedNotice:
    """분리된 판정 축과 최종 전달 정책을 담는 공지."""

    article: Article
    delivery: str
    category: str
    summary: str
    reason: str
    audience_fit: str = "unknown"
    interest_fit: str = "low"
    obligation: str = "none"
    consequence: str = "none"
    deadline: str | None = None
    dates: list[dict] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    benefits: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    uncertainties: list[str] = field(default_factory=list)
    source: str = "openai"

    @property
    def urgency(self) -> str:
        """구형 호출부에서 사용하는 긴급도 표현."""
        if self.delivery in {"immediate", "review"}:
            return "urgent"
        if self.delivery == "digest":
            return "digest"
        return "ignore"

    @property
    def score(self) -> int:
        """구형 표시·상태와의 호환용 파생값. 정책 결정에는 사용하지 않는다."""
        return {
            "immediate": 5,
            "review": 4,
            "digest": 3,
            "suppress": 1,
        }.get(self.delivery, 1)

    def to_dict(self) -> dict:
        return {
            "article": self.article.to_dict(),
            "delivery": self.delivery,
            "category": self.category,
            "reason": self.reason,
            "summary": self.summary,
            "audience_fit": self.audience_fit,
            "interest_fit": self.interest_fit,
            "obligation": self.obligation,
            "consequence": self.consequence,
            "deadline": self.deadline,
            "dates": self.dates,
            "actions": self.actions,
            "benefits": self.benefits,
            "evidence": self.evidence,
            "uncertainties": self.uncertainties,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "ClassifiedNotice":
        data = dict(value)
        data["article"] = Article.from_dict(data["article"])
        if "delivery" not in data:
            urgency = data.pop("urgency", "digest")
            data["delivery"] = "immediate" if urgency == "urgent" else "digest"
            data.setdefault("category", "other")
            data.setdefault("summary", data["article"].title)
            data.setdefault("reason", "이전 분류 결과")
            data.setdefault("source", "legacy")
        data.pop("score", None)
        return cls(**data)


# 외부 호출부 호환을 위한 별칭. 새 코드에서는 ClassifiedNotice를 사용한다.
ArticleMatch = ClassifiedNotice
