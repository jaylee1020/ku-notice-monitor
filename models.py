"""공지사항 데이터 모델"""

from dataclasses import dataclass, field


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

    @property
    def key(self) -> str:
        """보드 간 ID 충돌 방지를 위한 고유 키"""
        return f"{self.board_id}:{self.id}"
