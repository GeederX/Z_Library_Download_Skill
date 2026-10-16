"""Keep one transport alive through authentication, API operations and download."""
from contextlib import ExitStack
from typing import Callable, TypeVar

from .config import Store
from .errors import BookError
from .http import Http
from .providers.zlibrary import ZLibrary

T = TypeVar("T")


class ZlibSession:
    def __init__(self, store: Store, http: Http, *, mode: str = "auto", verification_timeout: float = 180, interactive: bool = False):
        self.store = store
        self.http = http
        self.mode = mode
        self.verification_timeout = verification_timeout
        self.interactive = interactive
        self.transport = "http"
        self._resources = ExitStack()
        self._browser = None
        if mode not in {"auto", "http", "browser"}:
            raise BookError("invalid_input", "Unknown API transport.")

    def __enter__(self):
        self._resources.__enter__()
        return self

    def __exit__(self, *exc):
        return self._resources.__exit__(*exc)

    def _http_provider(self) -> ZLibrary:
        provider = ZLibrary(self.http, self.store.base_url("zlib"), credentials=self.store.credentials("zlib"), session=self.store.session("zlib"))
        tokens = provider.authenticate()
        if tokens != self.store.session("zlib"):
            self.store.save_session("zlib", tokens)
        return provider

    def _browser_provider(self) -> ZLibrary:
        from .browser import BrowserTransport
        if self._browser is None:
            self._browser = self._resources.enter_context(BrowserTransport(
                self.store.base_url("zlib"), self.store.browser_profile_dir,
                session=self.store.session("zlib"), timeout=self.store.timeout,
                verification_timeout=self.verification_timeout,
            ))
        # Normal website login is performed by the user; passwords are never forwarded.
        tokens = self._browser.session_tokens(wait=True)
        provider = ZLibrary(self._browser, self.store.base_url("zlib"), session=tokens)
        verified = provider.authenticate()
        if verified != self.store.session("zlib"):
            self.store.save_session("zlib", verified)
        return provider

    @staticmethod
    def _require_explicit_browser() -> None:
        raise BookError("browser_required", "Browser verification is required. Run the same command with --transport browser to explicitly open the visible browser; credentials are not cleared.")

    def run(self, operation: Callable[..., T]) -> T:
        if self.mode != "browser":
            try:
                provider = self._http_provider()
                result = operation(provider, None)
            except BookError as error:
                if self.mode != "auto" or error.code != "provider_challenge":
                    raise
                if not self.interactive:
                    self._require_explicit_browser()
            else:
                self.transport = "http"
                return result
        provider = self._browser_provider()
        self.transport = "browser"
        return operation(provider, self._browser)
