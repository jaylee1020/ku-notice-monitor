"""HWP/HWPX와 PDF 격리 변환 테스트."""

from subprocess import TimeoutExpired
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ku_notice_monitor.document_extract import (
    PDF_NATIVE_FALLBACK_EXIT_CODE,
    DocumentExtractionError,
    extract_hwp_markdown,
    extract_pdf_markdown,
)


def test_hwp_extract_runs_in_subprocess():
    result = SimpleNamespace(returncode=0, stdout="| 대상 | 마감 |\n".encode(), stderr=b"")
    with patch("ku_notice_monitor.document_extract.subprocess.run", return_value=result) as run:
        markdown = extract_hwp_markdown(b"fake-hwp", ".hwp")
    assert "| 대상 | 마감 |" in markdown
    assert run.call_args.args[0][1:3] == ["-m", "syhwp"]


def test_hwp_extract_reports_converter_failure():
    result = SimpleNamespace(returncode=2, stdout=b"", stderr="손상된 문서".encode())
    with patch("ku_notice_monitor.document_extract.subprocess.run", return_value=result):
        with pytest.raises(DocumentExtractionError, match="변환 실패"):
            extract_hwp_markdown(b"broken", ".hwpx")


def test_pdf_extract_runs_in_isolated_subprocess():
    result = SimpleNamespace(returncode=0, stdout=b"# Notice\n\nDeadline", stderr=b"")
    with patch("ku_notice_monitor.document_extract.subprocess.run", return_value=result) as run:
        markdown = extract_pdf_markdown(b"%PDF-fake")
    assert markdown == "# Notice\n\nDeadline"
    assert run.call_args.args[0][1:3] == [
        "-m",
        "ku_notice_monitor.pdf_extract_worker",
    ]


def test_pdf_extract_requests_native_fallback():
    result = SimpleNamespace(
        returncode=PDF_NATIVE_FALLBACK_EXIT_CODE,
        stdout=b"",
        stderr=b"",
    )
    with patch("ku_notice_monitor.document_extract.subprocess.run", return_value=result):
        assert extract_pdf_markdown(b"%PDF-scanned") is None


def test_pdf_extract_reports_parser_failure():
    result = SimpleNamespace(returncode=1, stdout=b"", stderr=b"damaged pdf")
    with patch("ku_notice_monitor.document_extract.subprocess.run", return_value=result):
        with pytest.raises(DocumentExtractionError, match="PDF 변환 실패"):
            extract_pdf_markdown(b"broken")


def test_pdf_extract_reports_timeout():
    with patch(
        "ku_notice_monitor.document_extract.subprocess.run",
        side_effect=TimeoutExpired("pdf worker", 20),
    ):
        with pytest.raises(DocumentExtractionError, match="시간이 초과"):
            extract_pdf_markdown(b"slow")
