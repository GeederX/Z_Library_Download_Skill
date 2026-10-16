from contextlib import contextmanager
import hashlib
import importlib
import io
from pathlib import Path
import struct
import zipfile

import pytest

from book_tools.download import download, staged_download
from book_tools.errors import BookError
from book_tools.models import DownloadTicket


download_module = importlib.import_module("book_tools.download")
PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
CONTAINER = (
    '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
    '<rootfiles><rootfile full-path="OPS/book.opf" '
    'media-type="application/oebps-package+xml"/></rootfiles></container>'
).encode()
PACKAGE = b'<package xmlns="http://www.idpf.org/2007/opf" version="3.0"/>'


class Response:
    def __init__(self, chunks, headers=None):
        self.chunks = chunks
        self.headers = {key.lower(): value for key, value in (headers or {}).items()}

    def iter_bytes(self, chunk_size=None):
        for chunk in self.chunks:
            if isinstance(chunk, BaseException):
                raise chunk
            if chunk_size is None:
                yield chunk
            else:
                for start in range(0, len(chunk), chunk_size):
                    yield chunk[start:start + chunk_size]


class MemoryHttp:
    def __init__(self, data=b"", *, chunks=None, headers=None):
        self.response = Response([data] if chunks is None else chunks, headers)
        self.opened = False
        self.closed = False

    @contextmanager
    def stream(self, url):
        self.opened = True
        try:
            yield self.response
        finally:
            self.closed = True


def ticket(extension="pdf", *, filename=None, md5=None):
    return DownloadTicket(
        url="https://download.example/private?token=never-print-this",
        filename=filename or "book." + extension,
        extension=extension,
        source="annas",
        expected_md5=md5,
    )


def epub(*, container=CONTAINER, package=PACKAGE, package_path="OPS/book.opf", mimetype=b"application/epub+zip", extra=()):
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w") as archive:
        if mimetype is not None:
            archive.writestr("mimetype", mimetype, compress_type=zipfile.ZIP_STORED)
        if container is not None:
            archive.writestr("META-INF/container.xml", container, compress_type=zipfile.ZIP_DEFLATED)
        if package is not None:
            archive.writestr(package_path, package, compress_type=zipfile.ZIP_STORED)
        for name, data in extra:
            archive.writestr(name, data)
    return result.getvalue()


def palm():
    header = bytearray(88)
    header[60:68] = b"BOOKMOBI"
    header[76:78] = (1).to_bytes(2, "big")
    header[78:82] = (88).to_bytes(4, "big")
    return bytes(header) + bytes(16) + b"MOBI" + bytes(20)


def assert_empty(directory):
    assert list(directory.iterdir()) == []


@pytest.mark.parametrize("extension,data", [
    ("epub", epub()),
    ("pdf", PDF),
    ("mobi", palm()),
    ("azw3", palm()),
    ("djvu", b"AT&TFORM" + struct.pack(">I", 4) + b"DJVU"),
    ("fb2", b'<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0"><body><p>Hello</p></body></FictionBook>'),
    ("txt", "A real book.\n\u4f60\u597d\n".encode()),
    ("txt", "A UTF-16 book.\n\u4f60\u597d".encode("utf-16")),
])
def test_supported_formats_are_streamed_validated_and_receipted(tmp_path, extension, data):
    http = MemoryHttp(chunks=[data[:7], data[7:]], headers={"Content-Length": str(len(data))})
    result = download(http, ticket(extension, md5=hashlib.md5(data).hexdigest().upper()), tmp_path)
    assert result == {
        "source": "annas", "path": str(tmp_path / ("book." + extension)),
        "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        "extension": extension, "validated": True,
    }
    assert Path(result["path"]).read_bytes() == data
    assert "token" not in repr(result)
    assert http.closed
    assert sorted(path.name for path in tmp_path.iterdir()) == ["book." + extension]


def test_large_generated_text_stream_has_correct_checksum(tmp_path):
    block = b"A reasonably long line of plain text.\n" * 2048
    digest = hashlib.sha256()

    def chunks():
        for _ in range(128):
            digest.update(block)
            yield block

    result = download(MemoryHttp(chunks=chunks()), ticket("txt"), tmp_path)
    assert result["size"] == len(block) * 128
    assert result["sha256"] == digest.hexdigest()
    assert Path(result["path"]).stat().st_size == result["size"]


@pytest.mark.parametrize("extension", ["epub", "pdf", "mobi", "azw3", "djvu", "fb2", "txt"])
def test_html_error_page_never_becomes_a_book(tmp_path, extension):
    data = b"<!doctype html><html><body>Login required</body></html>"
    with pytest.raises(BookError) as caught:
        download(MemoryHttp(data), ticket(extension), tmp_path)
    assert caught.value.code == "invalid_file"
    assert_empty(tmp_path)


@pytest.mark.parametrize("data", [
    b"", b"\x00binary", b"not utf8: \xff", b"   \n\t", b" " * 100000 + b"<HTML>error</HTML>",
])
def test_invalid_text_is_rejected(tmp_path, data):
    with pytest.raises(BookError) as caught:
        download(MemoryHttp(data), ticket("txt"), tmp_path)
    assert caught.value.code == "invalid_file"
    assert_empty(tmp_path)


@pytest.mark.parametrize("data", [b"%PDF-1.7\ntruncated", b"not a PDF\n%%EOF", PDF + b"unexpected html"])
def test_pdf_requires_signature_and_final_eof(tmp_path, data):
    with pytest.raises(BookError) as caught:
        download(MemoryHttp(data), ticket(), tmp_path)
    assert caught.value.code == "invalid_file"
    assert_empty(tmp_path)


@pytest.mark.parametrize("data", [
    epub(mimetype=None), epub(mimetype=b"application/zip"), epub(container=None),
    epub(package=None), epub(package=b"<html/>"), epub(container=b"<container/>"),
    epub(container=CONTAINER.replace(b"OPS/book.opf", b"../book.opf"), package_path="../book.opf"),
    epub(container=CONTAINER.replace(b"OPS/book.opf", b"/book.opf")),
    epub(container=CONTAINER.replace(b"OPS/book.opf", b"C:\\book.opf")),
    epub(container=CONTAINER.replace(b"OPS/book.opf", b"%2e%2e/book.opf")),
    epub(container=CONTAINER.replace(b"OPS/book.opf", b"missing.opf")),
    epub(extra=[("../unrelated.xhtml", b"bad")]),
    epub(package=b" " * (1024 * 1024 + 1)),
    epub(container=b'<!DOCTYPE container [<!ENTITY x "unsafe">]>' + CONTAINER),
    epub(package=b'<!DOCTYPE package SYSTEM "file:///etc/passwd">' + PACKAGE),
])
def test_epub_invalid_metadata_and_unsafe_paths_are_rejected(tmp_path, data):
    with pytest.raises(BookError) as caught:
        download(MemoryHttp(data), ticket("epub"), tmp_path)
    assert caught.value.code == "invalid_file"
    assert_empty(tmp_path)


def test_epub_compressed_metadata_bomb_is_rejected(tmp_path):
    container = CONTAINER.replace(b"<rootfiles>", b" " * (256 * 1024) + b"<rootfiles>")
    with pytest.raises(BookError) as caught:
        download(MemoryHttp(epub(container=container)), ticket("epub"), tmp_path)
    assert caught.value.code == "invalid_file"
    assert_empty(tmp_path)


def test_epub_duplicate_member_is_not_ambiguously_validated(tmp_path):
    with pytest.warns(UserWarning, match="Duplicate name"):
        data = epub(extra=[("OPS/book.opf", b"<html/>")])
    with pytest.raises(BookError):
        download(MemoryHttp(data), ticket("epub"), tmp_path)
    assert_empty(tmp_path)


def test_epub_referenced_package_crc_is_checked(tmp_path):
    data = epub().replace(PACKAGE, PACKAGE.replace(b"3.0", b"2.0"), 1)
    with pytest.raises(BookError) as caught:
        download(MemoryHttp(data), ticket("epub"), tmp_path)
    assert caught.value.code == "invalid_file"
    assert_empty(tmp_path)


def test_epub_percent_encoded_unicode_package_path(tmp_path):
    path = "OPS/\u4e66 book.opf"
    container = CONTAINER.replace(b"OPS/book.opf", b"OPS/%E4%B9%A6%20book.opf")
    result = download(MemoryHttp(epub(container=container, package_path=path)), ticket("epub"), tmp_path)
    assert result["validated"] is True


def test_epub_oversized_directory_is_rejected_before_zip_parsing(tmp_path):
    data = bytearray(epub())
    offset = data.rfind(b"PK\x05\x06")
    struct.pack_into("<L", data, offset + 12, 9 * 1024 * 1024)
    with pytest.raises(BookError) as caught:
        download(MemoryHttp(bytes(data)), ticket("epub"), tmp_path)
    assert caught.value.code == "invalid_file"
    assert_empty(tmp_path)


@pytest.mark.parametrize("data", [
    b'<FictionBook><body></FictionBook>',
    b'<html/>',
    b'<!DOCTYPE FictionBook [<!ENTITY a "expanded">]><FictionBook>&a;</FictionBook>',
    b'<!DOCTYPE FictionBook SYSTEM "file:///etc/passwd"><FictionBook/>',
])
def test_fb2_requires_well_formed_safe_xml(tmp_path, data):
    with pytest.raises(BookError) as caught:
        download(MemoryHttp(data), ticket("fb2"), tmp_path)
    assert caught.value.code == "invalid_file"
    assert_empty(tmp_path)


@pytest.mark.parametrize("length", ["0", "1", str(len(PDF) + 1), "-1", "not-a-number", "9" * 5000])
def test_content_length_mismatch_or_invalid_value_cleans_partial(tmp_path, length):
    with pytest.raises(BookError) as caught:
        download(MemoryHttp(PDF, headers={"Content-Length": length}), ticket(), tmp_path)
    assert caught.value.code == "integrity_error"
    assert_empty(tmp_path)


def test_encoded_content_length_does_not_compare_wire_size_to_decoded_size(tmp_path):
    result = download(MemoryHttp(PDF, headers={"Content-Encoding": "gzip", "Content-Length": "7"}), ticket(), tmp_path)
    assert result["size"] == len(PDF)


@pytest.mark.parametrize("md5", ["0" * 32, "not-a-checksum"])
def test_expected_md5_failure_never_commits(tmp_path, md5):
    with pytest.raises(BookError) as caught:
        download(MemoryHttp(PDF), ticket(md5=md5), tmp_path)
    assert caught.value.code == "integrity_error"
    assert_empty(tmp_path)


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), RuntimeError("interrupted"), BookError("network_error", "Download interrupted.")])
def test_stream_failure_or_cancellation_removes_partial(tmp_path, failure):
    http = MemoryHttp(chunks=[PDF[:10], failure])
    with pytest.raises(type(failure)):
        download(http, ticket(), tmp_path)
    assert http.closed
    assert_empty(tmp_path)


def test_stream_open_failure_removes_partial(tmp_path):
    class Unavailable:
        @contextmanager
        def stream(self, url):
            raise BookError("network_error", "Network unavailable.")
            yield  # contextmanager protocol; the body is deliberately unreachable.

    with pytest.raises(BookError):
        download(Unavailable(), ticket(), tmp_path)
    assert_empty(tmp_path)


@pytest.mark.parametrize("kind", ["file", "directory", "symlink", "dangling_symlink"])
def test_existing_destination_is_never_replaced(tmp_path, kind):
    destination = tmp_path / "book.pdf"
    if kind == "file":
        destination.write_bytes(b"existing")
    elif kind == "directory":
        destination.mkdir()
    elif kind == "symlink":
        target = tmp_path / "target"
        target.write_bytes(b"existing")
        destination.symlink_to(target)
    else:
        destination.symlink_to(tmp_path / "missing")
    before = destination.lstat()
    http = MemoryHttp(PDF)
    with pytest.raises(BookError) as caught:
        download(http, ticket(), tmp_path)
    assert caught.value.code == "file_exists"
    assert destination.lstat().st_ino == before.st_ino
    assert not http.opened
    assert not list(tmp_path.glob("*.part"))


def test_collision_at_atomic_commit_preserves_racing_writer(tmp_path, monkeypatch):
    real_link = download_module.os.link
    destination = tmp_path / "book.pdf"

    def racing_link(source, target):
        assert not destination.exists()
        destination.write_bytes(b"concurrent writer")
        return real_link(source, target)

    monkeypatch.setattr(download_module.os, "link", racing_link)
    with pytest.raises(BookError) as caught:
        download(MemoryHttp(PDF), ticket(), tmp_path)
    assert caught.value.code == "file_exists"
    assert destination.read_bytes() == b"concurrent writer"
    assert list(tmp_path.iterdir()) == [destination]


def test_final_destination_is_not_visible_during_stream(tmp_path):
    destination = tmp_path / "book.pdf"

    def chunks():
        yield PDF[:10]
        assert not destination.exists()
        yield PDF[10:]
        assert not destination.exists()

    download(MemoryHttp(chunks=chunks()), ticket(), tmp_path)
    assert destination.read_bytes() == PDF
    assert list(tmp_path.iterdir()) == [destination]


def test_explicit_overwrite_keeps_old_file_until_atomic_commit(tmp_path):
    destination = tmp_path / "book.pdf"
    destination.write_bytes(b"old book")

    def chunks():
        assert destination.read_bytes() == b"old book"
        yield PDF[:10]
        assert destination.read_bytes() == b"old book"
        yield PDF[10:]
        assert destination.read_bytes() == b"old book"

    download(MemoryHttp(chunks=chunks()), ticket(), tmp_path, overwrite=True)
    assert destination.read_bytes() == PDF
    assert list(tmp_path.iterdir()) == [destination]


def test_failed_overwrite_keeps_previous_file(tmp_path):
    destination = tmp_path / "book.pdf"
    destination.write_bytes(b"old book")
    with pytest.raises(BookError):
        download(MemoryHttp(b"<html>not a book</html>"), ticket(), tmp_path, overwrite=True)
    assert destination.read_bytes() == b"old book"
    assert list(tmp_path.iterdir()) == [destination]


def test_overwrite_replaces_symlink_without_touching_its_target(tmp_path):
    target = tmp_path / "original.pdf"
    target.write_bytes(b"unrelated target")
    destination = tmp_path / "book.pdf"
    destination.symlink_to(target)
    download(MemoryHttp(PDF), ticket(), tmp_path, overwrite=True)
    assert not destination.is_symlink()
    assert destination.read_bytes() == PDF
    assert target.read_bytes() == b"unrelated target"


@pytest.mark.parametrize("operation", ["fsync", "link", "replace"])
def test_filesystem_failure_does_not_leave_partial_or_false_success(tmp_path, monkeypatch, operation):
    def fail(*args, **kwargs):
        raise OSError("local storage unavailable")

    monkeypatch.setattr(download_module.os, operation, fail)
    with pytest.raises(BookError) as caught:
        download(MemoryHttp(PDF), ticket(), tmp_path, overwrite=operation == "replace")
    assert caught.value.code == "download_failed"
    assert_empty(tmp_path)


@pytest.mark.parametrize("name,expected", [
    ("../../remote.pdf", "remote.epub"),
    (r"C:\secret\remote.exe", "remote.epub"),
    ("CON.epub", "_CON.epub"),
    ("LPT1.txt", "_LPT1.epub"),
    ("...", "book.epub"),
    ("evil\x00name?.zip", "evil_name_.epub"),
    ("\uff0e\uff0e\uff0fremote.exe", "remote.epub"),
])
def test_remote_names_are_sanitized_and_have_authoritative_extension(tmp_path, name, expected):
    result = download(MemoryHttp(epub()), ticket("epub", filename=name), tmp_path)
    assert Path(result["path"]) == tmp_path / expected
    assert list(tmp_path.iterdir()) == [tmp_path / expected]


def test_remote_multibyte_filename_is_shortened_without_corrupting_utf8(tmp_path):
    result = download(MemoryHttp(PDF), ticket(filename="\u4e66" * 300 + ".pdf"), tmp_path)
    name = Path(result["path"]).name
    assert len(name.encode("utf-8")) <= 240
    assert name.endswith(".pdf")
    assert "\ufffd" not in name


@pytest.mark.parametrize("name", [
    "", ".", "..", "../book.pdf", "sub/book.pdf", r"sub\book.pdf", "/book.pdf",
    "C:book.pdf", "CON.pdf", "NUL.pdf", "LPT1.pdf", "book.txt", "book.pdf ",
    "book\x00.pdf", "x" * 300 + ".pdf", "\uff0e\uff0e\uff0fbook.pdf",
])
def test_explicit_names_are_not_silently_reinterpreted_as_paths(tmp_path, name):
    http = MemoryHttp(PDF)
    with pytest.raises(BookError) as caught:
        download(http, ticket(), tmp_path, filename=name)
    assert caught.value.code == "invalid_filename"
    assert not http.opened
    assert_empty(tmp_path)


def test_explicit_name_without_extension_gets_requested_suffix(tmp_path):
    result = download(MemoryHttp(PDF), ticket(), tmp_path, filename="my book")
    assert Path(result["path"]).name == "my book.pdf"


def test_unsupported_format_fails_without_downloading(tmp_path):
    http = MemoryHttp(b"unvalidated bytes")
    with pytest.raises(BookError) as caught:
        download(http, ticket("exe"), tmp_path)
    assert caught.value.code == "unsupported_format"
    assert not http.opened
    assert_empty(tmp_path)


@pytest.mark.parametrize("data", [
    b'<FictionBook padding="' + b"x" * (2 * 1024 * 1024) + b'"/>',
    b"<FictionBook>" + b"<section>" * 300 + b"</section>" * 300 + b"</FictionBook>",
])
def test_fb2_unbounded_tokens_and_nesting_are_rejected(tmp_path, data):
    with pytest.raises(BookError) as caught:
        download(MemoryHttp(data), ticket("fb2"), tmp_path)
    assert caught.value.code == "invalid_file"
    assert_empty(tmp_path)


def test_fb2_large_text_nodes_remain_streamable(tmp_path):
    def chunks():
        yield b"<FictionBook><body><section><p>"
        for _ in range(40):
            yield b"A" * (64 * 1024)
        yield b"</p></section></body></FictionBook>"

    result = download(MemoryHttp(chunks=chunks()), ticket("fb2"), tmp_path)
    assert result["validated"] is True
    assert Path(result["path"]).stat().st_size == result["size"]


@pytest.mark.parametrize("overwrite", [False, True])
@pytest.mark.parametrize("delay_at", ["headers", "chunk", "end"])
def test_total_download_deadline_cleans_partial_and_preserves_existing_file(tmp_path, monkeypatch, overwrite, delay_at):
    clock = [100.0]
    monkeypatch.setattr(download_module.time, "monotonic", lambda: clock[0])
    destination = tmp_path / "book.pdf"
    if overwrite:
        destination.write_bytes(b"previous book")

    def chunks():
        yield PDF[:10]
        if delay_at == "chunk":
            clock[0] += 301.0
        yield PDF[10:]
        if delay_at == "end":
            clock[0] += 301.0

    class SlowHttp(MemoryHttp):
        @contextmanager
        def stream(self, url):
            if delay_at == "headers":
                clock[0] += 301.0
            with super().stream(url) as response:
                yield response

    http = SlowHttp(chunks=chunks())
    with pytest.raises(BookError) as caught:
        download(http, ticket(), tmp_path, overwrite=overwrite)
    assert caught.value.code == "timeout"
    assert caught.value.recoverable
    assert http.closed
    if overwrite:
        assert destination.read_bytes() == b"previous book"
        assert list(tmp_path.iterdir()) == [destination]
    else:
        assert_empty(tmp_path)


def test_download_finishing_within_total_deadline_succeeds(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(download_module.time, "monotonic", lambda: clock[0])

    def chunks():
        yield PDF[:10]
        clock[0] += 299.0
        yield PDF[10:]

    result = download(MemoryHttp(chunks=chunks()), ticket(), tmp_path)
    assert Path(result["path"]).read_bytes() == PDF


def test_slow_drip_is_stopped_without_waiting_for_a_fixed_size_chunk(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(download_module.time, "monotonic", lambda: clock[0])

    class DripResponse:
        headers = {}

        def iter_bytes(self, chunk_size=None):
            pending = bytearray()
            for _ in range(301):
                clock[0] += 1.0
                pending.extend(b"x")
                if chunk_size is None or len(pending) >= chunk_size:
                    yield bytes(pending)
                    pending.clear()
            raise AssertionError("Download continued reading after its total deadline.")

    http = MemoryHttp()
    http.response = DripResponse()
    with pytest.raises(BookError) as caught:
        download(http, ticket(), tmp_path)
    assert caught.value.code == "timeout"
    assert clock[0] == 400.0
    assert http.closed
    assert_empty(tmp_path)


@pytest.mark.parametrize("replace_stage", [False, True])
def test_staged_native_save_validates_current_file_and_returns_integrity_receipt(tmp_path, replace_stage):
    destination = tmp_path / "chosen book.pdf"
    with staged_download(
        ticket(md5=hashlib.md5(PDF).hexdigest().upper()), tmp_path, filename="chosen book"
    ) as staged:
        assert staged.path.parent == tmp_path
        assert staged.path != destination
        assert staged.path.is_file()
        if download_module.os.name == "posix":
            assert staged.path.stat().st_mode & 0o777 == 0o600
        staged.path.write_bytes(b"stale content" * 100)
        if replace_stage:
            staged.path.unlink()
        staged.path.write_bytes(PDF)
        assert not destination.exists()
        result = staged.finish()
        assert destination.read_bytes() == PDF
    assert not staged.path.exists()
    assert result == {
        "source": "annas", "path": str(destination), "size": len(PDF),
        "sha256": hashlib.sha256(PDF).hexdigest(), "extension": "pdf", "validated": True,
    }
    assert list(tmp_path.iterdir()) == [destination]


@pytest.mark.parametrize("extension,data", [
    ("epub", epub(package=b"<html/>")),
    ("epub", epub(container=CONTAINER.replace(b"OPS/book.opf", b"%2e%2e/book.opf"))),
    ("epub", epub(package=b'<!DOCTYPE package SYSTEM "file:///etc/passwd">' + PACKAGE)),
    ("pdf", PDF + b"<html>unexpected content</html>"),
    ("fb2", b'<!DOCTYPE FictionBook [<!ENTITY a "unsafe">]><FictionBook>&a;</FictionBook>'),
    ("txt", b"<html><body>Human verification required</body></html>"),
])
def test_staged_download_rejects_invalid_content_even_with_matching_checksum(tmp_path, extension, data):
    with pytest.raises(BookError) as caught:
        with staged_download(ticket(extension, md5=hashlib.md5(data).hexdigest()), tmp_path) as staged:
            staged.path.write_bytes(data)
            staged.finish()
    assert caught.value.code == "invalid_file"
    assert_empty(tmp_path)


@pytest.mark.parametrize("md5", ["0" * 32, "not-a-checksum"])
def test_staged_checksum_failure_does_not_replace_existing_book(tmp_path, md5):
    destination = tmp_path / "book.pdf"
    destination.write_bytes(b"previous book")
    with pytest.raises(BookError) as caught:
        with staged_download(ticket(md5=md5), tmp_path, overwrite=True) as staged:
            staged.path.write_bytes(PDF)
            staged.finish()
    assert caught.value.code == "integrity_error"
    assert destination.read_bytes() == b"previous book"
    assert list(tmp_path.iterdir()) == [destination]


def test_staged_commit_collision_preserves_new_destination(tmp_path):
    destination = tmp_path / "book.pdf"
    with pytest.raises(BookError) as caught:
        with staged_download(ticket(), tmp_path) as staged:
            staged.path.write_bytes(PDF)
            destination.write_bytes(b"racing writer")
            staged.finish()
    assert caught.value.code == "file_exists"
    assert destination.read_bytes() == b"racing writer"
    assert list(tmp_path.iterdir()) == [destination]


def test_staged_explicit_overwrite_keeps_existing_file_until_finish(tmp_path):
    destination = tmp_path / "book.pdf"
    destination.write_bytes(b"previous book")
    with staged_download(ticket(), tmp_path, overwrite=True) as staged:
        staged.path.write_bytes(PDF)
        assert destination.read_bytes() == b"previous book"
        staged.finish()
    assert destination.read_bytes() == PDF
    assert list(tmp_path.iterdir()) == [destination]


@pytest.mark.parametrize("failure", [None, KeyboardInterrupt(), RuntimeError("native transfer interrupted")])
def test_unfinished_staged_download_always_removes_partial(tmp_path, failure):
    def save():
        with staged_download(ticket(), tmp_path) as staged:
            staged.path.write_bytes(PDF[:10])
            if failure is not None:
                raise failure

    if failure is None:
        save()
    else:
        with pytest.raises(type(failure)):
            save()
    assert_empty(tmp_path)


def test_staged_no_clobber_fails_safe_when_hardlinks_are_unavailable(tmp_path, monkeypatch):
    def unsupported(*args, **kwargs):
        raise OSError("filesystem does not support hardlinks")

    monkeypatch.setattr(download_module.os, "link", unsupported)
    with pytest.raises(BookError) as caught:
        with staged_download(ticket(), tmp_path) as staged:
            staged.path.write_bytes(PDF)
            staged.finish()
    assert caught.value.code == "download_failed"
    assert_empty(tmp_path)
