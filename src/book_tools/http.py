"""Bounded HTTP with safe diagnostics and origin-scoped API redirect chains."""
from contextlib import contextmanager
from html import unescape
import ipaddress
import os
import re
import time
from urllib.parse import urljoin, urlsplit

import httpx

from .errors import BookError


def validate_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except (ValueError, TypeError):
        raise BookError("invalid_url", "Invalid service or download URL.") from None
    if not host or parts.username is not None or parts.password is not None or parts.fragment:
        raise BookError("invalid_url", "URLs must have a hostname and no embedded credentials or fragment.")
    if parts.scheme == "https":
        return url
    loopback = host.lower() == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    if parts.scheme == "http" and loopback and os.environ.get("BOOK_TOOLS_ALLOW_HTTP") == "1":
        return url
    raise BookError("invalid_url", "HTTPS is required; local HTTP fixtures require BOOK_TOOLS_ALLOW_HTTP=1.")


def _origin(url: str) -> tuple[str, str, int]:
    parsed = httpx.URL(validate_url(url))
    return parsed.scheme, parsed.host, parsed.port or (443 if parsed.scheme == "https" else 80)


def _status(response: httpx.Response) -> None:
    status = response.status_code
    if 200 <= status < 300:
        return
    if status >= 400 and "text/html" in response.headers.get("content-type", "").lower():
        try:
            text = response.text[:8192]
        except httpx.ResponseNotRead:
            # Error streams are about to be rejected; inspect only a bounded
            # prefix rather than buffering an arbitrary HTML download.
            text = next(response.iter_bytes(chunk_size=8192), b"").decode("utf-8", errors="replace")
        title = re.search(r"<title(?:\s[^>]*)?>([^<]*)</title\s*>", text, re.I)
        title_text = " ".join(unescape(title.group(1)).casefold().split()) if title else ""
        if any(phrase in title_text for phrase in ("verifying your browser", "checking your browser", "just a moment", "security verification")):
            raise BookError("provider_challenge", f"Service returned HTTP {status} with browser verification; automatic retries cannot solve it.")
    if status in (401, 403):
        raise BookError("access_denied", "The service denied access; check authentication or browser-challenge requirements.")
    if status == 429:
        raise BookError("rate_limited", "The service rate limit was reached.", recoverable=True)
    if 300 <= status < 400:
        raise BookError("redirect_refused", "Only same-origin 307/308 API redirects are allowed; method-changing redirects are refused.")
    raise BookError("http_error", f"Service returned HTTP {status}.", recoverable=status >= 500)


class _DeadlineStream(httpx.SyncByteStream):
    def __init__(self, stream: httpx.SyncByteStream, deadline: float):
        self.stream = stream
        self.deadline = deadline

    def __iter__(self):
        for chunk in self.stream:
            if time.monotonic() > self.deadline:
                raise BookError("timeout", "Download exceeded its 300-second transfer deadline.", recoverable=True)
            yield chunk

    def close(self):
        self.stream.close()


class Http:
    def __init__(self, timeout: float = 30.0):
        self.client = httpx.Client(
            timeout=httpx.Timeout(timeout, connect=min(timeout, 10)),
            follow_redirects=False,
            headers={"User-Agent": "book-tools/0.1 (+agent-independent CLI)"},
        )
        self.timeout = timeout

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.client.close()

    @contextmanager
    def _safe_errors(self):
        try:
            yield
        except httpx.InvalidURL:
            raise BookError("invalid_url", "The service or download URL is malformed.") from None
        except httpx.TimeoutException:
            raise BookError("timeout", "The service request timed out.", recoverable=True) from None
        except httpx.RequestError:
            raise BookError("network_error", "The service request failed; check connectivity and the configured endpoint.", recoverable=True) from None

    def _request(self, method: str, url: str, *, _allow_redirect: bool = False, **kwargs) -> httpx.Response:
        validate_url(url)
        # Explicit credentials and server cookies live only for this request chain.
        cookies = kwargs.pop("cookies", None)
        self.client.cookies.clear()
        if cookies:
            self.client.cookies.update(cookies)
        try:
            with self._safe_errors():
                request = self.client.build_request(method, url, **kwargs)
                origin = _origin(str(request.url))
                for redirects in range(6):
                    response = self.client.send(request, follow_redirects=False)
                    if _allow_redirect and response.status_code in (301, 302, 303, 307, 308):
                        return response
                    if response.status_code not in (307, 308):
                        _status(response)
                        return response
                    location = response.headers.get("location")
                    if not location:
                        raise BookError("invalid_response", "API redirect has no destination.")
                    target = urljoin(str(request.url), location)
                    if _origin(target) != origin:
                        raise BookError("redirect_refused", "API redirect changed origin; credentials were not forwarded.")
                    if redirects == 5:
                        raise BookError("redirect_limit", "The API exceeded five redirects.")
                    try:
                        body = request.content
                    except httpx.RequestNotRead:
                        raise BookError("redirect_refused", "API redirect requires a replayable request body.") from None
                    headers = request.headers.copy()
                    # Rebuild Cookie from the chain's jar, including response Set-Cookie.
                    # Rebuild Host for the validated destination rather than copying it.
                    headers.pop("cookie", None)
                    headers.pop("host", None)
                    request = self.client.build_request(request.method, target, content=body, headers=headers)
        finally:
            self.client.cookies.clear()

    def json(self, method: str, url: str, **kwargs) -> dict:
        response = self._request(method, url, **kwargs)
        try:
            result = response.json()
        except ValueError:
            raise BookError("invalid_response", "The service returned invalid JSON, possibly a challenge page.") from None
        if not isinstance(result, dict):
            raise BookError("invalid_response", "The service JSON response must be an object.")
        return result

    def text(self, url: str, **kwargs) -> str:
        # Anonymous HTML may use same-origin normalization redirects, not mirror discovery.
        if set(kwargs) - {"params"}:
            raise BookError("invalid_input", "Anonymous page requests accept only query parameters.")
        original = urlsplit(validate_url(url))
        origin = (original.scheme, original.hostname, original.port or (443 if original.scheme == "https" else 80))
        for _ in range(6):
            response = self._request("GET", url, _allow_redirect=True, **kwargs)
            if not 300 <= response.status_code < 400:
                return response.text
            location = response.headers.get("location")
            if not location:
                raise BookError("invalid_response", "Page redirect has no destination.")
            url = urljoin(str(response.url), location)
            redirected = urlsplit(validate_url(url))
            if (redirected.scheme, redirected.hostname, redirected.port or (443 if redirected.scheme == "https" else 80)) != origin:
                raise BookError("redirect_refused", "Anonymous page redirected to another origin; configure it explicitly if trusted.")
            kwargs = {}
        raise BookError("redirect_limit", "The page exceeded five redirects.")

    @contextmanager
    def stream(self, url: str):
        # A separate client never inherits API cookies or request credentials.
        deadline = time.monotonic() + 300
        with httpx.Client(timeout=self.timeout, follow_redirects=False, headers={
            "User-Agent": "book-tools/0.1", "Accept-Encoding": "identity",
        }) as client:
            with self._safe_errors():
                for _ in range(6):
                    validate_url(url)
                    with client.stream("GET", url) as response:
                        if time.monotonic() > deadline:
                            raise BookError("timeout", "Download exceeded its 300-second transfer deadline.", recoverable=True)
                        if response.status_code in (301, 302, 303, 307, 308):
                            location = response.headers.get("location")
                            if not location:
                                raise BookError("invalid_response", "Download redirect has no destination.")
                            url = urljoin(url, location)
                            client.cookies.clear()
                            continue
                        _status(response)
                        response.stream = _DeadlineStream(response.stream, deadline)
                        yield response
                        return
                raise BookError("redirect_limit", "The download exceeded five redirects.")
