"""Finder-friendly entry point for the packaged macOS app.

Opening ``Synchri.app`` starts the local interface.  Supplying arguments keeps
the very same executable useful to managed agents for ``activity``, ``read``,
and the rest of the CLI, so a bundled install never depends on PATH being set
up in a provider's terminal.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from pathlib import Path

# Running the source file directly is convenient when validating release
# tooling. PyInstaller already places the package on its import path.
if not getattr(sys, "frozen", False) and __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from synchri.cli.main import main as cli_main
from synchri.config import Workspace, resolve_workspace


# LaunchServices gives Finder-opened applications only the system search path.
# Keep this list side-effect free: reading shell startup files here would execute
# arbitrary user commands every time the app opens and can block on prompts.
_DESKTOP_CLI_DIRECTORIES = (
    ".local/bin",
    "bin",
    ".cargo/bin",
    ".npm-global/bin",
    ".volta/bin",
    ".bun/bin",
    ".local/share/mise/shims",
    ".asdf/shims",
    "Library/pnpm",
)
_VERSION_MANAGER_CLI_GLOBS = (
    ".nvm/versions/node/*/bin",
    ".local/share/fnm/node-versions/*/installation/bin",
    "Library/Application Support/fnm/node-versions/*/installation/bin",
)
_SYSTEM_CLI_DIRECTORIES = (
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
    "/Applications/ChatGPT.app/Contents/Resources",
)
_VERSION_DIRECTORY = re.compile(
    r"^v?(?P<major>\d+)(?:\.(?P<minor>\d+))?(?:\.(?P<patch>\d+))?(?P<suffix>[-+].*)?$"
)


def _connected_cli_directories(workspace: Workspace) -> list[str]:
    """Return executable directories from still-connected runtime records.

    The database is opened read-only so preparing the desktop environment can
    never create or migrate state. A record contributes only the exact runtime
    executable it originally verified; unrelated or malformed paths are
    ignored instead of becoming executable search locations.
    """
    database = workspace.db_path
    if not database.is_file():
        return []
    connection = None
    try:
        connection = sqlite3.connect(
            f"{database.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=0.25,
        )
        rows = connection.execute(
            "SELECT runtime, executable_path FROM runtime_connections "
            "WHERE state = 'connected' AND executable_path IS NOT NULL"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        if connection is not None:
            connection.close()

    # Imported lazily to keep this Finder entry point cheap and avoid making
    # its environment setup part of the session-mode import graph.
    from synchri.session.modes import KNOWN_RUNTIMES

    directories: list[str] = []
    for runtime, raw_path in rows:
        expected = (KNOWN_RUNTIMES.get(runtime) or {}).get("executable")
        candidate = Path(raw_path) if raw_path else None
        if (
            expected
            and candidate is not None
            and candidate.is_absolute()
            and candidate.name == expected
            and candidate.is_file()
            and os.access(candidate, os.X_OK)
        ):
            directories.append(str(candidate.parent))
    return directories


def _version_manager_sort_key(path: Path) -> tuple[int, int, int, bool, str]:
    """Order version-manager bins newest-first without lexical v9/v20 errors."""
    for part in reversed(path.parts):
        match = _VERSION_DIRECTORY.fullmatch(part)
        if match:
            return (
                int(match.group("major")),
                int(match.group("minor") or 0),
                int(match.group("patch") or 0),
                match.group("suffix") is None,
                part,
            )
    return (0, 0, 0, False, str(path))


def desktop_cli_path(
    current: str | None = None,
    *,
    home: Path | None = None,
    workspace: Workspace | None = None,
) -> str:
    """Build the deterministic PATH inherited by the engine and its agents."""
    user_home = (home or Path.home()).expanduser()
    existing = (os.environ.get("PATH", "") if current is None else current).split(os.pathsep)
    connected = _connected_cli_directories(workspace or resolve_workspace())
    version_managers = [
        str(path)
        for pattern in _VERSION_MANAGER_CLI_GLOBS
        for path in sorted(
            user_home.glob(pattern),
            key=_version_manager_sort_key,
            reverse=True,
        )
    ]
    candidates = [
        *existing,
        *connected,
        *(str(user_home / relative) for relative in _DESKTOP_CLI_DIRECTORIES),
        *version_managers,
        *_SYSTEM_CLI_DIRECTORIES,
    ]
    result: list[str] = []
    seen: set[str] = set()
    for value in candidates:
        if not value:
            continue
        normalized = str(Path(value).expanduser())
        if normalized in seen or not Path(normalized).is_dir():
            continue
        seen.add(normalized)
        result.append(normalized)
    return os.pathsep.join(result)


def prepare_desktop_environment(workspace: Workspace | None = None) -> None:
    """Give a Finder-launched app the CLI environment a terminal already has."""
    os.environ["PATH"] = desktop_cli_path(workspace=workspace)


def _requested_workspace(args: list[str]) -> Workspace:
    """Resolve the global ``--home`` early enough to prepare desktop PATH."""
    for index, argument in enumerate(args):
        if argument == "--home" and index + 1 < len(args):
            return resolve_workspace(args[index + 1])
        if argument.startswith("--home="):
            return resolve_workspace(argument.partition("=")[2])
    return resolve_workspace()


def main() -> int:
    # Finder adds this opaque process-serial-number argument on some macOS
    # versions.  It is not a command the CLI should try to parse.
    args = [arg for arg in sys.argv[1:] if not arg.startswith("-psn_")]
    prepare_desktop_environment(_requested_workspace(args))
    return cli_main(args or ["ui"])


if __name__ == "__main__":  # pragma: no cover - exercised by the release build
    raise SystemExit(main())
