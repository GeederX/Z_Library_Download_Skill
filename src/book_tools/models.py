"""Provider-neutral metadata; unknown fields never satisfy explicit filters."""
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Book:
    source: str
    id: str
    hash: str
    title: str
    author: str = ""
    publisher: str = ""
    year: str = ""
    language: str = ""
    extension: str = ""
    size: str = ""
    url: str = ""


@dataclass(frozen=True)
class SearchQuery:
    query: str
    language: str | None = None
    extension: str | None = None
    year_from: int | None = None
    year_to: int | None = None
    limit: int = 10
    page: int = 1


@dataclass(frozen=True)
class DownloadTicket:
    url: str
    filename: str
    extension: str
    source: str
    expected_md5: str | None = None


_LANGUAGE_ALIASES = {
    "en": "en", "eng": "en", "english": "en",
    "zh": "zh", "zho": "zh", "chi": "zh", "chinese": "zh", "中文": "zh",
    "de": "de", "german": "de", "fr": "fr", "french": "fr",
    "es": "es", "spanish": "es", "ja": "ja", "japanese": "ja",
    "ru": "ru", "russian": "ru", "it": "it", "italian": "it",
    "pt": "pt", "portuguese": "pt",
}


def normalize_language(value: str) -> str:
    value = value.strip().casefold()
    bracket = re.search(r"\[([a-z]{2,3}(?:-[a-z0-9]+)*)\]", value)
    if bracket:
        value = bracket.group(1)
    if re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]+)+", value):
        value = value.split("-", 1)[0]
    return _LANGUAGE_ALIASES.get(value, value)


def matches(book: Book, query: SearchQuery) -> bool:
    if query.language:
        requested = normalize_language(query.language)
        languages = re.split(r"[,;·/]", book.language)
        if not any(normalize_language(part) == requested for part in languages):
            return False
    if query.extension and book.extension.casefold().lstrip(".") != query.extension.casefold().lstrip("."):
        return False
    if query.year_from is not None or query.year_to is not None:
        match = re.fullmatch(r"\d{4}", str(book.year).strip())
        if not match:
            return False
        year = int(match.group())
        if query.year_from is not None and year < query.year_from:
            return False
        if query.year_to is not None and year > query.year_to:
            return False
    return True


def filter_books(books: list[Book], query: SearchQuery) -> list[Book]:
    return [book for book in books if matches(book, query)][:query.limit]
