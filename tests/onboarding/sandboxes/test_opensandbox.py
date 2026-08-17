"""Tests for :mod:`omnigent.onboarding.sandboxes.opensandbox`."""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

import click
import pytest

from omnigent.onboarding.sandboxes.base import DEFAULT_HOST_IMAGE
from omnigent.onboarding.sandboxes.opensandbox import (
    API_KEY_ENV_VAR,
    DOMAIN_ENV_VAR,
    HOST_IMAGE_ENV_VAR,
    MAX_LIFETIME_ENV_VAR,
    PROTOCOL_ENV_VAR,
    READY_TIMEOUT_ENV_VAR,
    REQUEST_TIMEOUT_ENV_VAR,
    SANDBOX_ENV_PASSTHROUGH_ENV_VAR,
    SERVER_PROXY_ENV_VAR,
    SNAPSHOT_ID_ENV_VAR,
    OpenSandboxLauncher,
    managed_token_ttl_s,
)


class _NotFound(Exception):
    status_code = 404


@dataclass
class _State:
    create_kwargs: dict[str, object] = field(default_factory=dict)
    connect_kwargs: dict[str, object] = field(default_factory=dict)
    command: str | None = None
    exit_code: int | None = 0
    stdout: list[str] = field(default_factory=list)
    stderr: list[str] = field(default_factory=list)
    killed: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    status: str = "running"
    missing: bool = False
    ready_error: Exception | None = None
    preflight_error: Exception | None = None


class _ConnectionConfig:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs


class _RunCommandOpts:
    pass


class _SandboxFilter:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs


class _Commands:
    def __init__(self, state: _State) -> None:
        self._state = state

    def run(self, command: str, *, opts: object) -> object:
        del opts
        self._state.command = command
        return SimpleNamespace(
            id="cmd-1",
            exit_code=self._state.exit_code,
            error=None,
            logs=SimpleNamespace(
                stdout=[SimpleNamespace(text=line) for line in self._state.stdout],
                stderr=[SimpleNamespace(text=line) for line in self._state.stderr],
            ),
        )

    def get_command_status(self, command_id: str) -> object:
        del command_id
        return SimpleNamespace(exit_code=self._state.exit_code)


class _Sandbox:
    state: _State

    def __init__(self, sandbox_id: str = "sb-1") -> None:
        self.id = sandbox_id
        self.commands = _Commands(self.state)

    @classmethod
    def create(cls, image: str | None, **kwargs: object) -> _Sandbox:
        cls.state.create_kwargs = {"image": image, **kwargs}
        return cls()

    @classmethod
    def connect(cls, sandbox_id: str, **kwargs: object) -> _Sandbox:
        cls.state.connect_kwargs = kwargs
        if cls.state.missing:
            raise _NotFound(sandbox_id)
        return cls(sandbox_id)

    def check_ready(self, *, timeout: timedelta, polling_interval: timedelta) -> None:
        del timeout, polling_interval
        if self.state.ready_error is not None:
            raise self.state.ready_error

    def kill(self) -> None:
        self.state.killed.append(str(self.id))
        self.state.status = "terminated"

    def close(self) -> None:
        self.state.closed.append(str(self.id))


class _Manager:
    state: _State

    @classmethod
    def create(cls, *, connection_config: object) -> _Manager:
        del connection_config
        return cls()

    def list_sandbox_infos(self, sandbox_filter: object) -> object:
        del sandbox_filter
        if self.state.preflight_error is not None:
            raise self.state.preflight_error
        return SimpleNamespace(items=[])

    def get_sandbox_info(self, sandbox_id: str) -> object:
        if self.state.missing:
            raise _NotFound(sandbox_id)
        return SimpleNamespace(status=SimpleNamespace(state=self.state.status))

    def kill_sandbox(self, sandbox_id: str) -> None:
        if self.state.missing:
            raise _NotFound(sandbox_id)
        self.state.killed.append(sandbox_id)
        self.state.status = "terminated"

    def close(self) -> None:
        self.state.closed.append("manager")


@pytest.fixture()
def sdk(monkeypatch: pytest.MonkeyPatch) -> _State:
    state = _State()
    monkeypatch.setattr(_Sandbox, "state", state, raising=False)
    monkeypatch.setattr(_Manager, "state", state, raising=False)

    root = types.ModuleType("opensandbox")
    root.SandboxSync = _Sandbox  # type: ignore[attr-defined]
    root.SandboxManagerSync = _Manager  # type: ignore[attr-defined]
    config = types.ModuleType("opensandbox.config")
    config.ConnectionConfigSync = _ConnectionConfig  # type: ignore[attr-defined]
    models = types.ModuleType("opensandbox.models")
    execd = types.ModuleType("opensandbox.models.execd")
    execd.RunCommandOpts = _RunCommandOpts  # type: ignore[attr-defined]
    sandboxes = types.ModuleType("opensandbox.models.sandboxes")
    sandboxes.SandboxFilter = _SandboxFilter  # type: ignore[attr-defined]
    for name, module in (
        ("opensandbox", root),
        ("opensandbox.config", config),
        ("opensandbox.models", models),
        ("opensandbox.models.execd", execd),
        ("opensandbox.models.sandboxes", sandboxes),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    monkeypatch.setenv(API_KEY_ENV_VAR, "secret-key")
    monkeypatch.setenv(DOMAIN_ENV_VAR, "sandbox.example.com")
    monkeypatch.setenv(SERVER_PROXY_ENV_VAR, "true")
    for name in (
        HOST_IMAGE_ENV_VAR,
        MAX_LIFETIME_ENV_VAR,
        PROTOCOL_ENV_VAR,
        READY_TIMEOUT_ENV_VAR,
        REQUEST_TIMEOUT_ENV_VAR,
        SANDBOX_ENV_PASSTHROUGH_ENV_VAR,
        SNAPSHOT_ID_ENV_VAR,
    ):
        monkeypatch.delenv(name, raising=False)
    return state


def test_constructor_builds_connection_and_validates_image_snapshot(sdk: _State) -> None:
    launcher = OpenSandboxLauncher()
    assert launcher._image == DEFAULT_HOST_IMAGE
    kwargs = cast(Any, launcher._connection_config).kwargs
    assert kwargs["domain"] == "sandbox.example.com"
    assert kwargs["use_server_proxy"] is True
    with pytest.raises(click.ClickException, match="only one"):
        OpenSandboxLauncher(image="img", snapshot_id="snap")


def test_provision_from_snapshot_omits_image(sdk: _State) -> None:
    assert OpenSandboxLauncher(snapshot_id="snap-1").provision("snapshot-test") == "sb-1"
    assert sdk.create_kwargs["image"] is None
    assert sdk.create_kwargs["snapshot_id"] == "snap-1"


def test_capabilities_are_managed_only(sdk: _State) -> None:
    capabilities = OpenSandboxLauncher().capabilities
    assert capabilities.managed_launch is True
    assert capabilities.cli_bootstrap is False
    assert capabilities.programmatic_terminate is True


def test_provision_uses_image_lifetime_metadata_and_env(
    sdk: _State, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "llm-secret")
    launcher = OpenSandboxLauncher(
        image="registry.example/host:v1",
        env=["OPENAI_API_KEY"],
        max_lifetime_s=7200,
        ready_timeout_s=240,
    )
    assert launcher.provision("managed-test") == "sb-1"
    assert sdk.create_kwargs["image"] == "registry.example/host:v1"
    assert sdk.create_kwargs["timeout"] == timedelta(seconds=7200)
    assert sdk.create_kwargs["ready_timeout"] == timedelta(seconds=240)
    assert sdk.create_kwargs["env"] == {"OPENAI_API_KEY": "llm-secret"}
    assert sdk.create_kwargs["metadata"] == {
        "omnigent-name": "managed-test",
        "omnigent-provider": "opensandbox",
    }


def test_env_passthrough_rejects_missing_and_control_credentials(sdk: _State) -> None:
    with pytest.raises(click.ClickException, match="NOT_SET"):
        OpenSandboxLauncher(env=["NOT_SET"]).provision("x")
    with pytest.raises(click.ClickException, match="control credential"):
        OpenSandboxLauncher(env=[API_KEY_ENV_VAR]).provision("x")


def test_readiness_failure_cleans_up_and_redacts_api_key(sdk: _State) -> None:
    sdk.ready_error = RuntimeError("backend rejected secret-key")
    with pytest.raises(click.ClickException, match=r"\[redacted\]") as exc:
        OpenSandboxLauncher().provision("x")
    assert "secret-key" not in str(exc.value)
    assert sdk.killed == ["sb-1"]
    assert "sb-1" in sdk.closed


def test_run_returns_output_and_checks_exit_status(sdk: _State) -> None:
    sdk.stdout = ["hello\n"]
    sdk.stderr = ["warning\n"]
    result = OpenSandboxLauncher().run("sb-1", "echo hello", check=False)
    assert result.returncode == 0
    assert result.stdout == "hello"
    assert result.stderr == "warning"
    assert sdk.command == "echo hello"

    sdk.exit_code = 3
    with pytest.raises(click.ClickException, match="exit 3"):
        OpenSandboxLauncher().run("sb-1", "false")


def test_terminate_handles_cached_uncached_and_missing(sdk: _State) -> None:
    launcher = OpenSandboxLauncher()
    launcher.provision("x")
    launcher.terminate("sb-1")
    assert sdk.killed == ["sb-1"]

    sdk.status = "running"
    launcher.terminate("sb-2")
    assert sdk.killed[-1] == "sb-2"

    sdk.missing = True
    launcher.terminate("already-gone")
    assert sdk.killed == ["sb-1", "sb-2"]


def test_prepare_and_is_running(sdk: _State) -> None:
    launcher = OpenSandboxLauncher()
    launcher.prepare()
    assert "manager" in sdk.closed
    assert launcher.is_running("sb-1") is True
    sdk.status = "terminated"
    assert launcher.is_running("sb-1") is False
    sdk.missing = True
    assert launcher.is_running("sb-1") is False


def test_prepare_redacts_provider_errors(sdk: _State) -> None:
    sdk.preflight_error = RuntimeError("backend rejected secret-key")
    with pytest.raises(click.ClickException, match=r"\[redacted\]") as exc:
        OpenSandboxLauncher().prepare()
    assert "secret-key" not in str(exc.value)


def test_managed_token_ttl_tracks_lifetime(sdk: _State) -> None:
    assert managed_token_ttl_s() == 25 * 3600
    assert managed_token_ttl_s(7200) == 10_800
    with pytest.raises(click.ClickException, match="at least 60"):
        managed_token_ttl_s(59)
