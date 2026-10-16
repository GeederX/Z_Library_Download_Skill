"""Browser safety checks that do not launch Chromium or contact a service."""
import asyncio
import json
import os
import stat
import subprocess
import sys

import pytest

from book_tools.browser import BrowserTransport, ProfileLock, _origin, _owned_profile, reset_profile
from book_tools.errors import BookError


def _child_lock(profile):
    return subprocess.run(
        [sys.executable, "-c", """
import sys
from pathlib import Path
from book_tools.browser import ProfileLock
from book_tools.errors import BookError
try:
    with ProfileLock(Path(sys.argv[1])):
        print('acquired')
except BookError as exc:
    print(exc.code)
""", str(profile)], capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_profile_lock_excludes_processes_and_preserves_inode(tmp_path):
    profile = tmp_path / "profiles" / "example"
    with ProfileLock(profile) as first:
        inode = first.path.stat().st_ino
        assert _child_lock(profile) == "browser_profile_busy"
        with pytest.raises(BookError) as error:
            reset_profile(profile)
        assert error.value.code == "browser_profile_busy"
    assert _child_lock(profile) == "acquired"
    assert first.path.stat().st_ino == inode


def test_profile_lock_released_after_process_exit(tmp_path):
    profile = tmp_path / "profile"
    subprocess.run([sys.executable, "-c", """
import os, sys
from pathlib import Path
from book_tools.browser import ProfileLock
lock = ProfileLock(Path(sys.argv[1]))
lock.__enter__()
os._exit(0)
""", str(profile)], check=True)
    with ProfileLock(profile):
        pass


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory permissions")
def test_profile_permissions_are_private(tmp_path):
    profile = tmp_path / "profiles" / "profile"
    with ProfileLock(profile) as lock:
        _owned_profile(lock.profile_dir, create=True)
        assert stat.S_IMODE(profile.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(profile.stat().st_mode) == 0o700
        assert stat.S_IMODE(lock.path.stat().st_mode) == 0o600


def test_reset_only_removes_owned_profile(tmp_path):
    profile = tmp_path / "profile"
    with ProfileLock(profile) as lock:
        _owned_profile(lock.profile_dir, create=True)
        (profile / "browser-data").write_text("private browser state")
    reset_profile(profile)
    assert not profile.exists()
    assert lock.path.exists()
    reset_profile(profile)  # An already-logged-out profile is harmless.
    assert not profile.exists()


def test_reset_refuses_unmarked_directory(tmp_path):
    profile = tmp_path / "ordinary-chrome"
    profile.mkdir()
    sentinel = profile / "Preferences"
    sentinel.write_text("keep")
    with pytest.raises(BookError) as error:
        reset_profile(profile)
    assert error.value.code == "browser_profile"
    assert sentinel.read_text() == "keep"


def test_launch_preparation_refuses_existing_daily_profile(tmp_path):
    profile = tmp_path / "ordinary-chrome"
    profile.mkdir()
    (profile / "Preferences").write_text("keep")
    with ProfileLock(profile) as lock:
        with pytest.raises(BookError) as error:
            _owned_profile(lock.profile_dir, create=True)
    assert error.value.code == "browser_profile"
    assert not (profile / ".book-tools-profile").exists()


def test_symlink_profile_and_lock_are_never_followed(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    profile = tmp_path / "profile"
    profile.symlink_to(real, target_is_directory=True)
    with pytest.raises(BookError):
        reset_profile(profile)
    assert real.is_dir()
    profile.unlink()
    lock_path = tmp_path / "profile.lock"
    target = tmp_path / "untouched"
    target.write_text("keep")
    lock_path.symlink_to(target)
    with pytest.raises(BookError):
        with ProfileLock(profile):
            pass
    assert target.read_text() == "keep"


def test_symlink_marker_cannot_authorize_reset(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    marker = tmp_path / "marker"
    marker.write_text("book-tools dedicated browser profile\n")
    (profile / ".book-tools-profile").symlink_to(marker)
    with pytest.raises(BookError):
        reset_profile(profile)
    assert profile.is_dir()
    assert marker.exists()


def test_effective_origin_and_browser_origin_serialization(tmp_path):
    assert _origin("https://EXAMPLE.test:443/path") == _origin("https://example.test/")
    assert _origin("https://example.test:444/") != _origin("https://example.test/")
    assert _origin("https://example.test:0/") != _origin("https://example.test/")
    assert _origin("https://bücher.test/") == _origin("https://xn--bcher-kva.test/")
    assert _origin("https://[0:0:0:0:0:0:0:1]/") == _origin("https://[::1]/")
    browser = BrowserTransport("https://EXAMPLE.test:443/base", tmp_path / "profile")
    assert browser._origin_url == "https://example.test"
    ipv6 = BrowserTransport("https://[::1]:444/", tmp_path / "ipv6")
    assert ipv6._origin_url == "https://[::1]:444"


@pytest.mark.parametrize("target", [
    "https://other.test/eapi/user/profile",
    "https://example.test:444/eapi/user/profile",
    "https://secret:password@example.test/eapi/user/profile",
    "https://example.test/eapi/user/profile#fragment",
])
def test_foreign_or_credential_urls_refused_without_browser(tmp_path, target):
    browser = BrowserTransport("https://example.test", tmp_path / "profile")
    with pytest.raises(BookError) as error:
        asyncio.run(browser._json("GET", target, {"cookies": {"remix_userkey": "secret"}}))
    assert error.value.code in {"redirect_refused", "invalid_url"}
    assert "secret" not in json.dumps(error.value.to_dict())
    assert not (tmp_path / "profile").exists()


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), True])
def test_nonfinite_or_nonpositive_timeouts_rejected(tmp_path, value):
    with pytest.raises(BookError) as error:
        BrowserTransport("https://example.test", tmp_path / "profile", verification_timeout=value)
    assert error.value.code == "invalid_argument"


def test_token_pair_is_complete_and_passwords_are_not_tokens():
    assert BrowserTransport._tokens({"email": "person", "password": "private"}) == {}
    assert BrowserTransport._tokens({"remix_userid": "1"}) == {}
    assert BrowserTransport._tokens({"remix_userid": True, "remix_userkey": "key"}) == {}
    assert BrowserTransport._tokens({"remix_userid": 17, "remix_userkey": " key "}) == {
        "remix_userid": "17", "remix_userkey": "key",
    }


def test_json_shape_not_profile_or_page_title_controls_api_success():
    assert BrowserTransport._response({"kind": "response", "status": 200, "html": True,
                                       "title": "Logged in", "payload": None}) is None
    with pytest.raises(BookError) as error:
        BrowserTransport._response({"kind": "response", "status": 200, "object": False, "payload": []})
    assert error.value.code == "invalid_response"
    # Transport acceptance is not provider-schema or search verification.
    assert BrowserTransport._response({"kind": "response", "status": 200, "object": True,
                                       "payload": {"user": {"id": "17"}}}) == {"user": {"id": "17"}}


@pytest.mark.parametrize(("response", "code"), [
    ({"kind": "timeout"}, "timeout"),
    ({"kind": "large"}, "invalid_response"),
    ({"kind": "origin"}, "redirect_refused"),
    ({"kind": "response", "status": 401}, "access_denied"),
    ({"kind": "response", "status": 429}, "rate_limited"),
    ({"kind": "response", "status": 503}, "http_error"),
])
def test_response_errors_are_safe_json(response, code):
    response["remote_error"] = "https://private.test/?token=NEVER-EXPOSE"
    with pytest.raises(BookError) as error:
        BrowserTransport._response(response)
    assert error.value.code == code
    encoded = json.dumps(error.value.to_dict())
    assert "NEVER-EXPOSE" not in encoded
    assert "private.test" not in encoded


def test_unexpected_failure_closes_loop_and_releases_lock(tmp_path):
    browser = BrowserTransport("https://example.test", tmp_path / "profile")
    browser._lock = ProfileLock(browser.profile_dir)
    browser._lock.__enter__()
    loop = browser._loop = asyncio.new_event_loop()

    async def fail():
        raise RuntimeError("https://private.test/?remix_userkey=secret")

    with pytest.raises(BookError) as error:
        browser._call(fail())
    assert error.value.code == "browser_error"
    assert "private.test" not in json.dumps(error.value.to_dict())
    assert loop.is_closed()
    assert browser._loop is None
    with ProfileLock(browser.profile_dir):
        pass


def test_active_event_loop_rejected_without_profile_access(tmp_path):
    browser = BrowserTransport("https://example.test", tmp_path / "profile")

    async def embedded():
        with pytest.raises(BookError) as error:
            browser.__enter__()
        assert error.value.code == "browser_unavailable"

    asyncio.run(embedded())
    assert not browser.profile_dir.exists()


def test_optional_package_missing_has_safe_install_guidance(tmp_path):
    result = subprocess.run([sys.executable, "-c", """
import importlib.abc, json, sys
from pathlib import Path
class NoPlaywright(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'playwright' or fullname.startswith('playwright.'):
            raise ModuleNotFoundError('playwright is intentionally unavailable')
sys.meta_path.insert(0, NoPlaywright())
from book_tools.browser import BrowserTransport
from book_tools.errors import BookError
try:
    with BrowserTransport('https://example.test', Path(sys.argv[1])):
        raise AssertionError('must not launch')
except BookError as exc:
    print(json.dumps(exc.to_dict()))
""", str(tmp_path / "profile")], capture_output=True, text=True, check=True)
    error = json.loads(result.stdout)
    assert error["code"] == "browser_unavailable"
    assert not (tmp_path / "profile").exists()
