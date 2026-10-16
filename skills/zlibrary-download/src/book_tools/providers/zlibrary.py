"""Small authenticated Z-Library EAPI client (protocol from MIT Zlibrary.py)."""
from __future__ import annotations

import re
from urllib.parse import quote

from ..errors import BookError
from ..http import validate_url
from ..models import Book, DownloadTicket, SearchQuery, filter_books, normalize_language
from ..transport import JsonTransport


def _text(value: object) -> str:
    return str(value).strip() if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""


def _schema() -> BookError:
    return BookError("provider_schema", "Z-Library returned an unsupported API response.")


class ZLibrary:
    source = "zlib"

    def __init__(self, http: JsonTransport, base_url: str, credentials: dict | None = None, session: dict | None = None):
        validate_url(base_url)
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.credentials = credentials or {}
        self.session = session or {}
        self._tokens: dict[str, str] = {}

    @staticmethod
    def _tokens_from(values: dict) -> dict[str, str]:
        user_id = _text(values.get("remix_userid"))
        key = _text(values.get("remix_userkey"))
        if not user_id or not key:
            return {}
        return {"remix_userid": user_id, "remix_userkey": key}

    def _request(self, method: str, path: str, *, tokens: dict | None = None, auth: bool = False, **kwargs) -> dict:
        cookies = {"siteLanguageV2": "en", **(self._tokens if tokens is None else tokens)}
        if not auth and not self._tokens:
            raise BookError("credentials_missing", "Authenticate with Z-Library before using this operation.")
        try:
            payload = self.http.json(method, self.base_url + path, cookies=cookies, **kwargs)
        except BookError as exc:
            if auth and exc.code in {"access_denied", "auth_failed"}:
                raise BookError("auth_failed", "Z-Library rejected the credentials or session.") from None
            raise
        if not isinstance(payload, dict) or payload.get("success") not in (True, 1, False, 0):
            raise _schema()
        if not payload["success"]:
            if auth:
                raise BookError("auth_failed", "Z-Library rejected the credentials or session.")
            # Only classify known error categories; never expose remote text or URLs.
            error = str(payload.get("error", "")).casefold()
            if any(word in error for word in ("login", "unauthorized", "session", "authentication")):
                self._tokens = {}
                raise BookError("auth_failed", "The Z-Library session is no longer valid.")
            if any(word in error for word in ("limit", "quota")):
                raise BookError("quota_exceeded", "Z-Library reports that the download quota is exhausted.")
            raise BookError("provider_error", "Z-Library could not complete the requested operation.")
        return payload

    def _accept_user(self, payload: dict, fallback: dict | None = None) -> dict:
        user = payload.get("user")
        if not isinstance(user, dict) or not _text(user.get("id")):
            raise _schema()
        key = _text(user.get("remix_userkey")) or (fallback or {}).get("remix_userkey", "")
        if not key:
            raise _schema()
        self._tokens = {"remix_userid": _text(user["id"]), "remix_userkey": key}
        return dict(self._tokens)

    def login(self, email: str, password: str) -> dict:
        if not email or not password:
            raise BookError("credentials_missing", "Z-Library requires both email and password.")
        self._tokens = {}
        payload = self._request("POST", "/eapi/user/login", auth=True, tokens={}, data={"email": email, "password": password})
        return self._accept_user(payload)

    def authenticate(self) -> dict:
        if self._tokens:
            return dict(self._tokens)
        rejected = False
        for values in (self.session, self.credentials):
            tokens = self._tokens_from(values)
            if not tokens:
                continue
            try:
                payload = self._request("GET", "/eapi/user/profile", tokens=tokens, auth=True)
                return self._accept_user(payload, tokens)
            except BookError as exc:
                if exc.code != "auth_failed":
                    raise
                rejected = True
        email, password = self.credentials.get("email"), self.credentials.get("password")
        if email and password:
            return self.login(email, password)
        if rejected:
            raise BookError("auth_failed", "Z-Library rejected the credentials or session.")
        raise BookError("credentials_missing", "Configure Z-Library token credentials or email and password.")

    def _book(self, item: dict) -> Book:
        if not isinstance(item, dict):
            raise _schema()
        book_id, book_hash, title = (_text(item.get(key)) for key in ("id", "hash", "title"))
        if not book_id or not book_hash or not title:
            raise _schema()
        return Book(
            source=self.source, id=book_id, hash=book_hash, title=title,
            author=_text(item.get("author")), publisher=_text(item.get("publisher")),
            year=_text(item.get("year")), language=_text(item.get("language")),
            extension=_text(item.get("extension")).lower().lstrip("."),
            size=_text(item.get("filesizeString")) or _text(item.get("filesize")),
            url=self.base_url + "/book/" + quote(book_id, safe="") + "/" + quote(book_hash, safe=""),
        )

    @staticmethod
    def _path(book_id: str, book_hash: str) -> str:
        if not book_id or not book_hash:
            raise BookError("invalid_argument", "Z-Library requires both a book ID and hash.")
        return "/eapi/book/" + quote(str(book_id), safe="") + "/" + quote(str(book_hash), safe="")

    def search(self, query: SearchQuery) -> list[Book]:
        language_names = {"en": "english", "zh": "chinese", "de": "german", "fr": "french", "es": "spanish", "ja": "japanese", "ru": "russian", "it": "italian", "pt": "portuguese"}
        language = normalize_language(query.language) if query.language else None
        data = {"message": query.query, "page": query.page, "limit": query.limit}
        for key, value in {"yearFrom": query.year_from, "yearTo": query.year_to, "languages": language_names.get(language, language), "extensions[]": query.extension.lower().lstrip(".") if query.extension else None}.items():
            if value is not None:
                data[key] = value
        payload = self._request("POST", "/eapi/book/search", data=data)
        items = payload.get("books")
        if not isinstance(items, list):
            raise _schema()
        return filter_books([self._book(item) for item in items], query)

    def info(self, book_id: str, book_hash: str) -> Book:
        payload = self._request("GET", self._path(book_id, book_hash))
        return self._book(payload.get("book"))

    def download_ticket(self, book_id: str, book_hash: str) -> DownloadTicket:
        payload = self._request("GET", self._path(book_id, book_hash) + "/file")
        item = payload.get("file")
        if not isinstance(item, dict):
            raise _schema()
        url, extension = _text(item.get("downloadLink")), _text(item.get("extension")).lower().lstrip(".")
        if not url or not re.fullmatch(r"[a-z0-9]+", extension):
            raise _schema()
        validate_url(url)
        title = _text(item.get("description")) or str(book_id)
        author = _text(item.get("author"))
        filename = title + (f" ({author})" if author else "") + "." + extension
        md5 = _text(item.get("md5"))
        if md5 and not re.fullmatch(r"[a-fA-F0-9]{32}", md5):
            raise _schema()
        return DownloadTicket(url=url, filename=filename, extension=extension, source=self.source, expected_md5=md5.lower() or None)

    def quota(self) -> dict:
        payload = self._request("GET", "/eapi/user/profile")
        user = payload.get("user")
        if not isinstance(user, dict):
            raise _schema()
        values = []
        for key in ("downloads_limit", "downloads_today"):
            value = user.get(key)
            if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value)):
                raise _schema()
            values.append(int(value))
        limit, today = values
        return {"downloads_limit": limit, "downloads_today": today, "downloads_left": max(0, limit - today)}
