"""신뢰 경계 밖의 문서 형식을 격리된 프로세스에서 텍스트로 변환한다."""

import subprocess
import sys
import tempfile
from pathlib import Path

from .constants import MAX_EXTRACTED_DOCUMENT_LENGTH

PDF_NATIVE_FALLBACK_EXIT_CODE = 3


class DocumentExtractionError(RuntimeError):
    """문서를 안전하게 변환하지 못했을 때 발생한다."""


def _truncate_extracted_document(markdown: str) -> str:
    if len(markdown) <= MAX_EXTRACTED_DOCUMENT_LENGTH:
        return markdown
    half = MAX_EXTRACTED_DOCUMENT_LENGTH // 2
    return markdown[:half] + "\n\n[중간 내용 생략]\n\n" + markdown[-half:]


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
    return _truncate_extracted_document(markdown)


def extract_pdf_markdown(data: bytes, timeout: int = 20) -> str | None:
    """신뢰도 높은 텍스트 PDF만 격리 프로세스에서 Markdown으로 변환한다.

    스캔·혼합 PDF나 인코딩 문제가 있는 PDF는 ``None``을 반환해 호출자가
    원본 PDF 처리 경로를 유지하도록 한다.
    """
    with tempfile.TemporaryDirectory(prefix="ku-notice-pdf-") as temp_dir:
        source = Path(temp_dir) / "document.pdf"
        source.write_bytes(data)
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "ku_notice_monitor.pdf_extract_worker",
                    str(source),
                ],
                cwd=temp_dir,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise DocumentExtractionError("PDF 변환 시간이 초과되었습니다.") from exc

    if completed.returncode == PDF_NATIVE_FALLBACK_EXIT_CODE:
        return None
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()[:300]
        raise DocumentExtractionError(
            f"PDF 변환 실패(code={completed.returncode}): {detail or '상세 없음'}"
        )
    markdown = completed.stdout.decode("utf-8", errors="replace").strip()
    if not markdown:
        raise DocumentExtractionError("PDF에서 텍스트를 추출하지 못했습니다.")
    return _truncate_extracted_document(markdown)
