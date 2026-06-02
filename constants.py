"""프로젝트 전역 상수 정의"""

# 피드 파싱
EMPTY_FEED_SENTINEL = "no exist data"
BOARD_CONTENT_CLASS = "hwp_editor_board_content"

# 본문 수집 (멀티모달 컨텍스트를 위한 넉넉한 상한)
MAX_ARTICLE_BODY_LENGTH = 4000
MAX_IMAGES_PER_ARTICLE = 6
IMAGE_DOWNLOAD_TIMEOUT = 15

# 프롬프트에 포함할 설명 텍스트 절삭 길이 (Gemini에 전달)
PROMPT_DESCRIPTION_MAX_LENGTH = 2000

# 이미지 필터 (트래킹 픽셀/아이콘 배제)
MIN_IMAGE_URL_LENGTH = 10

# 첨부파일
ATTACHMENT_DOWNLOAD_TIMEOUT = 45
MAX_ATTACHMENT_SIZE = 20 * 1024 * 1024  # 20MB (Gemini inline 한도)

# Gemini가 inline 바이너리로 직접 처리할 수 있는 파일 확장자
# 참고: https://ai.google.dev/gemini-api/docs/file-prompting-strategies
GEMINI_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"}
GEMINI_VIDEO_EXTENSIONS = {".mp4", ".mpeg", ".mov", ".avi", ".flv", ".mpg", ".webm", ".wmv", ".3gp", ".3gpp"}
GEMINI_AUDIO_EXTENSIONS = {".wav", ".mp3", ".aiff", ".aac", ".ogg", ".flac", ".m4a"}
GEMINI_DOCUMENT_EXTENSIONS = {".pdf"}
GEMINI_TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".html", ".htm", ".xml", ".rtf", ".json"}
GEMINI_NATIVE_EXTENSIONS = (
    GEMINI_IMAGE_EXTENSIONS
    | GEMINI_VIDEO_EXTENSIONS
    | GEMINI_AUDIO_EXTENSIONS
    | GEMINI_DOCUMENT_EXTENSIONS
    | GEMINI_TEXT_EXTENSIONS
)

# 확장자별 기본 MIME (mimetypes DB가 환경마다 달라서 고정값 테이블 유지)
GEMINI_EXTENSION_MIME_OVERRIDES = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".mp4": "video/mp4",
    ".mpeg": "video/mpeg",
    ".mpg": "video/mpeg",
    ".mov": "video/mov",
    ".avi": "video/avi",
    ".flv": "video/x-flv",
    ".webm": "video/webm",
    ".wmv": "video/wmv",
    ".3gp": "video/3gpp",
    ".3gpp": "video/3gpp",
    ".wav": "audio/wav",
    ".mp3": "audio/mp3",
    ".aiff": "audio/aiff",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".txt": "text/plain",
    ".md": "text/md",
    ".csv": "text/csv",
    ".html": "text/html",
    ".htm": "text/html",
    ".xml": "text/xml",
    ".rtf": "text/rtf",
    ".json": "application/json",
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

# Gemini 배치 크기
GEMINI_BATCH_SIZE = 10

# 텔레그램
MAX_TELEGRAM_MESSAGE_LENGTH = 4096
