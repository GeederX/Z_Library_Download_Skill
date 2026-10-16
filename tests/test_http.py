import httpx
import pytest

from book_tools.errors import BookError
from book_tools.http import Http, validate_url


@pytest.mark.parametrize("url", ["http://example.com", "file:///tmp/book", "https://user:password@example.com", "https://example.com/#secret", "not a url"])
def test_unsafe_urls_are_rejected(url):
    with pytest.raises(BookError):
        validate_url(url)


def test_local_http_is_explicitly_opt_in(monkeypatch):
    monkeypatch.delenv("BOOK_TOOLS_ALLOW_HTTP", raising=False)
    with pytest.raises(BookError):
        validate_url("http://127.0.0.1:1234")
    monkeypatch.setenv("BOOK_TOOLS_ALLOW_HTTP", "1")
    assert validate_url("http://127.0.0.1:1234") == "http://127.0.0.1:1234"
    with pytest.raises(BookError):
        validate_url("http://example.com")


def transport_http(handler):
    http = Http()
    http.client.close()
    http.client = httpx.Client(transport=httpx.MockTransport(handler))
    return http


def test_api_redirect_refused_before_credentials_can_be_forwarded():
    visited = []
    def handler(request):
        visited.append(request.url.host)
        return httpx.Response(302, headers={"location": "https://different.example"})
    with transport_http(handler) as http:
        with pytest.raises(BookError) as caught:
            http.json("POST", "https://trusted.example/login", data={"password": "private"})
    assert visited == ["trusted.example"]
    assert caught.value.code == "redirect_refused"


def test_transport_exception_does_not_leak_query_key():
    def handler(request):
        raise httpx.ReadTimeout("contains private-api-key", request=request)
    with transport_http(handler) as http:
        with pytest.raises(BookError) as caught:
            http.json("GET", "https://trusted.example/download?key=private-api-key")
    assert caught.value.code == "timeout"
    assert caught.value.recoverable is True
    assert "private-api-key" not in str(caught.value)


def test_challenge_html_is_not_a_json_success():
    with transport_http(lambda request: httpx.Response(200, text="<html>challenge</html>")) as http:
        with pytest.raises(BookError) as caught:
            http.json("GET", "https://trusted.example/search")
    assert caught.value.code == "invalid_response"


@pytest.mark.parametrize("status,recoverable", [(401, False), (403, False), (429, True), (503, True)])
def test_http_status_has_explicit_recovery_policy(status, recoverable):
    with transport_http(lambda request: httpx.Response(status, text="secret response body")) as http:
        with pytest.raises(BookError) as caught:
            http.json("GET", "https://trusted.example/search")
    assert caught.value.recoverable == recoverable
    assert "secret response body" not in str(caught.value)


def test_anonymous_same_origin_redirect_keeps_destination_query_without_replaying_old_params():
    urls = []
    def handler(request):
        urls.append(str(request.url))
        if len(urls) == 1:
            return httpx.Response(302, headers={"location": "/search?q=book&check=1"})
        return httpx.Response(200, text="<main>No results</main>")
    with transport_http(handler) as http:
        result = http.text("https://trusted.example/search", params={"q": "book", "page": 2})
    assert result == "<main>No results</main>"
    assert urls[-1] == "https://trusted.example/search?q=book&check=1"


def test_anonymous_redirect_does_not_discover_another_mirror():
    visited = []
    def handler(request):
        visited.append(request.url.host)
        return httpx.Response(302, headers={"location": "https://another.example/search"})
    with transport_http(handler) as http:
        with pytest.raises(BookError) as caught:
            http.text("https://trusted.example/search")
    assert caught.value.code == "redirect_refused"
    assert visited == ["trusted.example"]


def test_raw_deadline_fires_even_before_compressed_body_produces_decoded_bytes(monkeypatch):
    from book_tools.http import _DeadlineStream
    class IncompleteGzipHeader(httpx.SyncByteStream):
        def __iter__(self):
            yield b"\x1f"
            yield b"\x8b"
    times = iter((0, 301))
    monkeypatch.setattr("book_tools.http.time.monotonic", lambda: next(times))
    response = httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=_DeadlineStream(IncompleteGzipHeader(), 300))
    with pytest.raises(BookError) as caught:
        list(response.iter_bytes())
    assert caught.value.code == "timeout"
    assert caught.value.recoverable is True


@pytest.mark.parametrize("status", [307, 308])
@pytest.mark.parametrize("location", ["/authenticated?step=2", "https://TRUSTED.example:443/authenticated?step=2"])
def test_api_same_origin_redirect_preserves_post_and_chain_cookies_but_not_query_or_later_credentials(status, location):
    from urllib.parse import parse_qs
    visited = []
    def handler(request):
        visited.append((request.url.host, request.url.path))
        if request.url.path == "/login":
            return httpx.Response(status, headers={"location": location, "set-cookie": "gate=allowed; Path=/; Secure"})
        if request.url.path == "/authenticated":
            valid = (
                request.method == "POST"
                and parse_qs(request.content.decode()) == {"password": ["private-value"]}
                and dict(request.url.params) == {"step": "2"}
                and request.headers.get("content-type") == "application/x-www-form-urlencoded"
                and "gate=allowed" in request.headers.get("cookie", "")
                and "session=private-session" in request.headers.get("cookie", "")
            )
            return httpx.Response(200 if valid else 401, json={"authenticated": valid})
        # Neither explicitly supplied cookies nor redirect cookies survive the chain.
        return httpx.Response(200, json={"anonymous": "cookie" not in request.headers})
    with transport_http(handler) as http:
        result = http.json("POST", "https://trusted.example/login?original=private-query",
                           data={"password": "private-value"}, cookies={"session": "private-session"})
        later = http.json("GET", "https://different.example/status")
    assert result == {"authenticated": True}
    assert later == {"anonymous": True}
    assert visited == [("trusted.example", "/login"), ("trusted.example", "/authenticated"), ("different.example", "/status")]


@pytest.mark.parametrize("status", [307, 308])
@pytest.mark.parametrize("destination", ["https://different.example/next", "https://trusted.example:8443/next", "http://trusted.example/next"])
def test_api_preserving_redirect_refuses_changed_origin_before_sending_secrets(status, destination):
    visited = []
    def handler(request):
        visited.append(str(request.url))
        return httpx.Response(status, headers={"location": destination})
    with transport_http(handler) as http:
        with pytest.raises(BookError) as caught:
            http.json("POST", "https://trusted.example/login", data={"password": "private"}, cookies={"session": "private"})
    assert caught.value.code in {"redirect_refused", "invalid_url"}
    assert visited == ["https://trusted.example/login"]
    assert "private" not in str(caught.value)


@pytest.mark.parametrize("status", [301, 302, 303])
def test_api_method_changing_redirect_is_not_followed_even_on_same_origin(status):
    visited = []
    def handler(request):
        visited.append(request.method)
        return httpx.Response(status, headers={"location": "/next"})
    with transport_http(handler) as http:
        with pytest.raises(BookError) as caught:
            http.json("POST", "https://trusted.example/login", data={"password": "private"})
    assert caught.value.code == "redirect_refused"
    assert visited == ["POST"]


def test_api_same_url_cookie_handshake_is_allowed_but_loop_is_bounded():
    calls = []
    def handshake(request):
        calls.append(request)
        if "gate=allowed" not in request.headers.get("cookie", ""):
            return httpx.Response(307, headers={"location": str(request.url), "set-cookie": "gate=allowed; Path=/; Secure"})
        return httpx.Response(200, json={"success": True})
    with transport_http(handshake) as http:
        assert http.json("POST", "https://trusted.example/search", data={"q": "book"}) == {"success": True}
    assert len(calls) == 2

    calls.clear()
    def loop(request):
        calls.append(request)
        return httpx.Response(307, headers={"location": str(request.url)})
    with transport_http(loop) as http:
        with pytest.raises(BookError) as caught:
            http.json("POST", "https://trusted.example/search", data={"q": "book"})
    assert caught.value.code == "redirect_limit"
    assert len(calls) == 6  # Original request plus at most five redirects.


def test_api_missing_location_and_failure_both_clear_redirect_cookies():
    def handler(request):
        if request.url.path == "/bad":
            return httpx.Response(307, headers={"set-cookie": "gate=private; Path=/; Secure"})
        return httpx.Response(200, json={"anonymous": "cookie" not in request.headers})
    with transport_http(handler) as http:
        with pytest.raises(BookError) as caught:
            http.json("GET", "https://trusted.example/bad", cookies={"session": "private"})
        assert caught.value.code == "invalid_response"
        assert http.json("GET", "https://trusted.example/status") == {"anonymous": True}


@pytest.mark.parametrize("status", [403, 503, 513])
def test_browser_verification_is_not_treated_as_bad_credentials_or_retryable_outage(status):
    body = "<html><title>Verifying your browser | DiamWall</title><body>private-server-detail</body></html>"
    with transport_http(lambda request: httpx.Response(status, text=body, headers={"Content-Type": "text/html"})) as http:
        with pytest.raises(BookError) as caught:
            http.json("POST", "https://trusted.example/search", data={"q": "book"})
    assert caught.value.code == "provider_challenge"
    assert caught.value.recoverable is False
    assert str(status) in str(caught.value)
    assert "private-server-detail" not in str(caught.value)


@pytest.mark.parametrize("title,code", [
    ("Verifying your browser | Fixture", "provider_challenge"),
    ("Permission denied", "access_denied"),
])
def test_streamed_error_html_is_bounded_and_classified(monkeypatch, title, code):
    class ErrorStream(httpx.SyncByteStream):
        def __iter__(self):
            yield f"<title>{title}</title>".encode()
            yield b"x" * 8192
            pytest.fail("Must not read an entire error page")

    client = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(
        403, headers={"content-type": "text/html"}, stream=ErrorStream(),
    ))
    with Http() as http:
        monkeypatch.setattr("book_tools.http.httpx.Client", lambda **kwargs: client(transport=transport, **kwargs))
        with pytest.raises(BookError) as caught:
            with http.stream("https://trusted.example/book.epub"):
                pytest.fail("An error page must not be exposed as a book")
    assert caught.value.code == code
