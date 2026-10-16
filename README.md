# zlibrary-download skill

A reusable Agent Skill for finding books by title or author and downloading validated ebook files through the independent `book-tools` CLI. Supports Z-Library and Anna's Archive without depending on Claude Code or an agent-specific runtime directory.

HTTP is the default for every operation. A browser is a fallback for a recognized verification challenge, not the normal download workflow. Unofficial project; not affiliated with either service. Only retrieve material you are entitled to access.

## Repository Contents

- [`skills/zlibrary-download/SKILL.md`](skills/zlibrary-download/SKILL.md) — agent workflow, book selection, browser handoff and delivery rules
- [`skills/zlibrary-download/references/cli.md`](skills/zlibrary-download/references/cli.md) — detailed installation, configuration, transport and troubleshooting reference
- [`src/book_tools/`](src/book_tools/) — standalone CLI, providers, browser transport and download validation
- [`scripts/install.py`](scripts/install.py) — isolated project/user-level runtime installer
- [`tests/`](tests/) — offline regression tests with synthetic files and controlled responses
- `pyproject.toml`, `uv.lock`, `requirements*.lock` — package metadata and pinned dependencies

## What It Does

- Searches by title/author with language, format and year filters; the agent checks that a result is the requested work, not a study guide, summary or translation.
- Tries HTTP independently for search, details, download and quota. A previous browser search never forces the next download into browser mode.
- Reuses endpoint-bound sessions. Optional visible Chromium handles verification; a working cached browser session needs no human confirmation.
- Downloads to a private temporary file, validates the expected format, computes SHA-256 and publishes without overwriting existing files by default.
- Returns structured JSON for agent use. EPUB structure validation is not an edition guarantee, malware scan or proof of complete text.

## Install as a Skill

Requires **Python 3.11+**; Python 3.13 with modern OpenSSL is recommended. Install the CLI runtime and the Skill separately.

### 1. Install the runtime

```bash
git clone https://github.com/GeederX/Z_Library_Download_Skill.git
cd Z_Library_Download_Skill
python3.13 scripts/install.py --scope global
```

The installer prints the executable and runtime paths. On macOS/Linux, add `~/.local/bin` to `PATH` if necessary, then run:

```bash
book-tools --version
book-tools doctor
```

No sudo or system-Python package installation. Project-local installation is also available:

```bash
python3.13 scripts/install.py --scope project
.venv/bin/book-tools doctor
```

On Windows, use a compatible Python interpreter; the project entry point is `.venv\Scripts\book-tools.exe`. An installed command can run from any working directory. Do not move an existing virtual environment; reinstall after moving the source checkout.

### 2. Install the Skill

With the [Skills CLI](https://skills.sh/), which requires Node.js/npx:

```bash
npx skills add GeederX/Z_Library_Download_Skill --skill zlibrary-download --global
```

Or register the bundled Skill using the installed CLI, without Node.js:

```bash
book-tools skill install --scope global
```

The built-in installer uses `~/.agents/skills/zlibrary-download` and refuses to overwrite an existing Skill. Use `--scope project` for `.agents/skills/`, or `--directory /absolute/path/to/agent/skills` for another host. Alternatively, copy **all of** `skills/zlibrary-download/`, including `references/`, into your agent's supported skills directory.

Start a new agent session if needed for discovery. Different hosts control their own trust and reload behavior; installation is not a claim of tested compatibility with every agent.

### Optional browser support

Only needed when HTTP is actually blocked:

```bash
python3.13 scripts/install.py --scope global --browser
book-tools browser status
```

This explicitly installs the optional Playwright dependency and Chromium. Ordinary searches do not silently install a browser. Browser availability is not evidence that a site is logged in or has passed verification.

Re-run the runtime installer to upgrade; it preserves account configuration and browser Profiles. Update the installed Skill separately with the Skills CLI or an explicitly reviewed replacement. The former `zlib-download` Skill name has been replaced by `zlibrary-download`; do not keep both installed for the same workflow.

## Configure

```bash
book-tools config set download_dir ~/Downloads/Books
book-tools auth login --source zlib
book-tools auth status --source zlib
```

Run terminal login yourself: the email is visible, while passwords and API keys are hidden. Never paste passwords/API keys into agent chat or put them in command arguments. Z-Library caches endpoint-bound session tokens, not the password. Anna search is anonymous; downloads require a key from the configured service's API/donation account page, entered with `book-tools auth login --source annas`.

`book-tools config show` reports the actual OS-specific directories. Credentials, session tokens and browser Profiles live outside the Skill directory. Configure a different endpoint only after verifying that it is trusted; the tool does not discover mirrors or forward cached credentials to a new origin.

For manual website login, use `book-tools auth login --source zlib --transport browser`. See the [CLI reference](skills/zlibrary-download/references/cli.md) for environment-variable precedence, storage permissions and configuration options.

## Usage

Ask the agent, for example: “Find *The Cricket in Times Square* by George Selden in English EPUB and download it.” The agent follows [`SKILL.md`](skills/zlibrary-download/SKILL.md) to select the book, handle any verification and deliver the validated local file.

Or use the CLI directly:

```bash
book-tools search "The Cricket in Times Square" --source zlib --lang english --ext epub
book-tools download --source zlib --id BOOK_ID --hash BOOK_HASH --expect-format epub
book-tools quota
```

Replace `BOOK_ID` and `BOOK_HASH` with real search results. Existing files are preserved unless `--overwrite` is explicitly authorized. A successful download receipt includes the absolute path, format, byte size and SHA-256.

### Browser fallback

| Transport | Behavior |
|---|---|
| `auto` (default) | HTTP first for each command. Only a recognized verification challenge warrants browser fallback. |
| `http` | Never opens a browser; returns an error if blocked. Use for strictly unattended operation. |
| `browser` | Explicitly opens dedicated visible Chromium and reuses its private Profile. Working sessions continue automatically. |

In an agent/non-TTY call, `auto` returns `browser_required` instead of silently opening a window. The agent explains the handoff, then retries **only that blocked command** with `--transport browser`. Interactive terminals may perform that fallback themselves. Ordinary permission, rate-limit and network errors do not trigger a browser.

If human action is required, complete the site's verification/login and follow the prominent book-tools card. A normal page needs no repeated verification: click **我已完成验证，继续 / Continue book-tools** if the card is waiting. The CLI resumes API calls and native downloads; the agent does not need another browser tool to click Download. CAPTCHA is never solved automatically. Later `auto` commands start with HTTP again.

## Verification

- macOS arm64: the earlier full regression run passed 265 tests; the latest transport change passed 20 targeted tests. No full-suite rerun was requested.
- Real CLI + visible Chromium against an isolated local service: cached browser search without confirmation, browser search followed by HTTP EPUB download, structured errors for streamed verification pages, and interactive HTTP-to-browser download fallback.
- Real Z-Library browser search and validated English EPUB download were previously exercised. That does not guarantee future site availability. OPF metadata differed from the catalogue year, so edition claims require separate checks.
- Windows/Linux code paths exist but have not been verified on physical machines. Hosts' individual Skill interfaces have not all been tested.

Tests use synthetic data, not real account credentials or downloaded copyrighted books. More detailed behavior and platform limits are documented in the [CLI reference](skills/zlibrary-download/references/cli.md).

## Development

```bash
uv sync --python 3.13 --extra browser
uv run --extra browser book-tools doctor
uv run --extra browser pytest tests/test_cli.py
```

The browser extra is optional for HTTP-only development. Dependency pins are maintained in `uv.lock` and the two requirements lockfiles. Do not commit credential files, browser Profiles, virtual environments or downloaded books.

## License

MIT. See [`LICENSE`](LICENSE) for this project's license and retained upstream notices.

Z-Library protocol knowledge references [Bipinkrish/Zlibrary-API](https://github.com/Bipinkrish/Zlibrary-API); workflow inspiration comes from [psylch/media-master](https://github.com/psylch/media-master). Anna endpoints and HTML selectors reference [iosifache/annas-mcp](https://github.com/iosifache/annas-mcp); this project does not ship or run its binary. Service access and content rights remain the user's responsibility.
