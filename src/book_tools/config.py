"""User-scoped paths, explicit precedence, and separate private secret stores."""
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

from dotenv import dotenv_values
from platformdirs import user_cache_path, user_config_path, user_state_path

from .errors import BookError
from .http import validate_url

DEFAULT_ENDPOINTS = {"zlib": "https://1lib.sk", "annas": "https://annas-archive.gl"}
CONFIG_KEYS = {"zlib_url", "annas_url", "download_dir", "timeout"}


@dataclass(frozen=True)
class Paths:
    config: Path
    state: Path
    cache: Path

    @classmethod
    def discover(cls, home: str | None = None):
        override = home or os.environ.get("BOOK_TOOLS_HOME")
        if override:
            root = Path(override).expanduser().resolve()
            return cls(root, root / "state", root / "cache")
        return cls(user_config_path("book-tools", appauthor=False),
                   user_state_path("book-tools", appauthor=False),
                   user_cache_path("book-tools", appauthor=False))


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise BookError("config_invalid", "A book-tools configuration file is unreadable or invalid JSON.") from None
    if not isinstance(data, dict):
        raise BookError("config_invalid", "Configuration must be a JSON object.")
    return data


def private_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        path.parent.chmod(0o700)
    fd, name = tempfile.mkstemp(prefix=".book-tools-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def endpoint(value: str) -> str:
    if not isinstance(value, str):
        raise BookError("config_invalid", "Service endpoint must be a URL string.")
    value = value.rstrip("/")
    validate_url(value)
    parts = urlsplit(value)
    if parts.query or parts.path not in ("", "/"):
        raise BookError("config_invalid", "Service endpoint must be an origin without a path or query.")
    return value


class Store:
    def __init__(self, home: str | None = None):
        self.paths = Paths.discover(home)
        self.config_file = self.paths.config / "config.json"
        self.secrets_file = self.paths.config / "credentials.json"
        self.sessions_file = self.paths.state / "sessions.json"
        self.values = read_json(self.config_file)
        unknown = set(self.values) - CONFIG_KEYS
        if unknown:
            raise BookError("config_invalid", "Configuration contains unsupported keys; use config set.")
        self._env = {key: value for key, value in dotenv_values(self.paths.config / "credentials.env", interpolate=False).items() if value is not None}

    def env(self, name: str) -> str | None:
        return os.environ.get(name, self._env.get(name))

    def base_url(self, source: str) -> str:
        variable = "BOOK_TOOLS_ZLIB_URL" if source == "zlib" else "ANNAS_BASE_URL"
        return endpoint(self.env(variable) or self.values.get(f"{source}_url") or DEFAULT_ENDPOINTS[source])

    @property
    def timeout(self) -> float:
        try:
            value = float(self.values.get("timeout", 30))
        except (TypeError, ValueError):
            raise BookError("config_invalid", "Timeout must be a positive finite number.") from None
        if not math.isfinite(value) or value <= 0 or value > 300:
            raise BookError("config_invalid", "Timeout must be between zero and 300 seconds.")
        return value

    @property
    def download_dir(self) -> Path:
        value = self.env("BOOK_TOOLS_DOWNLOAD_DIR") or self.values.get("download_dir") or str(Path.home() / "Downloads")
        if not isinstance(value, str) or not value.strip():
            raise BookError("config_invalid", "Download directory must be a nonempty path.")
        return Path(value).expanduser().resolve()

    def credentials(self, source: str) -> dict:
        if source == "zlib":
            return {key: self.env(variable) for key, variable in (("email", "ZLIB_EMAIL"), ("password", "ZLIB_PASSWORD")) if self.env(variable)}
        stored = read_json(self.secrets_file).get("annas", {})
        if not isinstance(stored, dict):
            raise BookError("config_invalid", "Anna credentials must be an object.")
        key = self.env("ANNAS_SECRET_KEY")
        if not key and stored.get("endpoint") == self.base_url("annas"):
            key = stored.get("api_key")
        return {"api_key": key} if key else {}

    def session(self, source: str) -> dict:
        saved = read_json(self.sessions_file).get(source, {})
        if not isinstance(saved, dict):
            raise BookError("config_invalid", "Session entry must be an object.")
        if saved.get("endpoint") != self.base_url(source):
            return {}
        return {key: saved[key] for key in ("remix_userid", "remix_userkey") if saved.get(key)}

    @property
    def browser_profile_dir(self) -> Path:
        digest = hashlib.sha256(self.base_url("zlib").encode("utf-8")).hexdigest()
        return self.paths.state / "browser" / digest / "profile"

    def save_session(self, source: str, tokens: dict) -> None:
        allowed = {key: str(tokens[key]) for key in ("remix_userid", "remix_userkey") if tokens.get(key)}
        if source != "zlib" or len(allowed) != 2:
            raise BookError("invalid_response", "Authentication did not return a complete session.")
        data = read_json(self.sessions_file)
        data[source] = {"endpoint": self.base_url(source), **allowed}
        private_write(self.sessions_file, data)

    def save_api_key(self, key: str) -> None:
        if not key.strip():
            raise BookError("invalid_input", "API key cannot be empty.")
        data = read_json(self.secrets_file)
        data["annas"] = {"endpoint": self.base_url("annas"), "api_key": key.strip()}
        private_write(self.secrets_file, data)

    def logout(self, source: str) -> None:
        path = self.sessions_file if source == "zlib" else self.secrets_file
        data = read_json(path)
        data.pop(source, None)
        if path.exists():
            private_write(path, data)

    def set_value(self, key: str, value: str) -> None:
        if key not in CONFIG_KEYS:
            raise BookError("invalid_input", "Unsupported configuration key.")
        if key.endswith("_url"):
            parsed = endpoint(value)
        elif key == "timeout":
            try:
                parsed = float(value)
            except ValueError:
                raise BookError("invalid_input", "Timeout must be numeric.") from None
            if not math.isfinite(parsed) or not 0 < parsed <= 300:
                raise BookError("invalid_input", "Timeout must be between zero and 300 seconds.")
        else:
            if not value.strip():
                raise BookError("invalid_input", "Download directory cannot be empty.")
            parsed = str(Path(value).expanduser().resolve())
        self.values[key] = parsed
        private_write(self.config_file, self.values)

    def public(self) -> dict:
        return {
            "paths": {"config": str(self.paths.config), "state": str(self.paths.state), "cache": str(self.paths.cache)},
            "zlib_url": self.base_url("zlib"), "annas_url": self.base_url("annas"),
            "timeout": self.timeout, "download_dir": str(self.download_dir),
            "browser": {
                "profile": str(self.browser_profile_dir),
                "profile_present": self.browser_profile_dir.is_dir(),
            },
            "credentials": {
                "zlib_password_present": bool(self.credentials("zlib").get("password")),
                "zlib_session_present": bool(self.session("zlib")),
                "annas_key_present": bool(self.credentials("annas")),
            },
        }
