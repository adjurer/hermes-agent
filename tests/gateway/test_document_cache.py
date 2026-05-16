"""
Tests for document cache utilities in gateway/platforms/base.py.

Covers: get_document_cache_dir, cache_document_from_bytes,
        cleanup_document_cache, SUPPORTED_DOCUMENT_TYPES.
"""

import os
import time
import unicodedata
from pathlib import Path

import pytest

from gateway.platforms.base import (
    SUPPORTED_DOCUMENT_TYPES,
    cache_document_from_bytes,
    cleanup_document_cache,
    decode_text_document_bytes,
    get_document_cache_dir,
    normalize_document_filename,
    prepare_outbound_document_for_send,
)

# ---------------------------------------------------------------------------
# Fixture: redirect DOCUMENT_CACHE_DIR to a temp directory for every test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _redirect_cache(tmp_path, monkeypatch):
    """Point the module-level DOCUMENT_CACHE_DIR to a fresh tmp_path."""
    monkeypatch.setattr(
        "gateway.platforms.base.DOCUMENT_CACHE_DIR", tmp_path / "doc_cache"
    )


# ---------------------------------------------------------------------------
# TestGetDocumentCacheDir
# ---------------------------------------------------------------------------

class TestGetDocumentCacheDir:
    def test_creates_directory(self, tmp_path):
        cache_dir = get_document_cache_dir()
        assert cache_dir.exists()
        assert cache_dir.is_dir()

    def test_returns_existing_directory(self):
        first = get_document_cache_dir()
        second = get_document_cache_dir()
        assert first == second
        assert first.exists()


# ---------------------------------------------------------------------------
# TestCacheDocumentFromBytes
# ---------------------------------------------------------------------------

class TestCacheDocumentFromBytes:
    def test_basic_caching(self):
        data = b"hello world"
        path = cache_document_from_bytes(data, "test.txt")
        assert os.path.exists(path)
        assert Path(path).read_bytes() == data

    def test_filename_preserved_in_path(self):
        path = cache_document_from_bytes(b"data", "report.pdf")
        assert "report.pdf" in os.path.basename(path)

    def test_empty_filename_uses_fallback(self):
        path = cache_document_from_bytes(b"data", "")
        assert "document" in os.path.basename(path)

    def test_unique_filenames(self):
        p1 = cache_document_from_bytes(b"a", "same.txt")
        p2 = cache_document_from_bytes(b"b", "same.txt")
        assert p1 != p2

    def test_path_traversal_blocked(self):
        """Malicious directory components are stripped — only the leaf name survives."""
        path = cache_document_from_bytes(b"data", "../../etc/passwd")
        basename = os.path.basename(path)
        assert "passwd" in basename
        # Must NOT contain directory separators
        assert ".." not in basename
        # File must reside inside the cache directory
        cache_dir = get_document_cache_dir()
        assert Path(path).resolve().is_relative_to(cache_dir.resolve())

    def test_null_bytes_stripped(self):
        path = cache_document_from_bytes(b"data", "file\x00.pdf")
        basename = os.path.basename(path)
        assert "\x00" not in basename
        assert "file.pdf" in basename

    def test_dot_dot_filename_handled(self):
        """A filename that is literally '..' falls back to 'document'."""
        path = cache_document_from_bytes(b"data", "..")
        basename = os.path.basename(path)
        assert "document" in basename

    def test_none_filename_uses_fallback(self):
        path = cache_document_from_bytes(b"data", None)
        assert "document" in os.path.basename(path)

    def test_korean_filename_is_nfc_normalized(self):
        decomposed = unicodedata.normalize("NFD", "한글보고서.md")
        path = cache_document_from_bytes(b"data", decomposed)
        basename = os.path.basename(path)
        assert "한글보고서.md" in basename
        assert basename == unicodedata.normalize("NFC", basename)


class TestDocumentEncoding:
    def test_decode_cp949_korean_markdown(self):
        text, encoding = decode_text_document_bytes("# 제목\n한글 내용".encode("cp949"))
        assert text == "# 제목\n한글 내용"
        assert encoding == "cp949"

    def test_decode_utf16_korean_markdown(self):
        text, encoding = decode_text_document_bytes("# 제목\n한글 내용".encode("utf-16"))
        assert text == "# 제목\n한글 내용"
        assert encoding == "utf-16"

    def test_normalize_filename_strips_control_characters(self):
        assert normalize_document_filename("보고서\n\t.md") == "보고서.md"

    def test_prepare_outbound_text_document_uses_utf8_sig_copy(self, tmp_path):
        decomposed_name = unicodedata.normalize("NFD", "한글보고서.md")
        source = tmp_path / decomposed_name
        source.write_bytes("# 제목\n한글 내용".encode("cp949"))

        send_path, display_name = prepare_outbound_document_for_send(source)

        assert display_name == "한글보고서.md"
        assert send_path != str(source)
        sent_bytes = Path(send_path).read_bytes()
        assert sent_bytes.startswith(b"\xef\xbb\xbf")
        assert sent_bytes.decode("utf-8-sig") == "# 제목\n한글 내용"
        assert source.read_bytes() == "# 제목\n한글 내용".encode("cp949")

    def test_prepare_outbound_binary_document_does_not_reencode(self, tmp_path):
        source = tmp_path / unicodedata.normalize("NFD", "계약서.hwp")
        source.write_bytes(b"\xd0\xcf\x11\xe0binary-hwp")

        send_path, display_name = prepare_outbound_document_for_send(source)

        assert send_path == str(source)
        assert display_name == "계약서.hwp"
        assert source.read_bytes() == b"\xd0\xcf\x11\xe0binary-hwp"

    def test_prepare_outbound_json_does_not_add_bom(self, tmp_path):
        source = tmp_path / "data.json"
        source.write_bytes('{"name":"한글"}'.encode("utf-8"))

        send_path, display_name = prepare_outbound_document_for_send(source)

        assert send_path == str(source)
        assert display_name == "data.json"
        assert source.read_bytes() == '{"name":"한글"}'.encode("utf-8")


# ---------------------------------------------------------------------------
# TestCleanupDocumentCache
# ---------------------------------------------------------------------------

class TestCleanupDocumentCache:
    def test_removes_old_files(self, tmp_path):
        cache_dir = get_document_cache_dir()
        old_file = cache_dir / "old.txt"
        old_file.write_text("old")
        # Set modification time to 48 hours ago
        old_mtime = time.time() - 48 * 3600
        os.utime(old_file, (old_mtime, old_mtime))

        removed = cleanup_document_cache(max_age_hours=24)
        assert removed == 1
        assert not old_file.exists()

    def test_keeps_recent_files(self):
        cache_dir = get_document_cache_dir()
        recent = cache_dir / "recent.txt"
        recent.write_text("fresh")

        removed = cleanup_document_cache(max_age_hours=24)
        assert removed == 0
        assert recent.exists()

    def test_returns_removed_count(self):
        cache_dir = get_document_cache_dir()
        old_time = time.time() - 48 * 3600
        for i in range(3):
            f = cache_dir / f"old_{i}.txt"
            f.write_text("x")
            os.utime(f, (old_time, old_time))

        assert cleanup_document_cache(max_age_hours=24) == 3

    def test_empty_cache_dir(self):
        assert cleanup_document_cache(max_age_hours=24) == 0


# ---------------------------------------------------------------------------
# TestSupportedDocumentTypes
# ---------------------------------------------------------------------------

class TestSupportedDocumentTypes:
    def test_all_extensions_have_mime_types(self):
        for ext, mime in SUPPORTED_DOCUMENT_TYPES.items():
            assert ext.startswith("."), f"{ext} missing leading dot"
            assert "/" in mime, f"{mime} is not a valid MIME type"

    @pytest.mark.parametrize(
        "ext",
        [".pdf", ".md", ".markdown", ".txt", ".zip", ".docx", ".xlsx", ".pptx"],
    )
    def test_expected_extensions_present(self, ext):
        assert ext in SUPPORTED_DOCUMENT_TYPES
