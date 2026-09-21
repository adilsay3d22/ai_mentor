"""Paths, settings, the capture blocklist, and logging setup.

Nothing here knows about Qt. This module is safe to import before the
QApplication exists, which matters because logging is configured first.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Final

import structlog

# --- Paths -------------------------------------------------------------------

PACKAGE_DIR: Final[Path] = Path(__file__).resolve().parent
PROJECT_ROOT: Final[Path] = PACKAGE_DIR.parent

#: SR5 — screenshots are never persisted anywhere but here, and this is gitignored.
DEBUG_DIR: Final[Path] = PROJECT_ROOT / "debug"
LOG_PATH: Final[Path] = DEBUG_DIR / "mentor.log"
FIXTURES_DIR: Final[Path] = PROJECT_ROOT / "tests" / "fixtures" / "screens"


def load_dotenv(path: Path | None = None) -> None:
    """Read ``KEY=value`` lines from ``.env`` into the environment.

    Deliberately hand-rolled rather than adding python-dotenv: this is ten lines
    and the dependency list in ``architecture.md`` is meant to stay short.
    Existing environment variables always win, so a real environment variable is
    never silently overridden by a stale file.
    """
    env_path = path or (PROJECT_ROOT / ".env")
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


def api_key() -> str | None:
    """The Anthropic API key, or None if it has not been provided.

    Never logged, never written anywhere. ``.env`` is gitignored.
    """
    return _key_from_env("ANTHROPIC_API_KEY")


def openrouter_api_key() -> str | None:
    """The OpenRouter API key, or None. Same handling as the Anthropic one."""
    return _key_from_env("OPENROUTER_API_KEY")


def _key_from_env(name: str) -> str | None:
    load_dotenv()
    key = os.environ.get(name, "").strip()
    return key or None


def user_data_dir() -> Path:
    """Where the application map lives. Per-user, outside the repository."""
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "Mentor"
    return Path.home() / ".mentor"


def appmap_path() -> Path:
    return user_data_dir() / "appmap.db"


# --- Settings ----------------------------------------------------------------

HOTKEY: Final[str] = "ctrl+shift+space"

#: Manual advance, until the verifier in phase 6 makes it automatic.
NEXT_HOTKEY: Final[str] = "ctrl+shift+right"

#: The planner model. Opus 5 unless this is deliberately changed.
PLANNER_MODEL: Final[str] = "claude-opus-5"

#: Which planner to use: anthropic | openrouter | offline. Overridden by
#: --provider on the command line.
PLANNER_PROVIDER: Final[str] = "openrouter"

#: The model to ask for when going through OpenRouter.
#:
#: A **free** model by default, deliberately. A paid default means one
#: forgotten flag turns a test run into a bill; a free default means the worst
#: case is a worse step. Quality costs a flag:
#:     --model anthropic/claude-opus-5
#: Free models are rate limited per day rather than per token, and share an
#: upstream pool across everyone using them, so a long session can run out of
#: requests rather than money, and a model can be unavailable through no
#: fault of the key. ``planner/openrouter.py`` lists measured alternatives.
OPENROUTER_MODEL: Final[str] = "nex-agi/nex-n2.5-mini:free"

#: Effort trades thinking depth against latency. NFR1 allows 3s from the user
#: acting to the next highlight, and grounding already spends 1.4-2.4s of that,
#: so the planner runs at low effort. Picking one element from a supplied list is
#: not a problem that rewards deep reasoning; raise this if step quality suffers.
PLANNER_EFFORT: Final[str] = "low"

#: One step of JSON. Nowhere near this is needed, but a truncated response is a
#: failed step, and unused output tokens cost nothing.
PLANNER_MAX_TOKENS: Final[int] = 2048

#: How many times a rejected step may be re-requested before giving up honestly.
#: One, per architecture.md: "re-request once with the violation described".
PLANNER_MAX_RETRIES: Final[int] = 1

#: How long a step may sit unacknowledged before the caption softens (design.md).
WAITING_NUDGE_SECONDS: Final[float] = 60.0

#: Screen-change polling rate while waiting for the user to act.
WAIT_POLL_HZ: Final[float] = 2.0

#: Below this, the ring renders amber and the caption is prefixed "I think — ".
UNSURE_CONFIDENCE: Final[float] = 0.7

#: SR4 — applications Mentor refuses to capture. Process names, lowercased.
#: User-editable in phase 10; this is the shipped default.
DEFAULT_BLOCKLIST: Final[frozenset[str]] = frozenset(
    {
        "1password.exe",
        "bitwarden.exe",
        "keepass.exe",
        "keepassxc.exe",
        "dashlane.exe",
        "lastpass.exe",
        "enpass.exe",
        "nordpass.exe",
        "protonpass.exe",
        "credentialuibroker.exe",
        "lsass.exe",
    }
)


def is_blocked(process_name: str) -> bool:
    """True if SR4 forbids capturing this process."""
    return process_name.strip().lower() in DEFAULT_BLOCKLIST


# --- Logging -----------------------------------------------------------------


def configure_logging(*, debug: bool = False) -> None:
    """Send structlog to ``./debug/mentor.log`` as JSON, and to stderr when debugging.

    JSON on disk because planner requests and responses are logged verbatim and
    will need to be read back mechanically when a highlight lands wrong.
    """
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    timestamper = structlog.processors.TimeStamper(fmt="iso")
    shared_processors: list[structlog.typing.Processor] = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(),
            foreign_pre_chain=shared_processors,
        )
    )

    handlers: list[logging.Handler] = [file_handler]
    if debug:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processor=structlog.dev.ConsoleRenderer(colors=False),
                foreign_pre_chain=shared_processors,
            )
        )
        handlers.append(console_handler)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(logging.DEBUG if debug else logging.INFO)

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
