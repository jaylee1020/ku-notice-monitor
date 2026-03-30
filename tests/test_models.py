"""models.py 단위 테스트"""

import pytest

from models import Attachment


@pytest.mark.parametrize("filename,expected_ext", [
    ("문서.hwp", ".hwp"),
    ("파일.PDF", ".pdf"),
    ("양식.hwpx", ".hwpx"),
    ("image.JPG", ".jpg"),
    ("noext", ""),
])
def test_attachment_ext(filename, expected_ext):
    att = Attachment(filename=filename, url="https://example.com/download.do")
    assert att.ext == expected_ext
