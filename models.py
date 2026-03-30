"""공지사항 데이터 모델"""

from dataclasses import dataclass, field


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

    @property
    def key(self) -> str:
        """보드 간 ID 충돌 방지를 위한 고유 키"""
        return f"{self.board_id}:{self.id}"
