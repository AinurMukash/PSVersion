"""Fixture-only runtime and injectable pipeline runner.

This module is intentionally independent from the production pipeline, which
is implemented in later tasks.  It provides the stable test boundary needed by
those tasks: a validated fixture profile, scripted ``FakeTransport``, fake
clock/sleeper, isolated workspace and a no-network guard.  A future pipeline
can be passed as a callable or as an object exposing ``run``.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlencode, urlsplit, urlunsplit

from enbek_monitor.config import MonitorConfig, load_config
from enbek_monitor.source_discovery import SourceProfile, discover_source

from tests.fixtures.loader import DEFAULT_MANIFEST_PATH, FixtureManifest, load_manifest
from tests.support.fakes import FakeClock, FakeResponse, FakeSleeper, FakeTransport
from tests.support.network_guard import NoNetworkGuard, no_network
from tests.support.workspace import (
    TemporaryWorkspace,
    WorkspaceBytes,
    capture_workspace_bytes,
)


@dataclass(frozen=True, slots=True)
class FixtureRuntime:
    """All local inputs and test doubles used by one fixture-backed run."""

    manifest: FixtureManifest
    profile: SourceProfile
    transport: FakeTransport
    clock: FakeClock
    sleeper: FakeSleeper
    workspace: TemporaryWorkspace
    config: MonitorConfig


@dataclass(frozen=True, slots=True)
class HarnessResult:
    """Pipeline result plus before/after durable-artifact observations."""

    value: Any
    runtime: FixtureRuntime
    before: WorkspaceBytes
    after: WorkspaceBytes
    error: BaseException | None = None

    @property
    def failed(self) -> bool:
        return self.error is not None

    @property
    def artifacts_unchanged(self) -> bool:
        return self.before.files == self.after.files

    @property
    def current_history_docs_unchanged(self) -> bool:
        return self.artifacts_unchanged

    def assert_unchanged(self) -> None:
        if not self.artifacts_unchanged:
            raise AssertionError(
                "current/history/docs changed during fixture run: "
                f"before={sorted(self.before.files)}, after={sorted(self.after.files)}"
            )


class FixturePipelineHarness:
    """Build and execute one pipeline with only local fixture dependencies."""

    def __init__(
        self,
        workspace: TemporaryWorkspace | str | Path,
        *,
        manifest: FixtureManifest | str | Path | None = None,
        profile_path: str | Path | None = None,
        transport: FakeTransport | None = None,
        clock: FakeClock | None = None,
        sleeper: FakeSleeper | None = None,
    ) -> None:
        self._workspace_owner = not isinstance(workspace, TemporaryWorkspace)
        if isinstance(workspace, TemporaryWorkspace) and workspace.root is None:
            # Accept an inactive helper as a convenience while leaving cleanup
            # ownership with the caller that supplied the helper.
            workspace.__enter__()
        self.workspace = workspace
        self.manifest = _load_manifest(manifest)
        self.profile_path = Path(
            profile_path or self.manifest.resolve(self.manifest.fallback["profile_path"])
        ).resolve()
        self.clock = clock or FakeClock()
        self.sleeper = sleeper or FakeSleeper(self.clock)
        self.profile = discover_source(fixture_profile_path=self.profile_path)
        self.transport = transport or build_fixture_transport(
            self.manifest,
            self.profile,
            clock=self.clock,
        )
        root = _workspace_root(workspace)
        # Pass the already validated in-memory profile to ConfigLoader.  The
        # fallback profile lives under tests/fixtures, intentionally outside
        # Local_Workspace, so treating it as a workspace-relative path would
        # make an otherwise offline harness fail before it starts.
        self.config = load_config(
            workspace=root,
            fixture_profile=self.profile,
            source_transport="fixture",
        )

    def runtime(self) -> FixtureRuntime:
        workspace = self.workspace
        if not isinstance(workspace, TemporaryWorkspace):
            workspace = TemporaryWorkspace(workspace)
            workspace.__enter__()
            self.workspace = workspace
        return FixtureRuntime(
            manifest=self.manifest,
            profile=self.profile,
            transport=self.transport,
            clock=self.clock,
            sleeper=self.sleeper,
            workspace=workspace,
            config=self.config,
        )

    def run(
        self,
        pipeline: Callable[..., Any] | Any,
        *,
        mode: str = "sync",
        raise_on_error: bool = False,
        **kwargs: Any,
    ) -> HarnessResult:
        """Run ``pipeline`` under the no-network guard and capture state bytes.

        The callable receives only supported keyword arguments selected from
        its signature.  This permits both ``pipeline(runtime=...)`` and a
        production-style ``pipeline.run(config=..., transport=..., mode=...)``
        without coupling this test helper to future orchestration signatures.
        Failed runs are captured in the returned result by default; pass
        ``raise_on_error=True`` when a test expects the original exception.
        """

        runtime = self.runtime()
        before = capture_workspace_bytes(runtime.workspace)
        value: Any = None
        error: BaseException | None = None
        try:
            with no_network(allowed_transports=(runtime.transport,)) as guard:
                guard.assert_transport(runtime.transport)
                value = _invoke_pipeline(
                    pipeline,
                    runtime=runtime,
                    config=runtime.config,
                    workspace=runtime.workspace.path,
                    transport=runtime.transport,
                    profile=runtime.profile,
                    mode=mode,
                    clock=runtime.clock,
                    sleeper=runtime.sleeper,
                    **kwargs,
                )
        except BaseException as exc:
            error = exc
            if raise_on_error:
                after = capture_workspace_bytes(runtime.workspace)
                raise
        after = capture_workspace_bytes(runtime.workspace)
        return HarnessResult(
            value=value,
            runtime=runtime,
            before=before,
            after=after,
            error=error,
        )

    execute = run


# Short alias used by integration tests.
FixtureHarness = FixturePipelineHarness


def run_fixture_pipeline(
    pipeline: Callable[..., Any] | Any,
    *,
    workspace: TemporaryWorkspace | str | Path | None = None,
    mode: str = "sync",
    manifest: FixtureManifest | str | Path | None = None,
    profile_path: str | Path | None = None,
    raise_on_error: bool = False,
    **kwargs: Any,
) -> HarnessResult:
    """Convenience wrapper for one isolated fixture-backed pipeline run."""

    owner = workspace is None
    workspace_value: TemporaryWorkspace | str | Path = (
        TemporaryWorkspace() if owner else workspace  # type: ignore[assignment]
    )
    if owner:
        with workspace_value as temporary:
            runner = FixturePipelineHarness(
                temporary,
                manifest=manifest,
                profile_path=profile_path,
            )
            return runner.run(
                pipeline,
                mode=mode,
                raise_on_error=raise_on_error,
                **kwargs,
            )
    runner = FixturePipelineHarness(
        workspace_value,
        manifest=manifest,
        profile_path=profile_path,
    )
    return runner.run(
        pipeline,
        mode=mode,
        raise_on_error=raise_on_error,
        **kwargs,
    )


def build_fixture_transport(
    manifest: FixtureManifest,
    profile: SourceProfile,
    *,
    clock: FakeClock | None = None,
) -> FakeTransport:
    """Create exact URL routes from the manifest fallback fixture set."""

    fallback = manifest.fallback
    root = manifest.fixtures_root
    transport = FakeTransport(clock=clock or FakeClock())

    registry_paths = _string_sequence(fallback.get("registry_page_paths"))
    for index, relative_path in enumerate(registry_paths, start=1):
        response_bytes = manifest.resolve(relative_path).read_bytes()
        response = FakeResponse(
            200,
            body=response_bytes,
            headers={"Content-Type": "application/json"},
            url=profile.registry_url,
            request_kind="registry",
        )
        # Register the exact fixture next links and common query forms.  The
        # first page is also available at the profile URL used by a collector.
        if index == 1:
            transport.register(profile.registry_url, response, kind="registry")
        query = dict(profile.query)
        page_key = str(profile.pagination.get("request", {}).get("page_parameter", "page"))
        query[page_key] = index
        query_url = _with_query(profile.registry_url, query)
        transport.register(query_url, response, kind="registry")
        fixture_url = f"fixture://enbek/registry/page-{index:03d}.json"
        transport.register(fixture_url, response, kind="registry")

    for enbek_id, relative_path in _string_mapping(fallback.get("detail_paths")).items():
        response_bytes = manifest.resolve(relative_path).read_bytes()
        try:
            document = json.loads(response_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            document = {}
        detail_url = f"https://career.enbek.kz/ru/professionalstandart/{enbek_id}"
        transport.register(
            detail_url,
            FakeResponse(
                200,
                body=response_bytes,
                headers={"Content-Type": "application/json"},
                url=detail_url,
                request_kind="detail",
            ),
            kind="detail",
        )
        # Preserve any source URL explicitly embedded by a fixture as an
        # additional exact route, without deriving a new business URL.
        if isinstance(document, Mapping):
            source_url = document.get("page_url")
            if isinstance(source_url, str) and source_url != detail_url:
                transport.register(detail_url, transport._route_for(detail_url, "detail"), kind="detail")  # type: ignore[arg-type]

    for enbek_id, relative_path in _string_mapping(fallback.get("pdf_response_paths")).items():
        descriptor_path = manifest.resolve(relative_path)
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        body_path = descriptor.get("body_path")
        body = b""
        if isinstance(body_path, str):
            body = manifest.resolve(body_path).read_bytes()
        elif isinstance(descriptor.get("body"), str):
            body = descriptor["body"].encode("utf-8")
        url = descriptor.get("url")
        if not isinstance(url, str):
            url = f"https://career.enbek.kz/ru/api/professionalstandart/{enbek_id}/download"
        headers: dict[str, str] = {}
        content_type = descriptor.get("content_type")
        if isinstance(content_type, str):
            headers["Content-Type"] = content_type
        transport.register(
            url,
            FakeResponse(
                int(descriptor.get("status", 200)),
                body=body,
                headers=headers,
                url=url,
                request_kind="pdf",
            ),
            kind="pdf",
        )
    return transport


def _invoke_pipeline(pipeline: Any, **available: Any) -> Any:
    target = pipeline.run if hasattr(pipeline, "run") and callable(pipeline.run) else pipeline
    if not callable(target):
        raise TypeError("pipeline must be callable or expose a callable run method")
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        return target(available["runtime"])
    accepts_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    if accepts_kwargs:
        return target(**available)
    selected = {
        name: value
        for name, value in available.items()
        if name in signature.parameters
    }
    if not selected and len(signature.parameters) == 1:
        return target(available["runtime"])
    return target(**selected)


def _load_manifest(value: FixtureManifest | str | Path | None) -> FixtureManifest:
    if isinstance(value, FixtureManifest):
        return value
    return load_manifest(value or DEFAULT_MANIFEST_PATH)


def _workspace_root(workspace: TemporaryWorkspace | str | Path) -> Path:
    if isinstance(workspace, TemporaryWorkspace):
        if workspace.root is None:
            with workspace:
                return workspace.path
        return workspace.path
    return Path(workspace).expanduser().resolve()


def _with_query(url: str, query: Mapping[str, Any]) -> str:
    parts = urlsplit(url)
    encoded = urlencode(query, doseq=True)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, encoded, parts.fragment))


def _string_sequence(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _string_mapping(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): item
        for key, item in value.items()
        if isinstance(item, str)
    }


__all__ = [
    "FixtureHarness",
    "FixturePipelineHarness",
    "FixtureRuntime",
    "HarnessResult",
    "build_fixture_transport",
    "run_fixture_pipeline",
]
