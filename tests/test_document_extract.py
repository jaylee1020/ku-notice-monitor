"""HWP/HWPX 격리 변환 테스트."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ku_notice_monitor.document_extract import DocumentExtractionError, extract_hwp_markdown


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
