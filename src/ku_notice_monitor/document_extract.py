"""신뢰 경계 밖의 문서 형식을 격리된 프로세스에서 텍스트로 변환한다."""

import subprocess
import sys
import tempfile
from pathlib import Path

from .constants import MAX_EXTRACTED_DOCUMENT_LENGTH


class DocumentExtractionError(RuntimeError):
    """문서를 안전하게 변환하지 못했을 때 발생한다."""


def extract_hwp_markdown(data: bytes, extension: str, timeout: int = 20) -> str:
    """HWP/HWPX를 별도 프로세스에서 Markdown으로 변환한다."""
    if extension not in {".hwp", ".hwpx"}:
        raise ValueError(f"지원하지 않는 한글 문서 확장자입니다: {extension}")

    with tempfile.TemporaryDirectory(prefix="ku-notice-hwp-") as temp_dir:
        source = Path(temp_dir) / f"document{extension}"
        source.write_bytes(data)
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "syhwp", str(source)],
                cwd=temp_dir,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise DocumentExtractionError("HWP/HWPX 변환 시간이 초과되었습니다.") from exc

    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()[:300]
        raise DocumentExtractionError(
            f"HWP/HWPX 변환 실패(code={completed.returncode}): {detail or '상세 없음'}"
        )
    markdown = completed.stdout.decode("utf-8", errors="replace").strip()
    if not markdown:
        raise DocumentExtractionError("HWP/HWPX에서 텍스트를 추출하지 못했습니다.")
    if len(markdown) > MAX_EXTRACTED_DOCUMENT_LENGTH:
        half = MAX_EXTRACTED_DOCUMENT_LENGTH // 2
        markdown = (
            markdown[:half]
            + "\n\n[중간 내용 생략]\n\n"
            + markdown[-half:]
        )
    return markdown
