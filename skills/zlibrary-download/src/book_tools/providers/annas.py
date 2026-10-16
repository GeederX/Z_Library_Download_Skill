"""Anonymous Anna's Archive HTML metadata and authenticated JSON download tickets."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Comment, Tag

from ..errors import BookError
from ..http import Http, validate_url
from ..models import Book, DownloadTicket, SearchQuery, filter_books, normalize_language


_MD5 = re.compile(r"[a-fA-F0-9]{32}")
_FORMAT = re.compile(r"^(epub|pdf|mobi|azw3?|djvu|fb2|txt|cb[rz]|docx?|rtf)(?:\s*\([^)]*\))?$", re.I)
_SIZE = re.compile(r"\b\d+(?:[.,]\d+)?\s*[KMGT]?i?B\b", re.I)
_LANGUAGE = re.compile(r"([^\[\]·]+?)\s*\[([a-z]{2,3}(?:-[A-Za-z]+)?)\]", re.I)
_YEAR = re.compile(r"(?:^|[,;\s])((?:1[4-9]|20)\d{2})(?=$|[,;\s])")


def _schema() -> BookError:
    return BookError("provider_schema", "Anna's Archive returned an unsupported page or API response.")


def _text(element: Tag | None) -> str:
    return element.get_text(" ", strip=True) if element else ""


def _icon_text(container: Tag, icon: str) -> str:
    values = []
    for span in container.find_all("span"):
        if "icon-[mdi--" + icon + "]" in span.get("class", []):
            parent = span.find_parent("a") or span.parent
            value = _text(parent)
            if value and value not in values:
                values.append(value)
    return "; ".join(values)


def _metadata(container: Tag) -> dict[str, str]:
    result = {"language": "", "extension": "", "size": "", "year": ""}
    languages = []
    for element in container.select("div.text-gray-800, div.text-gray-500"):
        line = _text(element)
        for match in _LANGUAGE.finditer(line):
            language = match.group(1).strip().lstrip("✅✔✓ ") + " [" + match.group(2).lower() + "]"
            if language not in languages:
                languages.append(language)
        # Format/year must be discrete metadata fields, not words in a title or filename.
        for part in re.split(r"[·\n]", line):
            part = part.strip().lstrip("✅✔✓ ")
            fmt = _FORMAT.fullmatch(part)
            if fmt and not result["extension"]:
                result["extension"] = fmt.group(1).lower()
            size = _SIZE.search(part)
            if size and not result["size"]:
                result["size"] = size.group()
            if re.fullmatch(r"(?:1[4-9]|20)\d{2}", part) and not result["year"]:
                result["year"] = part
    result["language"] = "; ".join(languages)
    # Modern cards often carry publication year in the publisher row.
    if not result["year"]:
        publisher = _icon_text(container, "company")
        years = _YEAR.findall(publisher)
        if len(set(years)) == 1:
            result["year"] = years[0]
    return result


class Annas:
    source = "annas"

    def __init__(self, http: Http, base_url: str, credentials: dict | None = None, session: dict | None = None):
        validate_url(base_url)
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.credentials = credentials or {}

    @staticmethod
    def _md5(book_id: str, book_hash: str) -> str:
        value = book_hash or book_id
        if not isinstance(value, str) or not _MD5.fullmatch(value):
            raise BookError("invalid_argument", "Anna's Archive requires a 32-character MD5 book identifier.")
        return value.lower()

    @staticmethod
    def _page(html: str) -> BeautifulSoup:
        soup = BeautifulSoup(html, "html.parser")
        title = _text(soup.title).casefold()
        if any(word in title for word in ("just a moment", "ddos-guard", "checking your browser", "attention required", "security verification")) or soup.select_one("#challenge-form, #cf-challenge-running, script[src*='challenge-platform']"):
            raise BookError("provider_challenge", "Anna's Archive requires browser verification; this client cannot solve it.")
        # Anna deliberately hides later search cards in HTML comments for its UI.
        for comment in list(soup.find_all(string=lambda value: isinstance(value, Comment))):
            if "/md5/" in str(comment):
                fragment = BeautifulSoup(str(comment), "html.parser")
                comment.replace_with(fragment)
        return soup

    def _book(self, container: Tag, md5: str, title: str) -> Book:
        return Book(source=self.source, id=md5, hash=md5, title=title,
                    author=_icon_text(container, "user-edit"), publisher=_icon_text(container, "company"),
                    url=self.base_url + "/md5/" + md5, **_metadata(container))

    def search(self, query: SearchQuery) -> list[Book]:
        params = {"q": query.query, "content": "book_any", "page": query.page}
        if query.language:
            params["lang"] = normalize_language(query.language)
        if query.extension:
            params["ext"] = query.extension.lower().lstrip(".")
        soup = self._page(self.http.text(self.base_url + "/search", params=params))
        books, seen = [], set()
        for container in soup.select("div.max-w-full"):
            for link in container.select("a[href^='/md5/']"):
                match = re.fullmatch(r"/md5/([a-fA-F0-9]{32})/?", urlsplit(link.get("href", "")).path)
                title = _text(link)
                if not match or not title:
                    continue
                md5 = match.group(1).lower()
                if md5 in seen:
                    continue
                seen.add(md5)
                books.append(self._book(container, md5, title))
        if not books:
            text = _text(soup).casefold()
            no_results = re.search(r"\b(?:no (?:files|results|books|matches) found|no results|nothing found|0 results|we did not find any)\b", text)
            # Broken result markup is never mistaken for a genuine empty search.
            if not no_results or soup.select_one("a[href^='/md5/']"):
                raise _schema()
        return filter_books(books, query)

    def info(self, book_id: str, book_hash: str) -> Book:
        md5 = self._md5(book_id, book_hash)
        soup = self._page(self.http.text(self.base_url + "/md5/" + md5))
        title_element = soup.select_one("h1, div.text-3xl.font-bold, div.text-xl.font-bold")
        title = _text(title_element)
        if not title:
            page_title = _text(soup.title)
            if re.search(r"\s[-–—]\sAnna(?:'s|’s) Archive\s*$", page_title):
                title = re.sub(r"\s[-–—]\sAnna(?:'s|’s) Archive\s*$", "", page_title).strip()
        if not title or title.casefold() in {"not found", "404", "error", "page not found"}:
            raise _schema()
        container = title_element.parent if title_element else (soup.select_one("main") or soup.body or soup)
        # Metadata is usually a sibling of the title, with author/publisher a level above.
        while container.parent and container.name not in {"main", "body", "[document]"}:
            if container.select_one("div.text-gray-800, div.text-gray-500"):
                break
            container = container.parent
        book = self._book(container, md5, title)
        if not book.extension and not book.language:
            raise _schema()
        return book

    def download_ticket(self, book_id: str, book_hash: str) -> DownloadTicket:
        md5 = self._md5(book_id, book_hash)
        key = self.credentials.get("api_key")
        if not isinstance(key, str) or not key.strip():
            raise BookError("credentials_missing", "An Anna's Archive API key is required for downloads, not search or info.")
        # Resolve metadata before spending API quota; HTML challenges remain explicit.
        book = self.info(md5, md5)
        if not book.extension:
            raise _schema()
        payload = self.http.json("GET", self.base_url + "/dyn/api/fast_download.json", params={"md5": md5, "key": key})
        if not isinstance(payload, dict):
            raise _schema()
        error = payload.get("error")
        if error:
            if not isinstance(error, str):
                raise _schema()
            # Echoed credentials must not influence semantic error classification.
            category = error.replace(key, "").casefold()
            if re.search(r"\b(?:limit|limits|quota)\b", category):
                raise BookError("quota_exceeded", "Anna's Archive reports that the download quota is exhausted.")
            if re.search(r"\b(?:key|auth(?:entication|orization)?|unauthori[sz]ed|member(?:ship)?|donat(?:ion|e))\b", category):
                raise BookError("auth_failed", "Anna's Archive rejected the API key or download entitlement.")
            raise BookError("provider_error", "Anna's Archive could not resolve a download for this book.")
        url = payload.get("download_url")
        if not isinstance(url, str) or not url.strip():
            raise _schema()
        validate_url(url)
        return DownloadTicket(url=url, filename=book.title + "." + book.extension, extension=book.extension, source=self.source, expected_md5=md5)
