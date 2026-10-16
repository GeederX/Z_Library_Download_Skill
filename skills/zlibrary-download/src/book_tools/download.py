"""Streaming downloads with bounded format checks, not malware or DRM inspection."""

from __future__ import annotations

import codecs
from collections.abc import Iterator
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import struct
import tempfile
import time
import unicodedata
from urllib.parse import unquote
import xml.sax
from xml.sax.handler import ContentHandler, feature_namespaces
import zipfile
import zlib

from defusedxml import ElementTree, sax
from defusedxml.common import DefusedXmlException

from .errors import BookError
from .http import Http
from .models import DownloadTicket


_CHUNK_SIZE = 64 * 1024
_DOWNLOAD_TIMEOUT = 300.0
_METADATA_LIMIT = 1024 * 1024
_FORMATS = {"epub", "pdf", "mobi", "azw3", "djvu", "fb2", "txt"}
_RESERVED = re.compile(r"^(?:CON|PRN|AUX|NUL|CLOCK\$|COM[1-9]|LPT[1-9])$", re.I)
_CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
_OPF_NS = "http://www.idpf.org/2007/opf"


def _invalid(message: str = "Downloaded content does not match the requested format.") -> BookError:
    return BookError("invalid_file", message)


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise BookError("timeout", "Download exceeded the 300-second time limit.", recoverable=True)


def _filename(value: str, extension: str, *, explicit: bool) -> str:
    value = unicodedata.normalize("NFKC", value)
    if explicit:
        if (
            not value
            or value in {".", ".."}
            or any(c in value for c in '/\\<>:"|?*')
            or any(unicodedata.category(c).startswith("C") for c in value)
            or value != value.strip(" .")
        ):
            raise BookError("invalid_filename", "Filename must be a single safe filename, not a path.")
    else:
        value = value.replace("\\", "/").rsplit("/", 1)[-1]
        value = "".join(
            "_" if c in '<>:"|?*' or unicodedata.category(c).startswith("C") else c
            for c in value
        ).strip(" .")
        value = value or "book"

    stem, dot, suffix = value.rpartition(".")
    if dot and stem:
        if explicit and suffix.lower() != extension:
            raise BookError("invalid_filename", "Filename suffix must match the requested format.")
        value = stem
    if _RESERVED.fullmatch(value.split(".", 1)[0]):
        if explicit:
            raise BookError("invalid_filename", "Filename is reserved by the operating system.")
        value = "_" + value
    suffix = "." + extension
    max_bytes = 240 - len(suffix.encode("utf-8"))
    if explicit and len(value.encode("utf-8")) > max_bytes:
        raise BookError("invalid_filename", "Filename is too long.")
    value = value.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore").rstrip(" .")
    return (value or "book") + suffix


def _safe_member(name: str) -> bool:
    parts = name.rstrip("/").split("/")
    return bool(name) and not any(c in name for c in "\\\x00:") and all(
        part not in {"", ".", ".."} for part in parts
    )


def _zip_directory(file, size: int) -> None:
    # Bound zipfile's central-directory allocation before it constructs ZipInfo objects.
    file.seek(max(0, size - 65557))
    tail = file.read(65557)
    offset = tail.rfind(b"PK\x05\x06")
    if offset < 0 or len(tail) - offset < 22:
        raise _invalid("EPUB has no valid ZIP directory.")
    _, disk, directory_disk, disk_count, count, directory_size, start, comment = struct.unpack(
        "<4s4H2LH", tail[offset:offset + 22]
    )
    if (
        disk or directory_disk or disk_count != count
        or count == 65535 or start == 0xFFFFFFFF or directory_size == 0xFFFFFFFF
        or count > 10000 or directory_size > 8 * _METADATA_LIMIT
        or start + directory_size > size - len(tail) + offset
        or offset + 22 + comment != len(tail)
    ):
        raise _invalid("EPUB ZIP directory is unsupported or exceeds validation limits.")
    file.seek(0)


def _zip_metadata(archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    if (
        info.file_size > limit or info.flag_bits & 1
        or (info.external_attr >> 16) & 0o170000 == 0o120000
        or (info.file_size > _CHUNK_SIZE and info.file_size > max(1, info.compress_size) * 200)
    ):
        raise _invalid("EPUB metadata exceeds safe validation limits.")
    with archive.open(info) as member:
        data = member.read(limit + 1)
    if len(data) > limit or len(data) != info.file_size:
        raise _invalid("EPUB metadata size is inconsistent.")
    return data


def _xml(data: bytes):
    return ElementTree.fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)


def _epub(file, size: int) -> None:
    _zip_directory(file, size)
    with zipfile.ZipFile(file) as archive:
        entries = archive.infolist()
        if len(entries) > 10000:
            raise _invalid("EPUB ZIP directory exceeds validation limits.")
        names = {entry.filename: entry for entry in entries}
        if len(entries) != len(names) or any(not _safe_member(name) for name in names):
            raise _invalid("EPUB contains ambiguous or unsafe ZIP paths.")
        mime = names.get("mimetype")
        if (
            mime is None or not entries or entries[0] is not mime
            or mime.compress_type != zipfile.ZIP_STORED
            or _zip_metadata(archive, mime, 64) != b"application/epub+zip"
        ):
            raise _invalid("EPUB has no valid uncompressed mimetype entry.")
        container = names.get("META-INF/container.xml")
        if container is None:
            raise _invalid("EPUB is missing its container metadata.")
        root = _xml(_zip_metadata(archive, container, _METADATA_LIMIT))
        if root.tag != f"{{{_CONTAINER_NS}}}container":
            raise _invalid("EPUB container root is invalid.")
        rootfiles = root.findall(f"{{{_CONTAINER_NS}}}rootfiles/{{{_CONTAINER_NS}}}rootfile")
        if not rootfiles:
            raise _invalid("EPUB does not reference a package document.")
        for rootfile in rootfiles:
            path = rootfile.get("full-path", "")
            decoded = unquote(path, errors="strict")
            if (
                not _safe_member(path) or not _safe_member(decoded)
                or rootfile.get("media-type") != "application/oebps-package+xml"
            ):
                raise _invalid("EPUB references an invalid package path.")
            member = names.get(path) or names.get(decoded)
            if member is None:
                raise _invalid("EPUB package document is missing.")
            package = _xml(_zip_metadata(archive, member, _METADATA_LIMIT))
            if package.tag != f"{{{_OPF_NS}}}package":
                raise _invalid("EPUB package document is invalid.")


def _pdf(file, size: int) -> None:
    file.seek(0)
    if not re.match(rb"%PDF-(?:1\.[0-7]|2\.0)[\r\n\t ]", file.read(16)):
        raise _invalid("PDF signature is missing or invalid.")
    file.seek(max(0, size - 4096))
    if not file.read(4096).rstrip(b" \t\r\n\f").endswith(b"%%EOF"):
        raise _invalid("PDF end marker is missing.")


def _palm(file, size: int) -> None:
    file.seek(0)
    header = file.read(86)
    if len(header) < 86 or header[60:68] != b"BOOKMOBI":
        raise _invalid("MOBI/AZW3 Palm database signature is missing.")
    count = int.from_bytes(header[76:78], "big")
    first = int.from_bytes(header[78:82], "big")
    if not count or 78 + count * 8 > size or first < 78 + count * 8 or first + 20 > size:
        raise _invalid("MOBI/AZW3 record table is invalid.")
    file.seek(first + 16)
    if file.read(4) != b"MOBI":
        raise _invalid("MOBI/AZW3 content header is missing.")


def _djvu(file, size: int) -> None:
    file.seek(0)
    header = file.read(16)
    if (
        len(header) < 16 or header[:8] != b"AT&TFORM" or header[12:16] not in {b"DJVU", b"DJVM"}
        or int.from_bytes(header[8:12], "big") < 4
        or int.from_bytes(header[8:12], "big") + 12 != size
    ):
        raise _invalid("DJVU container signature or size is invalid.")


class _FictionBookRoot(ContentHandler):
    def __init__(self) -> None:
        super().__init__()
        self.seen_root = False
        self.progress = 0
        self.depth = 0

    def startElementNS(self, name, qname, attrs) -> None:
        self.progress += 1
        self.depth += 1
        if self.depth > 256:
            raise _invalid("FB2 nesting exceeds safe validation limits.")
        if not self.seen_root:
            if name not in {(None, "FictionBook"), ("http://www.gribuser.ru/xml/fictionbook/2.0", "FictionBook")}:
                raise _invalid("FB2 root element is invalid.")
            self.seen_root = True

    def endElementNS(self, name, qname) -> None:
        self.progress += 1
        self.depth -= 1

    def characters(self, content) -> None:
        self.progress += 1


def _fb2(file, size: int) -> None:
    handler = _FictionBookRoot()
    parser = sax.make_parser()
    parser.forbid_dtd = parser.forbid_entities = parser.forbid_external = True
    parser.setFeature(feature_namespaces, True)
    parser.setContentHandler(handler)
    file.seek(0)
    stalled_bytes = 0
    while chunk := file.read(_CHUNK_SIZE):
        progress = handler.progress
        parser.feed(chunk)
        stalled_bytes = stalled_bytes + len(chunk) if handler.progress == progress else 0
        # Expat may retain an unfinished attribute or other XML token across feeds.
        if stalled_bytes > _METADATA_LIMIT:
            raise _invalid("FB2 XML token exceeds safe validation limits.")
    parser.close()
    if not handler.seen_root:
        raise _invalid("FB2 document is empty.")


def _txt(file, size: int) -> None:
    file.seek(0)
    prefix = file.read(4)
    encoding = "utf-16" if prefix.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
    file.seek(0)
    sample = ""
    nonspace = False
    while True:
        chunk = file.read(_CHUNK_SIZE)
        text = decoder.decode(chunk, final=not chunk)
        if any(unicodedata.category(c) == "Cc" and c not in "\t\n\r\f" for c in text):
            raise _invalid("Text download contains binary control characters.")
        nonspace = nonspace or bool(text.strip())
        if len(sample) < 8192:
            candidate = text if sample else text.lstrip()
            sample += candidate[:8192 - len(sample)]
        if not chunk:
            break
    if not nonspace or re.search(r"<!doctype\s+html\b|<(?:html|head|body|script)(?:\s|>)", sample, re.I):
        raise _invalid("Text download is empty or appears to be an HTML page.")


def _validate(file, extension: str, size: int) -> None:
    validators = {"epub": _epub, "pdf": _pdf, "mobi": _palm, "azw3": _palm, "djvu": _djvu, "fb2": _fb2, "txt": _txt}
    try:
        validators[extension](file, size)
    except BookError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, KeyError, ValueError, RuntimeError,
            NotImplementedError, EOFError, zlib.error, ElementTree.ParseError,
            DefusedXmlException, xml.sax.SAXException):
        raise _invalid() from None


class _StagedDownload:
    def __init__(self, path: Path, destination: Path, ticket: DownloadTicket, extension: str, overwrite: bool):
        self.path = path
        self._destination = destination
        self._ticket = ticket
        self._extension = extension
        self._overwrite = overwrite
        self._active = True
        self._finished = False

    def finish(self) -> dict:
        """Validate the current staging file and atomically publish its receipt."""
        if not self._active or self._finished:
            raise BookError("download_failed", "Download staging is no longer active.")
        try:
            sha256 = hashlib.sha256()
            md5 = hashlib.md5(usedforsecurity=False) if self._ticket.expected_md5 is not None else None
            size = 0
            # Native download writers may replace the original temporary file.
            # Reopen its current contents rather than retaining the mkstemp inode.
            with self.path.open("r+b") as file:
                os.chmod(self.path, 0o600)
                while chunk := file.read(_CHUNK_SIZE):
                    size += len(chunk)
                    sha256.update(chunk)
                    if md5 is not None:
                        md5.update(chunk)
                if md5 is not None and md5.hexdigest() != self._ticket.expected_md5.lower():
                    raise BookError("integrity_error", "Download does not match the expected MD5 checksum.")
                _validate(file, self._extension, size)
                os.fsync(file.fileno())
            if self._overwrite:
                os.replace(self.path, self._destination)
            else:
                try:
                    os.link(self.path, self._destination)
                except FileExistsError:
                    raise BookError("file_exists", "Destination already exists; no file was replaced.") from None
            self._finished = True
            return {
                "source": self._ticket.source, "path": str(self._destination), "size": size,
                "sha256": sha256.hexdigest(), "extension": self._extension, "validated": True,
            }
        except OSError:
            raise BookError("download_failed", "Could not save the downloaded file.") from None


@contextmanager
def staged_download(
    ticket: DownloadTicket,
    output_dir: Path,
    *,
    filename: str | None = None,
    overwrite: bool = False,
) -> Iterator[_StagedDownload]:
    """Yield a private same-directory path; finish explicitly to publish a book.

    Writers must close the staging file before calling finish(). Exiting without
    finishing, including cancellation, removes it without touching the destination.
    EPUB checks bounded ZIP/package metadata, not every content member or full EPUB
    conformance. Other binary checks are structural signatures, not full parsers.
    FB2 limits nesting and unfinished XML tokens. TXT accepts UTF-8 and BOM-marked
    UTF-16. Destination directories are trusted local storage; same-directory
    hostile writers are outside this threat model.
    """
    extension = ticket.extension.lower().lstrip(".")
    if extension not in _FORMATS:
        raise BookError("unsupported_format", "Supported formats: epub, pdf, mobi, azw3, djvu, fb2, txt.")
    name = _filename(ticket.filename if filename is None else filename, extension, explicit=filename is not None)
    if ticket.expected_md5 is not None and not re.fullmatch(r"[0-9a-fA-F]{32}", ticket.expected_md5):
        raise BookError("integrity_error", "Expected MD5 checksum is invalid.")
    destination = Path(output_dir) / name
    temporary: Path | None = None
    staged: _StagedDownload | None = None
    try:
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not overwrite and os.path.lexists(destination):
                raise BookError("file_exists", "Destination already exists; use overwrite explicitly to replace it.")
            descriptor, temporary_name = tempfile.mkstemp(prefix=".book-tools-", suffix=".part", dir=destination.parent)
            temporary = Path(temporary_name)
            os.close(descriptor)
            staged = _StagedDownload(temporary, destination, ticket, extension, overwrite)
            yield staged
        finally:
            if staged is not None:
                staged._active = False
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    except OSError:
        raise BookError("download_failed", "Could not save the downloaded file.") from None


def download(
    http: Http,
    ticket: DownloadTicket,
    output_dir: Path,
    *,
    filename: str | None = None,
    overwrite: bool = False,
) -> dict:
    """Stream HTTP bytes, then use the shared validation and atomic commit path."""
    with staged_download(ticket, output_dir, filename=filename, overwrite=overwrite) as staged:
        deadline = time.monotonic() + _DOWNLOAD_TIMEOUT
        with staged.path.open("wb") as file:
            with http.stream(ticket.url) as response:
                _check_deadline(deadline)
                encoded = response.headers.get("content-encoding", "identity").strip().lower()
                length = response.headers.get("content-length") if encoded in {"", "identity"} else None
                if length is not None and not re.fullmatch(r"[0-9]+", length.strip()):
                    raise BookError("integrity_error", "Download has an invalid Content-Length.")
                try:
                    expected_size = int(length) if length is not None else None
                except ValueError:
                    raise BookError("integrity_error", "Download has an invalid Content-Length.") from None
                size = 0
                # Native yields avoid buffering a slow drip until a fixed chunk fills.
                for chunk in response.iter_bytes():
                    _check_deadline(deadline)
                    view = memoryview(chunk)
                    for start in range(0, len(view), _CHUNK_SIZE):
                        _check_deadline(deadline)
                        file.write(view[start:start + _CHUNK_SIZE])
                    size += len(view)
                _check_deadline(deadline)
                if expected_size is not None and size != expected_size:
                    raise BookError("integrity_error", "Download size does not match Content-Length.")
        _check_deadline(deadline)
        return staged.finish()
