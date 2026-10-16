"""CLI boundary: one JSON result, safe errors, no agent-specific business logic."""
import argparse
from dataclasses import asdict
import getpass
import importlib.util
from importlib.resources import files, as_file
import json
import math
import os
from pathlib import Path
import shutil
import sys
import subprocess
import tempfile

from . import __version__
from .config import Store
from .download import download
from .errors import BookError
from .http import Http
from .models import SearchQuery
from .providers.annas import Annas
from .providers.zlibrary import ZLibrary
from .session import ZlibSession


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's message may contain an unrecognized secret-bearing argument.
        raise BookError("invalid_arguments", "Invalid command arguments; use book-tools --help or COMMAND --help.")


def _positive(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _verification_seconds(value):
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive finite number") from None
    if not math.isfinite(number) or not 0 < number <= 600:
        raise argparse.ArgumentTypeError("must be between zero and 600 seconds")
    return number


def _transport_options(command):
    command.add_argument("--transport", choices=("auto", "http", "browser"), default="auto",
                         help="Z-Library transport; auto tries HTTP for each command, browser explicitly opens a visible browser")
    command.add_argument("--verification-timeout", type=_verification_seconds, default=180,
                         help="Seconds allowed for human browser verification/login (maximum 600)")


def parser() -> Parser:
    root = Parser(prog="book-tools", description="Agent-independent search and validated book downloads. Results are JSON.")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--home", help="Override all book-tools config/state/cache paths")
    commands = root.add_subparsers(dest="command", required=True)
    p = commands.add_parser("search", help="Search metadata; Anna search does not need an API key")
    p.add_argument("query")
    p.add_argument("--source", choices=("auto", "zlib", "annas"), default="auto")
    p.add_argument("--lang")
    p.add_argument("--ext")
    p.add_argument("--year-from", type=int)
    p.add_argument("--year-to", type=int)
    p.add_argument("--limit", type=_positive, default=10)
    p.add_argument("--page", type=_positive, default=1)
    _transport_options(p)
    for command in ("info", "download"):
        p = commands.add_parser(command, help="Book details" if command == "info" else "Download, validate, and commit a file")
        p.add_argument("--source", choices=("zlib", "annas"), required=True)
        p.add_argument("--id", default="")
        p.add_argument("--hash", required=True)
        _transport_options(p)
        if command == "download":
            p.add_argument("--output", "-o", help="Destination directory")
            p.add_argument("--filename", help="Single output filename, not a path")
            p.add_argument("--overwrite", action="store_true", help="Explicitly permit replacing an existing file")
            p.add_argument("--expect-format", help="Reject a ticket whose format differs from this value")
    p = commands.add_parser("auth", help="Local login, status, and logout; passwords remain hidden")
    p.add_argument("action", choices=("login", "status", "logout"))
    p.add_argument("--source", choices=("zlib", "annas"), required=True)
    _transport_options(p)
    p = commands.add_parser("quota", help="Read authenticated Z-Library download allowance")
    _transport_options(p)
    p = commands.add_parser("doctor", help="Local diagnostics; networking is opt-in")
    p.add_argument("--online", action="store_true", help="Explicitly check Z-Library session and Anna reachability")
    _transport_options(p)
    p = commands.add_parser("config", help="Show non-sensitive configuration or set a non-secret value")
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("show")
    s = sub.add_parser("set")
    s.add_argument("key", choices=("zlib_url", "annas_url", "download_dir", "timeout"))
    s.add_argument("value")
    p = commands.add_parser("browser", help="Explicit Chromium installation or local browser status")
    p.add_argument("action", choices=("install", "status"))
    p = commands.add_parser("skill", help="Install the bundled generic Agent Skill; does not install the runtime")
    sub = p.add_subparsers(dest="action", required=True)
    s = sub.add_parser("install")
    s.add_argument("--scope", choices=("global", "project"), default="project")
    s.add_argument("--directory", help="Explicit agent skills directory; skill is copied into its zlib-download child")
    return root


def _zlib_session(args, store: Store, http: Http, *, mode: str | None = None):
    return ZlibSession(store, http, mode=mode or args.transport,
                       verification_timeout=args.verification_timeout,
                       interactive=sys.stdin.isatty())


def _anna(store: Store, http: Http):
    return Annas(http, store.base_url("annas"), credentials=store.credentials("annas"))


def _search(args, store: Store, http: Http) -> dict:
    if not args.query.strip():
        raise BookError("invalid_input", "Search query cannot be empty.")
    if args.year_from is not None and args.year_to is not None and args.year_from > args.year_to:
        raise BookError("invalid_input", "Start year cannot exceed end year.")
    query = SearchQuery(args.query, args.lang, args.ext, args.year_from, args.year_to, args.limit, args.page)
    sources = [args.source] if args.source != "auto" else ["zlib", "annas"]
    if args.transport == "browser" and args.source == "auto":
        sources = ["zlib"]
    attempts = []
    for source in sources:
        try:
            if source == "zlib":
                with _zlib_session(args, store, http) as session:
                    books = session.run(lambda provider, browser: provider.search(query))
                    transport = session.transport
            else:
                if args.transport == "browser":
                    raise BookError("invalid_input", "Browser transport is supported only for Z-Library.")
                books = _anna(store, http).search(query)
                transport = "http"
            return {"source": source, "transport": transport, "count": len(books), "books": [asdict(book) for book in books], "attempts": attempts}
        except BookError as error:
            if args.source != "auto" or args.transport == "browser":
                raise
            attempts.append({"source": source, **error.to_dict()})
            if not error.recoverable and error.code != "credentials_missing":
                raise
    raise BookError("sources_unavailable", "No search source was usable. " + "; ".join(f"{item['source']}: {item['code']}" for item in attempts), recoverable=any(item["recoverable"] for item in attempts))


def _auth(args, store: Store, http: Http) -> dict:
    if args.action == "status":
        if args.source == "zlib":
            configured = bool(store.session("zlib")) or all(store.credentials("zlib").get(key) for key in ("email", "password"))
        else:
            configured = bool(store.credentials("annas"))
        result = {"source": args.source, "configured": configured, "checked_online": False}
        if args.source == "zlib":
            result["browser_profile_present"] = store.browser_profile_dir.is_dir()
        return result
    if args.action == "logout":
        if args.source == "zlib":
            from .browser import reset_profile
            reset_profile(store.browser_profile_dir)
        store.logout(args.source)
        return {"source": args.source, "stored_credentials_removed": True, "note": "Local browser profile is removed for Z-Library. Environment variables and credentials.env are not modified; remote sessions are not revoked."}
    if args.source == "annas" and args.transport == "browser":
        raise BookError("invalid_input", "Browser transport is supported only for Z-Library.")
    if args.source == "zlib" and args.transport == "browser":
        with _zlib_session(args, store, http) as session:
            session.run(lambda provider, browser: None)
        return {"source": "zlib", "authenticated": True, "password_stored": False, "transport": "browser"}
    if not sys.stdin.isatty():
        raise BookError("interactive_required", "Run auth login in your own interactive terminal; do not pass credentials in command arguments or chat.")
    if args.source == "annas":
        key = getpass.getpass("Anna API key (hidden): ")
        store.save_api_key(key)
        return {"source": "annas", "stored": True, "checked_online": False}
    email = input("Z-Library email: ").strip()
    password = getpass.getpass("Z-Library password (hidden): ")
    provider = ZLibrary(http, store.base_url("zlib"))
    try:
        tokens = provider.login(email, password)
    except BookError as error:
        if args.transport != "auto" or error.code != "provider_challenge":
            raise
        with _zlib_session(args, store, http, mode="browser") as session:
            session.run(lambda provider, browser: None)
        return {"source": "zlib", "authenticated": True, "password_stored": False, "transport": "browser"}
    store.save_session("zlib", tokens)
    return {"source": "zlib", "authenticated": True, "password_stored": False, "transport": "http"}


def _doctor(args, store: Store, http: Http) -> dict:
    result = {"version": __version__, "python": sys.version.split()[0], "config": store.public(), "checked_online": args.online}
    if args.online:
        checks = {"zlib": {"authenticated": False, "search": False, "download_checked": False}}
        def check_zlib(provider, browser):
            checks["zlib"]["authenticated"] = True
            checks["zlib"]["quota"] = provider.quota()
            provider.search(SearchQuery("Pride and Prejudice", limit=1))
            checks["zlib"]["search"] = True
        try:
            with _zlib_session(args, store, http) as session:
                session.run(check_zlib)
                checks["zlib"]["transport"] = session.transport
        except BookError as error:
            checks["zlib"]["error"] = error.to_dict()
        try:
            http.text(store.base_url("annas"))
            checks["annas"] = {"reachable": True}
        except BookError as error:
            checks["annas"] = {"reachable": False, "error": error.to_dict()}
        result["services"] = checks
    return result


def install_skill(args) -> dict:
    if args.directory:
        parent = Path(args.directory).expanduser().resolve()
    else:
        parent = (Path.home() if args.scope == "global" else Path.cwd()) / ".agents" / "skills"
    destination = parent / "zlibrary-download"
    if destination.exists() or destination.is_symlink():
        raise BookError("skill_exists", "A zlibrary-download skill already exists at the destination; choose another skills directory. Existing skills are not overwritten.")
    parent.mkdir(parents=True, exist_ok=True)
    resource = files("book_tools").joinpath("skill")
    # A source checkout and an installed Skill share the same self-contained root.
    checkout = Path(__file__).resolve().parents[2]
    source = checkout if (checkout / "SKILL.md").is_file() else resource
    staging = Path(tempfile.mkdtemp(prefix=".book-tools-skill-", dir=parent))
    try:
        with as_file(source) as source_path:
            shutil.copytree(source_path, staging / "zlibrary-download", ignore=shutil.ignore_patterns(
                ".venv", "__pycache__", "*.py[cod]", "*.egg-info", ".pytest_cache",
                ".git", "build", "dist", ".env", "credentials.env", "credentials.json", "sessions.json",
            ))
        # rename does not replace a populated directory, but check again to avoid replacing empty ones.
        if destination.exists() or destination.is_symlink():
            raise BookError("skill_exists", "A skill appeared at the destination during installation.")
        (staging / "zlibrary-download").rename(destination)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {"path": str(destination), "runtime": "book-tools", "note": "Reload your agent if required. Agent discovery rules vary; --directory selects an agent-specific skills directory."}


def _browser_status(store: Store) -> dict:
    available = importlib.util.find_spec("playwright") is not None
    executable_present = False
    if available:
        from playwright.sync_api import sync_playwright, Error
        try:
            with sync_playwright() as playwright:
                executable_present = Path(playwright.chromium.executable_path).is_file()
        except (Error, OSError):
            pass
    return {"playwright_installed": available, "chromium_installed": executable_present,
            "profile": str(store.browser_profile_dir), "profile_present": store.browser_profile_dir.is_dir(),
            "checked_online": False}


def _browser_command(args, store: Store) -> dict:
    if args.action == "install":
        if importlib.util.find_spec("playwright") is None:
            raise BookError("browser_unavailable", "Re-run this project's installer with --browser to install optional Playwright support.")
        try:
            result = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                                    capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            raise BookError("browser_install_failed", "Chromium installation exceeded 300 seconds.", recoverable=True) from None
        if result.returncode:
            raise BookError("browser_install_failed", "Chromium installation failed; check network access and available disk space.")
    status = _browser_status(store)
    if args.action == "install" and not status["chromium_installed"]:
        raise BookError("browser_install_failed", "Chromium is still unavailable after installation.")
    return status


def _download_book(args, store: Store, http: Http, provider, browser=None) -> dict:
    ticket = provider.download_ticket(args.id, args.hash)
    if args.expect_format and args.expect_format.lower().lstrip(".") != ticket.extension.lower().lstrip("."):
        raise BookError("format_mismatch", "The selected book does not match --expect-format; no file was downloaded.")
    output = Path(args.output).expanduser().resolve() if args.output else store.download_dir
    if browser is not None:
        return browser.download(ticket, output, filename=args.filename, overwrite=args.overwrite)
    return download(http, ticket, output, filename=args.filename, overwrite=args.overwrite)


def execute(args) -> dict:
    if args.command == "skill":
        return install_skill(args)
    store = Store(args.home)
    if args.command == "browser":
        return _browser_command(args, store)
    if args.command == "config":
        if args.action == "set":
            store.set_value(args.key, args.value)
        return store.public()
    with Http(store.timeout) as http:
        if args.command == "search":
            return _search(args, store, http)
        if args.command == "auth":
            return _auth(args, store, http)
        if args.command == "doctor":
            return _doctor(args, store, http)
        if args.command == "quota":
            with _zlib_session(args, store, http) as session:
                result = session.run(lambda provider, browser: provider.quota())
                return {**result, "transport": session.transport}
        if args.source == "zlib" and not args.id:
            raise BookError("invalid_input", "Z-Library requires both --id and --hash.")
        if args.source == "zlib":
            with _zlib_session(args, store, http) as session:
                if args.command == "info":
                    result = session.run(lambda provider, browser: asdict(provider.info(args.id, args.hash)))
                else:
                    result = session.run(lambda provider, browser: _download_book(args, store, http, provider, browser))
                return {**result, "transport": session.transport}
        if args.transport == "browser":
            raise BookError("invalid_input", "Browser transport is supported only for Z-Library.")
        provider = _anna(store, http)
        if args.command == "info":
            return {**asdict(provider.info(args.id, args.hash)), "transport": "http"}
        return {**_download_book(args, store, http, provider), "transport": "http"}


def main() -> int:
    try:
        args = parser().parse_args()
        result = execute(args)
        print(json.dumps({"ok": True, **result}, ensure_ascii=False, allow_nan=False))
        return 0
    except BookError as error:
        print(json.dumps({"ok": False, "error": error.to_dict()}, ensure_ascii=False), file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print(json.dumps({"ok": False, "error": {"code": "cancelled", "message": "Operation cancelled.", "recoverable": False}}), file=sys.stderr)
        return 130
    except OSError:
        print(json.dumps({"ok": False, "error": {"code": "filesystem_error", "message": "Filesystem operation failed; check paths and permissions.", "recoverable": False}}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
