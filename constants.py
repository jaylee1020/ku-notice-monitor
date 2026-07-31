"""프로젝트 전역 상수 정의"""

# 피드 파싱
EMPTY_FEED_SENTINEL = "no exist data"
BOARD_CONTENT_CLASS = "hwp_editor_board_content"

# 본문 수집 (멀티모달 컨텍스트를 위한 넉넉한 상한)
MAX_ARTICLE_BODY_LENGTH = 4000
MAX_IMAGES_PER_ARTICLE = 6
IMAGE_DOWNLOAD_TIMEOUT = 15

# 프롬프트에 포함할 설명 텍스트 절삭 길이
PROMPT_DESCRIPTION_MAX_LENGTH = 2000

# 이미지 필터 (트래킹 픽셀/아이콘 배제)
MIN_IMAGE_URL_LENGTH = 10

# 첨부파일
ATTACHMENT_DOWNLOAD_TIMEOUT = 45
MAX_ATTACHMENT_SIZE = 20 * 1024 * 1024
MAX_TOTAL_MEDIA_SIZE = 45 * 1024 * 1024

# Responses API에 직접 전달하는 미디어 형식.
OPENAI_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
OPENAI_FILE_EXTENSIONS = {
    ".pdf", ".txt", ".md", ".csv", ".tsv", ".html", ".htm", ".xml", ".rtf", ".json",
    ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
}
OPENAI_NATIVE_EXTENSIONS = OPENAI_IMAGE_EXTENSIONS | OPENAI_FILE_EXTENSIONS

# 확장자별 기본 MIME (mimetypes DB가 환경마다 달라서 고정값 테이블 유지)
OPENAI_EXTENSION_MIME_OVERRIDES = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".html": "text/html",
    ".htm": "text/html",
    ".xml": "text/xml",
    ".rtf": "text/rtf",
    ".json": "application/json",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

# 상태 관리
STATE_RETENTION_DAYS = 90

# 네트워크 타임아웃 (초)
FEED_FETCH_TIMEOUT = 15
ARTICLE_BODY_TIMEOUT = 15

# 동시 요청 제한 (멀티모달 처리량 향상 + 서버 과부하/차단 방지)
MAX_CONCURRENT_IMAGE_DOWNLOADS = 8
MAX_CONCURRENT_ATTACHMENT_DOWNLOADS = 5
# 본문 크롤링 동시 요청 제한. 첫 실행 시 모든 공지가 신규로 잡혀
# 수백 건이 동시에 요청되는 것을 막는다.
MAX_CONCURRENT_BODY_FETCHES = 8

# 공지별 독립 분석 동시성. 한 공지 실패가 다른 공지 판정을 오염시키지 않게 한다.
AI_MAX_CONCURRENCY = 4

# 텔레그램
MAX_TELEGRAM_MESSAGE_LENGTH = 4096
