"""Provider protocol fixtures: metadata, strict filters, authentication and safe failures."""
from urllib.parse import parse_qs

import httpx
import pytest

from book_tools.errors import BookError
from book_tools.http import Http
from book_tools.models import SearchQuery
from book_tools.providers import Annas, ZLibrary


MD5 = "0123456789abcdef0123456789abcdef"
OTHER_MD5 = "abcdef0123456789abcdef0123456789"
TOKENS = {"remix_userid": "123", "remix_userkey": "fixture-token-not-a-secret"}


@pytest.fixture
def http_factory():
    clients = []

    def make(handler):
        http = Http()
        http.client.close()
        http.client = httpx.Client(transport=httpx.MockTransport(handler))
        clients.append(http)
        return http

    yield make
    for http in clients:
        http.client.close()


def response(payload, status=200):
    return httpx.Response(status, json=payload)


def zbook(**overrides):
    return {"id": 42, "hash": "book-hash", "title": "A real title", "author": "A. Author", "publisher": "A Press", "year": "2021", "language": "English", "extension": "EPUB", "filesizeString": "1.4 MB", **overrides}


def zprovider(http_factory, operation):
    def handler(request):
        if request.url.path == "/eapi/user/profile":
            return response({"success": 1, "user": {"id": 123, "remix_userkey": TOKENS["remix_userkey"], "downloads_limit": 10, "downloads_today": 3}})
        return operation(request)

    provider = ZLibrary(http_factory(handler), "https://zlib.test", session=TOKENS)
    provider.authenticate()
    return provider


def card(md5=MD5, *, title="A useful book", meta="✅ English [en] · EPUB · 1.4MB · 2021", publisher="A Press", author="Ada Author"):
    return f'''<div class="result"><a href="/md5/{md5}"><img src="/cover.jpg"></a>
    <div class="max-w-full"><a href="/md5/{md5}">{title}</a>
    <a href="/search?q=author"><span class="icon-[mdi--user-edit]"></span>{author}</a>
    <a href="/search?q=publisher"><span class="icon-[mdi--company]"></span>{publisher}</a>
    <div class="text-gray-800">{meta}</div></div></div>'''


def detail(meta="English [en] · EPUB · 1.4MB · 2021"):
    return f'''<html><head><title>A useful book - Anna's Archive</title></head><body><main>
    <div class="text-3xl font-bold">A useful book</div>
    <a href="/search?q=author"><span class="icon-[mdi--user-edit]"></span>Ada Author</a>
    <a href="/search?q=publisher"><span class="icon-[mdi--company]"></span>A Press</a>
    <div class="text-gray-500">{meta}</div></main></body></html>'''


def anna_with_html(http_factory, html):
    return Annas(http_factory(lambda request: httpx.Response(200, text=html)), "https://anna.test")


def test_zlib_metadata_and_strict_local_filters(http_factory):
    def operation(request):
        form = parse_qs(request.content.decode())
        assert form["languages"] == ["english"]
        assert form["extensions[]"] == ["epub"]
        return response({"success": 1, "books": [zbook(), zbook(id=43, year=""), zbook(id=44, language="Chinese"), zbook(id=45, extension="PDF")]})

    books = zprovider(http_factory, operation).search(SearchQuery("books", language="en", extension=".EPUB", year_from=2020, year_to=2022))
    assert len(books) == 1
    book = books[0]
    assert (book.id, book.hash, book.title, book.author, book.publisher) == ("42", "book-hash", "A real title", "A. Author", "A Press")
    assert (book.year, book.language, book.extension, book.size) == ("2021", "English", "epub", "1.4 MB")
    assert book.url == "https://zlib.test/book/42/book-hash"


def test_zlib_info_normalizes_metadata(http_factory):
    provider = zprovider(http_factory, lambda request: response({"success": 1, "book": zbook(year=2021)}))
    book = provider.info("42", "book-hash")
    assert book.source == "zlib" and book.year == "2021" and book.extension == "epub"


@pytest.mark.parametrize("payload", [{"success": 1}, {"success": 1, "books": {}}, {"success": 1, "books": [{"id": 1, "title": "Missing hash"}]}, {"success": "yes", "books": []}])
def test_zlib_schema_drift_is_not_empty_success(http_factory, payload):
    provider = zprovider(http_factory, lambda request: response(payload))
    with pytest.raises(BookError) as caught:
        provider.search(SearchQuery("anything"))
    assert caught.value.code == "provider_schema"


def test_zlib_valid_empty_search(http_factory):
    provider = zprovider(http_factory, lambda request: response({"success": 1, "books": []}))
    assert provider.search(SearchQuery("not found")) == []


def test_zlib_session_auth_does_not_return_profile_secrets(http_factory):
    def handler(request):
        assert "remix_userkey=fixture-token-not-a-secret" in request.headers["cookie"]
        return response({"success": 1, "user": {"id": 123, "email": "not-returned@example.test", "name": "Private", "remix_userkey": TOKENS["remix_userkey"]}})

    provider = ZLibrary(http_factory(handler), "https://zlib.test", session={**TOKENS, "email": "ignored@example.test"})
    assert provider.authenticate() == TOKENS


@pytest.mark.parametrize("expired_response", [response({"success": 0, "error": "Invalid session"}), response({}, 401), response({}, 403)])
def test_zlib_rejected_session_falls_back_to_login(http_factory, expired_response):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("profile"):
            return expired_response
        return response({"success": 1, "user": {"id": 456, "remix_userkey": "replacement-fixture-token"}})

    provider = ZLibrary(http_factory(handler), "https://zlib.test", session=TOKENS, credentials={"email": "fixture@example.test", "password": "fixture-password"})
    assert provider.authenticate() == {"remix_userid": "456", "remix_userkey": "replacement-fixture-token"}
    assert calls == ["/eapi/user/profile", "/eapi/user/login"]


def test_zlib_network_failure_does_not_try_other_credentials(http_factory):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        raise httpx.ConnectError("untrusted secret URL", request=request)

    provider = ZLibrary(http_factory(handler), "https://zlib.test", session=TOKENS, credentials={"email": "fixture@example.test", "password": "fixture-password"})
    with pytest.raises(BookError) as caught:
        provider.authenticate()
    assert caught.value.code == "network_error" and caught.value.recoverable
    assert "secret" not in str(caught.value)
    assert calls == ["/eapi/user/profile"]


@pytest.mark.parametrize("credentials,session,expected", [({}, {}, "credentials_missing"), ({"remix_userid": "123"}, {}, "credentials_missing"), ({}, TOKENS, "auth_failed")])
def test_zlib_missing_or_rejected_authentication(http_factory, credentials, session, expected):
    provider = ZLibrary(http_factory(lambda request: response({"success": 0, "error": "secret key in remote failure"})), "https://zlib.test", credentials=credentials, session=session)
    with pytest.raises(BookError) as caught:
        provider.authenticate()
    assert caught.value.code == expected
    assert "secret key" not in str(caught.value)


def test_zlib_download_ticket_is_metadata_only(http_factory):
    provider = zprovider(http_factory, lambda request: response({"success": 1, "file": {"downloadLink": "https://files.test/book?token=fixture", "description": "Title", "author": "Author", "extension": "EPUB", "md5": MD5}}))
    ticket = provider.download_ticket("42", "book-hash")
    assert (ticket.filename, ticket.extension, ticket.source, ticket.expected_md5) == ("Title (Author).epub", "epub", "zlib", MD5)
    assert ticket.url.startswith("https://files.test/")


@pytest.mark.parametrize("url,extension,expected", [("http://unsafe.test/book", "epub", "invalid_url"), ("https://files.test/book", "", "provider_schema"), ("https://files.test/book", "../epub", "provider_schema")])
def test_zlib_rejects_invalid_tickets(http_factory, url, extension, expected):
    provider = zprovider(http_factory, lambda request: response({"success": 1, "file": {"downloadLink": url, "extension": extension}}))
    with pytest.raises(BookError) as caught:
        provider.download_ticket("42", "hash")
    assert caught.value.code == expected


def test_zlib_quota_contains_only_validated_counters(http_factory):
    provider = zprovider(http_factory, lambda request: pytest.fail("Unexpected request"))
    assert provider.quota() == {"downloads_limit": 10, "downloads_today": 3, "downloads_left": 7}


@pytest.mark.parametrize("counter", [None, True, -1, "unlimited"])
def test_zlib_quota_never_invents_missing_counters(http_factory, counter):
    provider = ZLibrary(http_factory(lambda request: response({"success": 1, "user": {"id": 123, "remix_userkey": TOKENS["remix_userkey"], "downloads_limit": counter, "downloads_today": 1}})), "https://zlib.test", session=TOKENS)
    provider.authenticate()
    with pytest.raises(BookError) as caught:
        provider.quota()
    assert caught.value.code == "provider_schema"


def test_anna_anonymous_metadata_comments_and_deduplication(http_factory):
    html = card() + "<!--" + card() + card(OTHER_MD5, title="Second book", meta="Chinese [zh] · PDF · 4.2 MB · 2019") + "-->"
    books = anna_with_html(http_factory, html).search(SearchQuery("query"))
    assert [book.id for book in books] == [MD5, OTHER_MD5]
    book = books[0]
    assert (book.title, book.author, book.publisher, book.year, book.language, book.extension, book.size) == ("A useful book", "Ada Author", "A Press", "2021", "English [en]", "epub", "1.4MB")
    assert book.url == "https://anna.test/md5/" + MD5


def test_anna_strict_filters_ignore_remote_filter_claims(http_factory):
    html = card(meta="English [en] · EPUB · 1MB · 2021") + card(OTHER_MD5, meta="Chinese [zh] · PDF · 2MB · 2010")
    provider = anna_with_html(http_factory, html)
    assert [book.id for book in provider.search(SearchQuery("query", language="english", extension="EPUB", year_from=2020, year_to=2022))] == [MD5]
    assert provider.search(SearchQuery("query", language="zh", extension="epub")) == []


def test_anna_unknown_metadata_does_not_satisfy_filters(http_factory):
    provider = anna_with_html(http_factory, card(meta="3 MB"))
    assert len(provider.search(SearchQuery("query"))) == 1
    for query in (SearchQuery("query", language="en"), SearchQuery("query", extension="epub"), SearchQuery("query", year_from=1900)):
        assert provider.search(query) == []


def test_anna_multilanguage_and_publisher_year(http_factory):
    provider = anna_with_html(http_factory, card(meta="✅ English [en] · Hindi [hi] · EPUB · 0.7MB", publisher="Example Press, 2015"))
    books = provider.search(SearchQuery("query", language="hi", year_from=2015, year_to=2015))
    assert len(books) == 1 and books[0].year == "2015"
    assert "English [en]" in books[0].language and "Hindi [hi]" in books[0].language


@pytest.mark.parametrize("html,code", [("", "provider_schema"), ("<h1>Unexpected site redesign</h1>", "provider_schema"), ("<title>Just a moment...</title><h1>No results</h1>", "provider_challenge"), ("<title>DDoS-Guard</title>", "provider_challenge"), ("<form id='challenge-form'></form>", "provider_challenge"), ("<div>No results</div><a href='/md5/broken'>Bad result</a>", "provider_schema")])
def test_anna_drift_and_challenges_are_not_empty_success(http_factory, html, code):
    with pytest.raises(BookError) as caught:
        anna_with_html(http_factory, html).search(SearchQuery("query"))
    assert caught.value.code == code


def test_anna_recognized_empty_search(http_factory):
    assert anna_with_html(http_factory, "<main>No files found.</main>").search(SearchQuery("query")) == []


def test_anna_info_is_anonymous_and_normalized(http_factory):
    book = anna_with_html(http_factory, detail()).info(MD5.upper(), "")
    assert (book.id, book.hash, book.title, book.author, book.extension, book.year) == (MD5, MD5, "A useful book", "Ada Author", "epub", "2021")


@pytest.mark.parametrize("identifier", ["", "../secret", "not-a-md5", "a" * 31])
def test_anna_rejects_invalid_md5_before_network(http_factory, identifier):
    provider = Annas(http_factory(lambda request: pytest.fail("Invalid ID must not reach HTTP")), "https://anna.test")
    with pytest.raises(BookError) as caught:
        provider.info(identifier, "")
    assert caught.value.code == "invalid_argument"


def test_anna_download_requires_key_not_search_credentials(http_factory):
    provider = Annas(http_factory(lambda request: pytest.fail("Missing key must not reach HTTP")), "https://anna.test")
    with pytest.raises(BookError) as caught:
        provider.download_ticket(MD5, "")
    assert caught.value.code == "credentials_missing"


def anna_ticket_provider(http_factory, payload):
    def handler(request):
        if request.url.path == "/md5/" + MD5:
            return httpx.Response(200, text=detail())
        if request.url.path == "/dyn/api/fast_download.json":
            return response(payload)
        pytest.fail("Ticket resolution must not download the binary")

    return Annas(http_factory(handler), "https://anna.test", credentials={"api_key": "fixture-api-key"})


def test_anna_json_ticket_resolution_without_binary(http_factory):
    ticket = anna_ticket_provider(http_factory, {"download_url": "https://files.test/book?token=fixture", "error": ""}).download_ticket(MD5, "")
    assert ticket.filename == "A useful book.epub" and ticket.expected_md5 == MD5 and ticket.source == "annas"


@pytest.mark.parametrize("payload,code", [({}, "provider_schema"), ({"download_url": None}, "provider_schema"), ({"download_url": "http://files.test/book"}, "invalid_url"), ({"error": {"private": "fixture-api-key"}}, "provider_schema"), ({"error": "invalid key fixture-api-key https://secret.test/"}, "auth_failed"), ({"error": "download quota exhausted fixture-api-key"}, "quota_exceeded"), ({"error": "unavailable fixture-api-key"}, "provider_error")])
def test_anna_ticket_faults_are_explicit_and_sanitized(http_factory, payload, code):
    with pytest.raises(BookError) as caught:
        anna_ticket_provider(http_factory, payload).download_ticket(MD5, "")
    assert caught.value.code == code
    assert "fixture-api-key" not in str(caught.value) and "secret.test" not in str(caught.value)
