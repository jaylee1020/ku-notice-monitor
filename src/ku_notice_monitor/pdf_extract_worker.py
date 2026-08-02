"""pdf-inspector를 격리 프로세스에서 실행하는 작은 워커."""

import sys
from pathlib import Path
from typing import Any

from .document_extract import PDF_NATIVE_FALLBACK_EXIT_CODE, _truncate_extracted_document

MIN_PDF_TEXT_CONFIDENCE = 0.95


def _can_use_local_markdown(result: Any) -> bool:
    markdown = str(result.markdown or "").strip()
    return bool(
        result.pdf_type == "text_based"
        and result.confidence >= MIN_PDF_TEXT_CONFIDENCE
        and not result.pages_needing_ocr
        and not result.has_encoding_issues
        and markdown
    )


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("PDF 파일 경로 하나가 필요합니다.", file=sys.stderr)
        return 2

    try:
        import pdf_inspector

        result = pdf_inspector.process_pdf(str(Path(args[0])))
    except Exception as exc:
        print(f"pdf-inspector 오류: {exc}", file=sys.stderr)
        return 1

    if not _can_use_local_markdown(result):
        return PDF_NATIVE_FALLBACK_EXIT_CODE

    markdown = _truncate_extracted_document(str(result.markdown).strip())
    sys.stdout.write(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
