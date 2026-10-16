import json
import os
import stat

import pytest

from book_tools.config import Store
from book_tools.errors import BookError


@pytest.fixture
def store(tmp_path, monkeypatch):
    for key in ("ZLIB_EMAIL", "ZLIB_PASSWORD", "ANNAS_SECRET_KEY", "BOOK_TOOLS_ZLIB_URL", "ANNAS_BASE_URL", "BOOK_TOOLS_DOWNLOAD_DIR"):
        monkeypatch.delenv(key, raising=False)
    return Store(str(tmp_path / "home"))


def test_session_storage_never_duplicates_password_and_redacts_status(store, monkeypatch):
    monkeypatch.setenv("ZLIB_EMAIL", "private@example.invalid")
    monkeypatch.setenv("ZLIB_PASSWORD", "private-password-value")
    store.save_session("zlib", {"remix_userid": "123", "remix_userkey": "private-token", "password": "private-password-value"})
    saved = json.loads(store.sessions_file.read_text())
    assert set(saved["zlib"]) == {"endpoint", "remix_userid", "remix_userkey"}
    assert not store.config_file.exists()
    assert not store.secrets_file.exists()
    public = json.dumps(store.public())
    for secret in ("private-password-value", "private-token", "private@example.invalid"):
        assert secret not in public
    if os.name != "nt":
        assert stat.S_IMODE(store.sessions_file.stat().st_mode) == 0o600
        assert stat.S_IMODE(store.sessions_file.parent.stat().st_mode) == 0o700


def test_session_and_api_key_are_bound_to_endpoint(store):
    store.save_session("zlib", {"remix_userid": "123", "remix_userkey": "secret"})
    store.save_api_key("secret-key")
    assert store.session("zlib")["remix_userid"] == "123"
    assert store.credentials("annas")["api_key"] == "secret-key"
    store.set_value("zlib_url", "https://other.example")
    store.set_value("annas_url", "https://other.example")
    assert store.session("zlib") == {}
    assert store.credentials("annas") == {}


def test_explicit_env_file_and_process_precedence(store, monkeypatch, tmp_path):
    store.paths.config.mkdir()
    (store.paths.config / "credentials.env").write_text('ZLIB_EMAIL="from-file@example.invalid"\nZLIB_PASSWORD="from file"\n')
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ZLIB_PASSWORD=wrong-cwd-password\n")
    loaded = Store(str(store.paths.config))
    assert loaded.credentials("zlib")["password"] == "from file"
    monkeypatch.setenv("ZLIB_PASSWORD", "process-password")
    assert loaded.credentials("zlib")["password"] == "process-password"


def test_download_directory_is_effective_and_env_takes_precedence(store, tmp_path, monkeypatch):
    first = tmp_path / "configured"
    second = tmp_path / "override"
    store.set_value("download_dir", str(first))
    assert Store(str(store.paths.config)).download_dir == first
    monkeypatch.setenv("BOOK_TOOLS_DOWNLOAD_DIR", str(second))
    assert store.download_dir == second


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "301", "not-a-number"])
def test_invalid_timeout_is_rejected_without_saving(store, value):
    with pytest.raises(BookError):
        store.set_value("timeout", value)
    assert not store.config_file.exists()


def test_corrupt_config_is_an_actionable_error(store):
    store.paths.config.mkdir()
    store.config_file.write_text("not json")
    with pytest.raises(BookError) as caught:
        Store(str(store.paths.config))
    assert caught.value.code == "config_invalid"


def test_sensitive_file_replacement_restores_private_permissions(store):
    store.save_api_key("first-key")
    if os.name != "nt":
        store.secrets_file.chmod(0o644)
    store.save_api_key("replacement-key")
    assert store.credentials("annas")["api_key"] == "replacement-key"
    if os.name != "nt":
        assert stat.S_IMODE(store.secrets_file.stat().st_mode) == 0o600
