import json
import os
from pathlib import Path
import subprocess
import sys


def run_cli(tmp_path, *args):
    env = os.environ.copy()
    env["BOOK_TOOLS_HOME"] = str(tmp_path / "config")
    for key in ("ZLIB_EMAIL", "ZLIB_PASSWORD", "ANNAS_SECRET_KEY", "BOOK_TOOLS_ZLIB_URL", "ANNAS_BASE_URL", "BOOK_TOOLS_DOWNLOAD_DIR"):
        env.pop(key, None)
    return subprocess.run([sys.executable, "-m", "book_tools.cli", *args], cwd=tmp_path, env=env, capture_output=True, text=True)


def test_invalid_arguments_are_machine_readable_without_echoing_secrets(tmp_path):
    result = run_cli(tmp_path, "--password=secret-on-command-line")
    assert result.returncode == 1
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_arguments"
    assert "secret-on-command-line" not in result.stderr


def test_unconfigured_zlib_has_actionable_error_not_traceback(tmp_path):
    result = run_cli(tmp_path, "search", "public domain title", "--source", "zlib")
    assert result.returncode == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["code"] == "credentials_missing"


def test_skill_install_preserves_existing_skill(tmp_path):
    skills = tmp_path / "skills"
    destination = skills / "zlib-download"
    destination.mkdir(parents=True)
    marker = destination / "SKILL.md"
    marker.write_text("existing user skill")
    result = run_cli(tmp_path, "skill", "install", "--directory", str(skills))
    assert result.returncode == 1
    assert json.loads(result.stderr)["error"]["code"] == "skill_exists"
    assert marker.read_text() == "existing user skill"


def test_auth_login_noninteractive_does_not_accept_piped_credentials(tmp_path):
    result = run_cli(tmp_path, "auth", "login", "--source", "zlib")
    assert result.returncode == 1
    assert json.loads(result.stderr)["error"]["code"] == "interactive_required"
    assert not (tmp_path / "config" / "state" / "sessions.json").exists()


def test_configured_destination_survives_another_process_and_cwd(tmp_path):
    destination = tmp_path / "books with spaces"
    saved = run_cli(tmp_path, "config", "set", "download_dir", str(destination))
    assert saved.returncode == 0
    fetched = run_cli(tmp_path, "config", "show")
    assert fetched.returncode == 0
    assert Path(json.loads(fetched.stdout)["download_dir"]) == destination


def test_browser_transport_rejects_anna_instead_of_silently_using_http(tmp_path):
    result = run_cli(tmp_path, "search", "book", "--source", "annas", "--transport", "browser")
    assert result.returncode == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["code"] == "invalid_input"


def test_explicit_browser_source_auto_preserves_failure_instead_of_trying_unsupported_anna(tmp_path, monkeypatch):
    from contextlib import contextmanager
    import pytest
    from book_tools import cli
    from book_tools.errors import BookError
    monkeypatch.setenv("BOOK_TOOLS_HOME", str(tmp_path / "config"))
    class FailedSession:
        def run(self, operation):
            raise BookError("timeout", "Browser API timed out.", recoverable=True)
    @contextmanager
    def failed_session(*args, **kwargs):
        yield FailedSession()
    monkeypatch.setattr(cli, "_zlib_session", failed_session)
    args = cli.parser().parse_args(["search", "book", "--source", "auto", "--transport", "browser"])
    with pytest.raises(BookError) as caught:
        cli.execute(args)
    assert caught.value.code == "timeout"
    assert caught.value.recoverable is True


def test_old_browser_preference_does_not_force_browser_login(tmp_path):
    from book_tools.config import private_write
    private_write(tmp_path / "config" / "state" / "browser-preference.json", {
        "zlib": {"endpoint": "https://1lib.sk", "transport": "browser"},
    })
    result = run_cli(tmp_path, "auth", "login", "--source", "zlib")
    assert result.returncode == 1
    assert json.loads(result.stderr)["error"]["code"] == "interactive_required"
