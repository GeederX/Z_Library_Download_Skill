#!/usr/bin/env python3
"""Install runtime into its own venv; never modify the system site-packages."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import venv


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", choices=("global", "project"), default="project")
    parser.add_argument("--bin-dir", type=Path, help="Global command directory (default ~/.local/bin)")
    parser.add_argument("--browser", action="store_true", help="Install optional Playwright support and its Chromium runtime")
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        parser.error("Python 3.11+ is required; run this script with a compatible interpreter.")
    root = Path(__file__).resolve().parents[1]
    if args.scope == "project":
        environment = root / ".venv"
    else:
        if sys.platform == "darwin":
            data = Path.home() / "Library" / "Application Support"
        elif os.name == "nt":
            data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        else:
            data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        environment = data / "book-tools" / "runtime"
    executable_dir = environment / ("Scripts" if os.name == "nt" else "bin")
    python = executable_dir / ("python.exe" if os.name == "nt" else "python")
    command = executable_dir / ("book-tools.exe" if os.name == "nt" else "book-tools")
    link = None
    if args.scope == "global":
        bin_dir = (args.bin_dir or Path.home() / ".local" / "bin").expanduser().resolve()
        link = bin_dir / ("book-tools.cmd" if os.name == "nt" else "book-tools")
        if link.exists() or link.is_symlink():
            owned = link.is_symlink() and link.resolve() == command.resolve()
            if os.name == "nt" and link.is_file():
                owned = link.read_text(encoding="utf-8") == f'@"{command}" %*\n'
            if not owned:
                parser.error("The destination command already exists and is not managed by this installer; select --bin-dir.")
    venv.EnvBuilder(with_pip=True).create(environment)
    constraints = root / ("requirements-browser.lock" if args.browser else "requirements.lock")
    package = f"{root}[browser]" if args.browser else str(root)
    subprocess.run([str(python), "-m", "pip", "install", "--upgrade", "--constraint", str(constraints), package], check=True)
    if args.browser:
        subprocess.run([str(python), "-m", "playwright", "install", "chromium"], check=True)
    if link:
        link.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            link.write_text(f'@"{command}" %*\n', encoding="utf-8")
        elif not link.is_symlink():
            link.symlink_to(command)
    subprocess.run([str(command), "--version"], check=True)
    print(json.dumps({"scope": args.scope, "command": str(link or command), "environment": str(environment), "note": "Add the command directory to PATH if needed. Skill registration is a separate explicit step."}))


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.returncode) from None
