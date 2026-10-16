"""Optional, visible, origin-bound browser transport; verification is human-operated.

The public transport is synchronous. Its owned asyncio loop drives Playwright's
async API so native downloads can be cancelled before private staging is removed.
``page`` is an async Playwright Page; ``screenshot`` is the synchronous helper.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import ipaddress
import math
import os
from pathlib import Path
import shutil
import stat
from urllib.parse import urlencode, urlsplit, urlunsplit

from .download import staged_download
from .errors import BookError
from .http import validate_url
from .models import DownloadTicket


_OWNER = "book-tools dedicated browser profile\n"
_MARKER = ".book-tools-profile"
_TOKEN_NAMES = ("remix_userid", "remix_userkey")


def _origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(validate_url(url))
    host = parts.hostname.lower()
    try:
        host = ipaddress.ip_address(host).compressed
    except ValueError:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise BookError("invalid_url", "The configured browser hostname is invalid.") from None
    port = parts.port if parts.port is not None else (443 if parts.scheme == "https" else 80)
    return parts.scheme, host, port


def _profile_error() -> BookError:
    return BookError("browser_profile", "The dedicated browser profile is unsafe or inaccessible.")


def _check_path(path: Path) -> None:
    # Reject a redirected profile or immediate container. Higher system aliases
    # (notably macOS /var -> /private/var) do not authorize an unmarked profile.
    for item in (path, path.parent):
        if item.is_symlink():
            raise _profile_error()


class ProfileLock:
    """Nonblocking OS-held sibling lock, shared by browser launch and logout.

    The lock file deliberately survives release. Unlinking it would allow another
    process to acquire a different inode while the original owner still holds it.
    """

    def __init__(self, profile_dir: Path):
        self.profile_dir = Path(profile_dir).absolute()
        self._fd: int | None = None
        if self.profile_dir == self.profile_dir.parent:
            raise _profile_error()
        self.path = self.profile_dir.with_name(self.profile_dir.name + ".lock")

    def __enter__(self):
        if self._fd is not None:
            raise BookError("browser_profile_busy", "The dedicated browser profile is already in use.")
        fd = None
        try:
            _check_path(self.profile_dir)
            self.profile_dir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _check_path(self.profile_dir)
            if os.name == "posix":
                self.profile_dir.parent.chmod(0o700)
            if self.path.is_symlink():
                raise _profile_error()
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise _profile_error()
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                raise _profile_error()
            if os.name == "nt":
                import msvcrt
                if info.st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                except OSError:
                    raise BookError("browser_profile_busy", "The dedicated browser profile is already in use.") from None
            else:
                import fcntl
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise BookError("browser_profile_busy", "The dedicated browser profile is already in use.") from None
                os.fchmod(fd, 0o600)
            self._fd = fd
            return self
        except BaseException as exc:
            if fd is not None:
                os.close(fd)
            if isinstance(exc, OSError):
                raise _profile_error() from None
            raise

    def __exit__(self, *exc):
        if self._fd is not None:
            # Closing releases both flock and Windows byte-range locks.
            os.close(self._fd)
            self._fd = None


def _owned_profile(profile: Path, *, create: bool) -> None:
    _check_path(profile)
    if not profile.exists():
        if not create:
            return
        profile.mkdir(mode=0o700)
    if not profile.is_dir():
        raise _profile_error()
    marker = profile / _MARKER
    if marker.is_symlink():
        raise _profile_error()
    if marker.exists():
        if not marker.is_file() or marker.stat().st_size != len(_OWNER) or marker.read_text() != _OWNER:
            raise _profile_error()
    elif create and not any(profile.iterdir()):
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as output:
            output.write(_OWNER)
    else:
        # In particular, never adopt or remove a user's ordinary Chrome profile.
        raise _profile_error()
    if os.name == "posix":
        profile.chmod(0o700)


def reset_profile(profile_dir: Path) -> None:
    """Remove only a marked dedicated profile, under the same launch lock."""
    with ProfileLock(profile_dir) as lock:
        try:
            _owned_profile(lock.profile_dir, create=False)
            if lock.profile_dir.exists():
                shutil.rmtree(lock.profile_dir)
        except (OSError, UnicodeError):
            raise _profile_error() from None


# No credential values or remote content are rendered in our human-control UI.
# Only the user clicks this button; it never interacts with verification controls.
_CONTINUE_SCRIPT = """({origin}) => {
    if (location.origin !== origin || document.getElementById('__book_tools_continue')) return false;
    const host = document.createElement('div');
    host.id = '__book_tools_continue';
    host.style.cssText = 'all:initial;position:fixed!important;top:16px!important;left:50%!important;transform:translateX(-50%)!important;width:min(760px,calc(100vw - 32px))!important;z-index:2147483647!important;pointer-events:none!important';
    const root = host.attachShadow({mode: 'open'});
    root.innerHTML = `
        <style>
            :host { color-scheme: light; }
            * { box-sizing: border-box; }
            .card { pointer-events:auto; background:#fff; color:#0f172a; border:2px solid #2563eb;
                border-radius:16px; padding:24px; box-shadow:0 14px 48px #0f172a40;
                font:18px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
            .heading { display:flex; justify-content:space-between; align-items:flex-start; gap:16px; }
            .brand { color:#1d4ed8; font-size:14px; font-weight:750; letter-spacing:.02em; }
            h2 { margin:4px 0 12px; font-size:26px; line-height:1.3; }
            p { margin:0 0 10px; }
            .site { color:#475569; font-size:15px; overflow-wrap:anywhere; }
            .note { color:#475569; font-size:15px; }
            button { font:inherit; cursor:pointer; }
            button:focus-visible { outline:3px solid #0f172a; outline-offset:3px; }
            .toggle { flex:none; min-height:44px; padding:8px 12px; background:#fff; color:#334155;
                border:1px solid #94a3b8; border-radius:8px; font-size:15px; }
            .continue { display:block; width:100%; min-height:64px; margin-top:16px; padding:12px 20px;
                border:0; border-radius:10px; background:#1d4ed8; color:#fff; font-size:21px; font-weight:700; }
            .continue:hover { background:#1e40af; }
            .english { font-size:15px; font-weight:500; margin-left:10px; white-space:nowrap; }
            [hidden] { display:none!important; }
            @media (max-width:520px) {
                .card { padding:16px; font-size:17px; }
                h2 { font-size:22px; }
                .heading { gap:8px; }
                .toggle { padding:8px; }
                .continue { font-size:19px; }
                .english { display:block; margin-left:0; }
            }
        </style>
        <section class="card" role="region" aria-label="book-tools 人工确认">
            <div class="heading">
                <div><div class="brand">BOOK-TOOLS · 等待你的确认</div><h2>请完成网站验证或登录</h2></div>
                <button class="toggle" type="button" aria-expanded="true" aria-controls="instructions">收起说明</button>
            </div>
            <div id="instructions" role="status" aria-live="polite">
                <p>先在当前网站完成验证码或登录。<strong>如果页面已正常显示，无需重复验证。</strong></p>
                <p>完成后点下方蓝色按钮，程序会自动继续搜索或下载。</p>
                <p class="site">当前站点：<span data-origin></span></p>
                <p class="note">本提示来自 book-tools，不是网站验证码。请先不要关闭窗口；挡住页面时可收起说明。</p>
            </div>
            <button class="continue" type="button">我已完成验证，继续<span class="english">Continue book-tools</span></button>
        </section>`;
    root.querySelector('[data-origin]').textContent = new URL(origin).host;
    const details = root.getElementById('instructions');
    const toggle = root.querySelector('.toggle');
    toggle.onclick = () => {
        details.hidden = !details.hidden;
        toggle.setAttribute('aria-expanded', String(!details.hidden));
        toggle.textContent = details.hidden ? '展开说明' : '收起说明';
    };
    root.querySelector('.continue').onclick = () => {
        window.__bookToolsContinue = true;
        host.remove();
    };
    document.documentElement.append(host);
    return true;
}"""

_FETCH_SCRIPT = """async ({url, method, body, timeout, origin}) => {
    if (location.origin !== origin) return {kind: 'origin'};
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
        const response = await fetch(url, {
            method, body, credentials: 'same-origin', mode: 'same-origin',
            redirect: 'error', signal: controller.signal,
            headers: body === null ? {'Accept': 'application/json'} :
                {'Accept': 'application/json', 'Content-Type': 'application/x-www-form-urlencoded'}
        });
        // Stream a bounded API response; a download must never travel through evaluate.
        const reader = response.body && response.body.getReader();
        const chunks = []; let size = 0;
        if (reader) {
            while (true) {
                const {done, value} = await reader.read();
                if (done) break;
                size += value.byteLength;
                if (size > 4 * 1024 * 1024) { await reader.cancel(); return {kind: 'large'}; }
                chunks.push(value);
            }
        }
        const bytes = new Uint8Array(size); let offset = 0;
        for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
        const text = new TextDecoder().decode(bytes);
        let payload; try { payload = JSON.parse(text); } catch (_) {}
        return {kind: 'response', status: response.status,
            html: (response.headers.get('content-type') || '').toLowerCase().includes('text/html') || /^\\s*</.test(text),
            object: payload !== null && typeof payload === 'object' && !Array.isArray(payload),
            payload: payload !== null && typeof payload === 'object' && !Array.isArray(payload) ? payload : null};
    } catch (error) {
        return {kind: error.name === 'AbortError' ? 'timeout' : 'blocked'};
    } finally { clearTimeout(timer); }
}"""


class BrowserTransport:
    def __init__(self, base_url: str, profile_dir: Path, *, session: dict | None = None,
                 timeout: float = 30, verification_timeout: float = 180, headless: bool = False):
        self._origin = _origin(base_url)
        scheme, host, port = self._origin
        authority = f"[{host}]" if ":" in host else host
        if port != (443 if scheme == "https" else 80):
            authority += f":{port}"
        self._origin_url = f"{scheme}://{authority}"
        for value in (timeout, verification_timeout):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise BookError("invalid_argument", "Browser timeouts must be positive finite seconds.")
        self.timeout = float(timeout)
        self.verification_timeout = float(verification_timeout)
        self.headless = headless
        self.profile_dir = Path(profile_dir)
        self._session = self._tokens(session or {})
        self._loop = None
        self._playwright = None
        self._context = None
        self._page = None
        self._lock = None

    @staticmethod
    def _tokens(values: dict) -> dict[str, str]:
        result = {}
        for name in _TOKEN_NAMES:
            value = values.get(name)
            if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
                result[name] = str(value).strip()
        return result if len(result) == 2 else {}

    @property
    def page(self):
        """The actual async Playwright Page (not an authentication/search status)."""
        if self._page is None:
            raise BookError("browser_closed", "The browser transport is not open.")
        return self._page

    def __enter__(self):
        if self._loop is not None:
            raise BookError("browser_profile_busy", "The browser transport is already open.")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise BookError("browser_unavailable", "Run browser mode in a synchronous CLI process, outside an active asyncio loop.")
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise BookError("browser_unavailable", "Install optional browser support with this project's --browser installer option, then run book-tools browser install.") from None
        try:
            self._lock = ProfileLock(self.profile_dir)
            self._lock.__enter__()
            try:
                _owned_profile(self._lock.profile_dir, create=True)
            except (OSError, UnicodeError):
                raise _profile_error() from None
            self._loop = asyncio.new_event_loop()
            self._call(self._open(async_playwright))
            return self
        except BaseException:
            self.close()
            raise

    async def _open(self, factory):
        try:
            self._playwright = await asyncio.wait_for(factory().start(), self.timeout)
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(self._lock.profile_dir), headless=self.headless, accept_downloads=True,
                timeout=self.timeout * 1000,
            )
            self._context.set_default_timeout(self.timeout * 1000)
            self._context.set_default_navigation_timeout(self.timeout * 1000)
            self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
            await self._context.route("**/*", self._guard_navigation)
            await self._page.goto(self._origin_url + "/", wait_until="domcontentloaded", timeout=self.timeout * 1000)
            self._check_origin()
            await self._import_cookies(self._session)
        except BookError:
            raise
        except Exception:
            raise BookError("browser_unavailable", "Chromium could not start or open the configured site. Run book-tools browser install; check browser/display availability and the configured endpoint.") from None

    async def _guard_navigation(self, route):
        request = route.request
        if request.is_navigation_request() and request.frame == self._page.main_frame:
            try:
                allowed = _origin(request.url) == self._origin
            except BookError:
                allowed = False
            if not allowed:
                await route.abort("blockedbyclient")
                return
        await route.continue_()

    def _check_origin(self):
        try:
            allowed = _origin(self.page.url) == self._origin
        except BookError:
            allowed = False
        if not allowed:
            raise BookError("redirect_refused", "The browser left the configured origin; no API credentials were sent.")

    async def _cookies(self) -> dict[str, str]:
        self._check_origin()
        cookies = await asyncio.wait_for(self._context.cookies(self._origin_url + "/eapi/user/profile"), self.timeout)
        return self._tokens({cookie["name"]: cookie["value"] for cookie in cookies if cookie["name"] in _TOKEN_NAMES})

    async def _import_cookies(self, supplied: dict):
        self._check_origin()
        values = {key: str(value) for key, value in supplied.items()
                  if isinstance(key, str) and key not in _TOKEN_NAMES
                  and isinstance(value, (str, int)) and not isinstance(value, bool)}
        # Browser identity always wins over a cached HTTP token pair.
        if not await self._cookies():
            values.update(self._tokens(supplied))
        if values:
            self._check_origin()
            await asyncio.wait_for(self._context.add_cookies([
                {"name": key, "value": value, "url": self._origin_url + "/", "httpOnly": True,
                 "secure": self._origin[0] == "https", "sameSite": "Lax"}
                for key, value in values.items()
            ]), self.timeout)

    def _call(self, coroutine):
        if self._loop is None:
            coroutine.close()
            raise BookError("browser_closed", "The browser transport is not open.")
        task = self._loop.create_task(coroutine)
        try:
            return self._loop.run_until_complete(task)
        except BaseException as exc:
            if not task.done():
                task.cancel()
                with suppress(BaseException):
                    self._loop.run_until_complete(task)
            self.close()
            if isinstance(exc, (BookError, KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(exc, (TimeoutError, asyncio.CancelledError)):
                raise BookError("timeout", "The browser operation timed out or was cancelled.", recoverable=True) from None
            raise BookError("browser_error", "The browser operation failed; check the visible browser and configured endpoint.", recoverable=True) from None

    async def _close_resources(self):
        if self._context is not None:
            with suppress(Exception):
                await asyncio.wait_for(self._context.close(), 10)
        if self._playwright is not None:
            with suppress(Exception):
                await asyncio.wait_for(self._playwright.stop(), 10)
        pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        for task in pending:
            task.cancel()
        if pending:
            with suppress(Exception):
                await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 5)

    def close(self):
        try:
            if self._loop is not None and not self._loop.is_closed():
                with suppress(BaseException):
                    self._loop.run_until_complete(self._close_resources())
                with suppress(BaseException):
                    self._loop.run_until_complete(self._loop.shutdown_asyncgens())
                self._loop.close()
        finally:
            self._loop = self._playwright = self._context = self._page = None
            if self._lock is not None:
                self._lock.__exit__(None, None, None)
                self._lock = None

    def __exit__(self, *exc):
        self.close()

    async def _show_continue(self):
        self._check_origin()
        inserted = await asyncio.wait_for(self.page.evaluate(_CONTINUE_SCRIPT, {"origin": self._origin_url}), self.timeout)
        if inserted:
            await asyncio.wait_for(self.page.bring_to_front(), self.timeout)

    async def _wait_human(self):
        if self.headless:
            raise BookError("provider_challenge", "Verification requires visible browser mode and a human-operated browser.")
        deadline = self._loop.time() + self.verification_timeout
        while self._loop.time() < deadline:
            self._check_origin()
            try:
                if await asyncio.wait_for(self.page.evaluate("() => window.__bookToolsContinue === true"), min(self.timeout, max(0.01, deadline - self._loop.time()))):
                    await asyncio.wait_for(self.page.evaluate("() => { window.__bookToolsContinue = false; }"), min(self.timeout, max(0.01, deadline - self._loop.time())))
                    return
                await asyncio.wait_for(self._show_continue(), max(0.01, deadline - self._loop.time()))
            except BookError:
                raise
            except Exception:
                # Navigation replaces the execution context. It is not proof of success.
                if self.page.is_closed():
                    raise BookError("provider_challenge", "The verification browser was closed before completion.") from None
            await asyncio.sleep(min(0.25, max(0, deadline - self._loop.time())))
        raise BookError("verification_timeout", "The human browser verification deadline expired; rerun browser mode when ready.")

    def session_tokens(self, *, wait: bool = True) -> dict:
        """Read cookies only. The provider must still validate authentication/schema."""
        return self._call(self._session_tokens(wait))

    async def _session_tokens(self, wait: bool):
        pair = await self._cookies()
        if pair or not wait:
            return pair
        if self.headless:
            raise BookError("credentials_missing", "Sign in manually using visible browser mode.")
        deadline = self._loop.time() + self.verification_timeout
        while self._loop.time() < deadline:
            try:
                await asyncio.wait_for(self._show_continue(), max(0.01, deadline - self._loop.time()))
            except BookError:
                raise
            except Exception:
                if self.page.is_closed():
                    raise BookError("credentials_missing", "The browser was closed before sign-in completed.") from None
            if self._loop.time() >= deadline:
                break
            try:
                pair = await asyncio.wait_for(self._cookies(), max(0.01, deadline - self._loop.time()))
            except TimeoutError:
                break
            if pair:
                return pair
            await asyncio.sleep(min(0.25, max(0, deadline - self._loop.time())))
        raise BookError("verification_timeout", "The manual browser sign-in deadline expired.")

    def json(self, method: str, url: str, **kwargs) -> dict:
        return self._call(self._json(method, url, kwargs))

    async def _fetch(self, method: str, url: str, body: str | None):
        from playwright.async_api import Error as PlaywrightError
        self._check_origin()
        try:
            return await asyncio.wait_for(self.page.evaluate(_FETCH_SCRIPT, {
                "url": url, "method": method, "body": body, "timeout": self.timeout * 1000,
                "origin": self._origin_url,
            }), self.timeout + 1)
        except PlaywrightError as error:
            if self.page.is_closed():
                raise BookError("provider_challenge", "The browser was closed before the API request completed.") from None
            # Verification scripts may navigate while fetch is running. Keep the
            # window available for explicit human continuation; never replay the
            # interrupted request silently or mistake page navigation for success.
            if "Execution context was destroyed" in str(error) or "Cannot find context with specified id" in str(error):
                self._check_origin()
                return {"kind": "blocked"}
            raise

    @staticmethod
    def _response(result: dict) -> dict | None:
        kind = result.get("kind")
        if kind == "origin":
            raise BookError("redirect_refused", "The browser left the configured origin; no API credentials were sent.")
        if kind == "timeout":
            raise BookError("timeout", "The browser API request timed out.", recoverable=True)
        if kind == "large":
            raise BookError("invalid_response", "The browser API response exceeded its 4 MiB limit.")
        if kind == "blocked":
            return None
        status = result.get("status", 0)
        if status == 429:
            raise BookError("rate_limited", "The service rate limit was reached.", recoverable=True)
        if result.get("html"):
            return None
        if status in (401, 403):
            raise BookError("access_denied", "The service denied the browser session.")
        if not 200 <= status < 300:
            raise BookError("http_error", "The browser API request returned an unsuccessful HTTP status.", recoverable=status >= 500)
        if not result.get("object") or not isinstance(result.get("payload"), dict):
            raise BookError("invalid_response", "The browser API response must be a JSON object.")
        return result["payload"]

    async def _json(self, method: str, url: str, kwargs: dict):
        if _origin(url) != self._origin:
            raise BookError("redirect_refused", "Browser API requests must use the configured origin.")
        self._check_origin()
        if set(kwargs) - {"data", "params", "cookies"}:
            raise BookError("invalid_argument", "Browser API requests accept only data, params and cookies.")
        method = method.upper()
        if method not in {"GET", "POST"}:
            raise BookError("invalid_argument", "Browser API requests support GET and POST only.")
        data = kwargs.get("data")
        if data is not None and not isinstance(data, dict):
            raise BookError("invalid_argument", "Browser API form data must be a mapping.")
        if data and any(str(key).lower() in {"password", "email"} for key in data):
            raise BookError("credentials_missing", "Sign in manually in the visible browser; browser mode does not forward passwords.")
        if method == "GET" and data:
            raise BookError("invalid_argument", "GET requests do not accept form bodies.")
        params = kwargs.get("params")
        if params:
            try:
                query = urlencode(params, doseq=True)
            except (TypeError, ValueError):
                raise BookError("invalid_argument", "Browser API query parameters are invalid.") from None
            parts = urlsplit(url)
            url = urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query + ("&" if parts.query else "") + query, ""))
        supplied = kwargs.get("cookies") or {}
        if not isinstance(supplied, dict):
            raise BookError("invalid_argument", "Browser API cookies must be a mapping.")
        await self._import_cookies(supplied)
        body = urlencode(data, doseq=True) if data is not None else None
        payload = self._response(await self._fetch(method, url, body))
        if payload is not None:
            return payload
        # A normal search surface exposes its verification UI; it proves nothing
        # about EAPI search availability. Never navigate to a credential-bearing API.
        path = urlsplit(url).path
        surface = self._origin_url + "/"
        if path.rstrip("/") == "/eapi/book/search" and data and data.get("message"):
            surface = self._origin_url + "/s/?" + urlencode({"q": data["message"]})
        try:
            await self.page.goto(surface, wait_until="domcontentloaded", timeout=self.timeout * 1000)
        except Exception:
            self._check_origin()
            # A loading/challenge page can still be used for manual verification.
        await self._wait_human()
        payload = self._response(await self._fetch(method, url, body))
        if payload is None:
            raise BookError("provider_challenge", "The API remains blocked after human verification; no further automatic retry was made.")
        return payload

    def screenshot(self, path: Path | str) -> None:
        self._call(self._screenshot(path))

    async def _screenshot(self, path):
        self._check_origin()
        await self.page.screenshot(path=str(path), timeout=self.timeout * 1000)

    def download(self, ticket: DownloadTicket, output_dir: Path, *, filename: str | None = None,
                 overwrite: bool = False) -> dict:
        return self._call(self._bounded_download(ticket, output_dir, filename, overwrite))

    async def _bounded_download(self, ticket, output_dir, filename, overwrite):
        try:
            return await asyncio.wait_for(self._download(ticket, output_dir, filename, overwrite), 300)
        except TimeoutError:
            raise BookError("timeout", "The native browser download exceeded its 300-second deadline.", recoverable=True) from None

    async def _download(self, ticket, output_dir, filename, overwrite):
        validate_url(ticket.url)
        self._check_origin()
        with staged_download(ticket, output_dir, filename=filename, overwrite=overwrite) as staged:
            transfer = None
            download_page = None
            isolated = None
            cross_origin = _origin(ticket.url) != self._origin
            try:
                context = self._context
                if cross_origin:
                    # A CDN gets no API cookies, including when it shares a hostname
                    # but uses another port (browser cookies are not port-scoped).
                    isolated = await self._context.browser.new_context(accept_downloads=True)
                    context = isolated
                download_page = await context.new_page()

                async def guard_download(route):
                    try:
                        validate_url(route.request.url)
                        if not cross_origin and _origin(route.request.url) != self._origin:
                            await route.abort("blockedbyclient")
                            return
                    except BookError:
                        await route.abort("blockedbyclient")
                        return
                    await route.continue_()

                await download_page.route("**/*", guard_download)
                if cross_origin:
                    await download_page.set_content("<html><body></body></html>")
                else:
                    # Same-origin download attributes must be attached to a real
                    # same-origin document, not an opaque about:blank document.
                    await download_page.goto(self._origin_url + "/", wait_until="domcontentloaded",
                                             timeout=self.timeout * 1000)
                    if _origin(download_page.url) != self._origin:
                        raise BookError("redirect_refused", "The download page left the configured origin.")
                await download_page.evaluate("""url => {
                    const a = document.createElement('a');
                    a.id = '__book_tools_download'; a.rel = 'noreferrer';
                    a.download = ''; a.textContent = 'Download book'; a.href = url;
                    document.documentElement.append(a);
                }""", ticket.url)
                async with download_page.expect_download(timeout=self.timeout * 1000) as event:
                    await download_page.locator("#__book_tools_download").click(timeout=self.timeout * 1000, no_wait_after=True)
                transfer = await event.value
                complete = False
                try:
                    await asyncio.wait_for(transfer.save_as(str(staged.path)), 300)
                    if await asyncio.wait_for(transfer.failure(), self.timeout):
                        raise BookError("download_failed", "The browser could not complete the native download.", recoverable=True)
                    receipt = staged.finish()
                    complete = True
                    return receipt
                finally:
                    if not complete:
                        try:
                            await asyncio.wait_for(transfer.cancel(), 5)
                        except Exception:
                            with suppress(Exception):
                                await asyncio.wait_for(context.close(), 5)
            finally:
                if download_page is not None:
                    with suppress(Exception):
                        await asyncio.wait_for(download_page.close(), 5)
                if isolated is not None:
                    with suppress(Exception):
                        await asyncio.wait_for(isolated.close(), 5)
