"""The packaged entry point restores CLI discovery for Finder launches."""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

from synchri import mac_app
from synchri.config import Workspace
from synchri.runner import doctor
from synchri.session import modes
from synchri.session.modes import (
    KNOWN_RUNTIMES,
    ParticipantPlan,
    managed_command,
    planning_command,
    resolve_runtime_command,
    resolve_runtime_executable,
)
from synchri.storage import db


FINDER_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def connected_workspace(
    root: Path,
    runtime: str,
    path: Path,
    *,
    state: str = "connected",
) -> Workspace:
    workspace = Workspace(root).ensure()
    connection = db.connect(workspace.db_path)
    db.initialize(connection)
    doctor._save_connection(
        connection,
        {
            "runtime": runtime,
            "state": state,
            "executable_path": str(path),
            "version": "1.2.3",
            "adapter_revision": "test",
            "auth_indication": True,
            "resume": doctor.RESUME_UNSUPPORTED,
            "checks": [],
            "detail": "connected",
        },
    )
    connection.close()
    return workspace


def test_desktop_environment_restores_only_the_verified_runtime(tmp_path, monkeypatch):
    cli = executable(tmp_path / "custom-toolchain" / "claude")
    sibling = executable(tmp_path / "custom-toolchain" / "codex")
    executable(tmp_path / "home" / ".local" / "bin" / "claude")
    workspace = connected_workspace(tmp_path / "state", "claude_code", cli)
    monkeypatch.setenv("PATH", FINDER_PATH)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    mac_app.prepare_desktop_environment(workspace)
    resolved = os.environ["PATH"].split(os.pathsep)

    assert resolved[:4] == FINDER_PATH.split(os.pathsep)
    assert str(cli.parent) not in resolved
    assert resolve_runtime_executable("claude_code") == str(cli)
    assert resolve_runtime_executable("codex") != str(sibling)


def test_desktop_entrypoint_prepares_path_before_starting_the_ui(tmp_path, monkeypatch):
    cli = executable(tmp_path / "custom-toolchain" / "claude")
    workspace = connected_workspace(tmp_path / "state", "claude_code", cli)
    monkeypatch.setenv("PATH", FINDER_PATH)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SYNCHRI_HOME", str(workspace.home))
    monkeypatch.setattr(sys, "argv", ["Synchri", "-psn_0_12345"])

    def fake_main(args):
        assert args == ["ui"]
        assert resolve_runtime_executable("claude_code") == str(cli)
        return 0

    monkeypatch.setattr(mac_app, "cli_main", fake_main)

    assert mac_app.main() == 0


def test_desktop_entrypoint_uses_the_explicit_workspace_for_connected_clis(
    tmp_path,
    monkeypatch,
):
    cli = executable(tmp_path / "custom-toolchain" / "claude")
    requested = connected_workspace(tmp_path / "requested-state", "claude_code", cli)
    monkeypatch.setenv("PATH", FINDER_PATH)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SYNCHRI_HOME", str(tmp_path / "default-state"))
    monkeypatch.setattr(
        sys,
        "argv",
        ["Synchri", "--home", str(requested.home), "ui"],
    )

    def fake_main(args):
        assert args == ["--home", str(requested.home), "ui"]
        assert resolve_runtime_executable("claude_code") == str(cli)
        return 0

    monkeypatch.setattr(mac_app, "cli_main", fake_main)

    assert mac_app.main() == 0
    assert mac_app._requested_workspace([f"--home={requested.home}"]).home == requested.home


def test_version_manager_paths_are_ordered_by_version_not_text(tmp_path):
    old = tmp_path / "home" / ".nvm" / "versions" / "node" / "v9.0.0" / "bin"
    current = (
        tmp_path
        / "home"
        / ".local"
        / "share"
        / "fnm"
        / "node-versions"
        / "v20.0.0"
        / "installation"
        / "bin"
    )
    old.mkdir(parents=True)
    current.mkdir(parents=True)

    resolved = mac_app.desktop_cli_path(
        FINDER_PATH,
        home=tmp_path / "home",
    ).split(os.pathsep)

    assert resolved.index(str(current)) < resolved.index(str(old))


def test_desktop_environment_does_not_trust_failed_or_misnamed_runtime_records(
    tmp_path,
    monkeypatch,
):
    failed = executable(tmp_path / "failed-toolchain" / "claude")
    workspace = connected_workspace(
        tmp_path / "state",
        "claude_code",
        failed,
        state="failed",
    )
    connection = db.connect(workspace.db_path)
    doctor._save_connection(
        connection,
        {
            "runtime": "codex",
            "state": "connected",
            "executable_path": str(executable(tmp_path / "wrong-name" / "agent")),
            "version": "1.2.3",
            "adapter_revision": "test",
            "auth_indication": True,
            "resume": doctor.RESUME_UNSUPPORTED,
            "checks": [],
            "detail": "connected",
        },
    )
    connection.close()

    monkeypatch.setenv("PATH", FINDER_PATH)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    mac_app.prepare_desktop_environment(workspace)

    assert resolve_runtime_executable("claude_code") != str(failed)
    assert resolve_runtime_executable("codex") != str(tmp_path / "wrong-name" / "agent")


def test_maintained_commands_pin_the_resolved_runtime_executable(tmp_path, monkeypatch):
    claude = executable(tmp_path / "bin with spaces" / "claude")
    executable(claude.parent / "node")
    newer_node = executable(tmp_path / "newer-node" / "node")
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join((str(newer_node.parent), str(claude.parent))),
    )
    plan = ParticipantPlan("Claude", "claude_code", "primary_builder")

    for command in (managed_command(plan), planning_command(plan)):
        argv = shlex.split(command)
        assert argv[0] == "/usr/bin/env"
        assert argv[1].removeprefix("PATH=").split(os.pathsep)[0] == str(claude.parent)
        assert str(claude) in argv


def test_wrapped_canary_pins_copilot_without_losing_placeholders(tmp_path, monkeypatch):
    copilot = executable(tmp_path / "provider tools" / "copilot")
    monkeypatch.setenv("PATH", str(copilot.parent))
    definition = KNOWN_RUNTIMES["copilot"]

    command = resolve_runtime_command(
        "copilot",
        definition["connection_test_command"],
        definition=definition,
    )
    argv = shlex.split(command)

    assert argv[0] == "/usr/bin/env"
    assert argv[1].removeprefix("PATH=").split(os.pathsep)[0] == str(copilot.parent)
    assert str(copilot) in argv
    assert "{prompt}" in argv


def test_resume_command_keeps_both_deferred_values_unquoted(tmp_path, monkeypatch):
    claude = executable(tmp_path / "provider tools" / "claude")
    monkeypatch.setenv("PATH", str(claude.parent))
    definition = KNOWN_RUNTIMES["claude_code"]

    command = resolve_runtime_command(
        "claude_code",
        definition["resume_command"],
        definition=definition,
    )
    argv = shlex.split(command)

    assert argv[0] == "/usr/bin/env"
    assert argv[1].removeprefix("PATH=").split(os.pathsep)[0] == str(claude.parent)
    assert str(claude) in argv
    assert "{resume_id}" in argv
    assert "{prompt}" in argv


def test_non_posix_commands_do_not_depend_on_usr_bin_env(tmp_path, monkeypatch):
    claude = executable(tmp_path / "provider tools" / "claude")
    monkeypatch.setenv("PATH", str(claude.parent))
    monkeypatch.setattr(modes, "_ENV_EXECUTABLE", None)

    command = resolve_runtime_command(
        "claude_code",
        KNOWN_RUNTIMES["claude_code"]["managed_command"],
    )
    argv = shlex.split(command)

    assert argv[0] == str(claude)
    assert not any(argument.startswith("PATH=") for argument in argv)
