import httpx
import pytest

from book_tools.config import Store, private_write
from book_tools.errors import BookError
from book_tools.http import Http
from book_tools.models import SearchQuery
from book_tools.session import ZlibSession


@pytest.fixture
def store(tmp_path, monkeypatch):
    for key in ("ZLIB_EMAIL", "ZLIB_PASSWORD", "BOOK_TOOLS_ZLIB_URL"):
        monkeypatch.delenv(key, raising=False)
    store = Store(str(tmp_path / "settings"))
    store.set_value("zlib_url", "https://trusted.example")
    store.save_session("zlib", {"remix_userid": "123", "remix_userkey": "private-session"})
    return store


@pytest.mark.parametrize("mode,code", [("auto", "browser_required"), ("http", "provider_challenge")])
def test_noninteractive_challenge_preserves_credentials_without_opening_browser(store, monkeypatch, mode, code):
    monkeypatch.setattr(ZlibSession, "_browser_provider", lambda self: pytest.fail("Unexpected browser launch"))
    def handler(request):
        if request.url.path == "/eapi/user/profile":
            return httpx.Response(200, json={"success": True, "user": {"id": 123, "remix_userkey": "private-session"}})
        return httpx.Response(513, text="<title>Verifying your browser | DiamWall</title>", headers={"content-type": "text/html"})
    with Http() as http:
        http.client.close()
        http.client = httpx.Client(transport=httpx.MockTransport(handler))
        with ZlibSession(store, http, mode=mode, interactive=False) as session:
            with pytest.raises(BookError) as caught:
                session.run(lambda provider, browser: provider.search(SearchQuery("book")))
    assert caught.value.code == code
    assert store.session("zlib")["remix_userkey"] == "private-session"


@pytest.mark.parametrize("interactive", [False, True])
def test_auto_uses_http_even_with_legacy_browser_preference(store, monkeypatch, interactive):
    private_write(store.paths.state / "browser-preference.json", {
        "zlib": {"endpoint": store.base_url("zlib"), "transport": "browser"},
    })
    monkeypatch.setattr(ZlibSession, "_browser_provider", lambda self: pytest.fail("Unexpected browser launch"))
    def handler(request):
        return httpx.Response(200, json={"success": True, "user": {
            "id": 123, "remix_userkey": "private-session", "downloads_limit": 10, "downloads_today": 4,
        }})
    with Http() as http:
        http.client.close()
        http.client = httpx.Client(transport=httpx.MockTransport(handler))
        with ZlibSession(store, http, interactive=interactive) as session:
            result = session.run(lambda provider, browser: provider.quota())
            assert session.transport == "http"
    assert result == {"downloads_limit": 10, "downloads_today": 4, "downloads_left": 6}


@pytest.mark.parametrize("status,code", [(403, "access_denied"), (429, "rate_limited"), (503, "http_error"), (0, "network_error")])
def test_auto_does_not_open_browser_for_non_challenge_errors(store, monkeypatch, status, code):
    monkeypatch.setattr(ZlibSession, "_browser_provider", lambda self: pytest.fail("Unexpected browser launch"))
    def handler(request):
        if request.url.path == "/eapi/user/profile":
            return httpx.Response(200, json={"success": True, "user": {"id": 123, "remix_userkey": "private-session"}})
        if not status:
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(status, text="Service unavailable", headers={"content-type": "text/html"})
    with Http() as http:
        http.client.close()
        http.client = httpx.Client(transport=httpx.MockTransport(handler))
        with ZlibSession(store, http, interactive=True) as session:
            with pytest.raises(BookError) as caught:
                session.run(lambda provider, browser: provider.search(SearchQuery("book")))
    assert caught.value.code == code


def test_browser_profile_and_session_are_endpoint_bound(store):
    first_profile = store.browser_profile_dir
    store.set_value("zlib_url", "https://different.example")
    assert store.browser_profile_dir != first_profile
    assert store.session("zlib") == {}


def test_logout_clears_only_local_zlib_tokens(store):
    store.save_api_key("other-source-key")
    store.logout("zlib")
    assert store.session("zlib") == {}
    assert store.credentials("annas")["api_key"] == "other-source-key"
