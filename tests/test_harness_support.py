"""Narrow checks for the local-only fixture harness boundary."""

from __future__ import annotations

import socket
from urllib.request import urlopen

import pytest

from tests.support.harness import FixturePipelineHarness
from tests.support.network_guard import OutboundNetworkError, no_network
from tests.support.workspace import TemporaryWorkspace


def test_no_network_guard_blocks_dns_and_http_entry_points() -> None:
    with no_network():
        with pytest.raises(OutboundNetworkError):
            socket.getaddrinfo("example.invalid", 443)
        with pytest.raises(OutboundNetworkError):
            urlopen("http://example.invalid")


def test_fixture_harness_runs_fake_transport_without_outbound_network() -> None:
    with TemporaryWorkspace() as workspace:
        runner = FixturePipelineHarness(workspace)
        result = runner.run(
            lambda runtime, **_: runtime.transport.request(
                runtime.profile.registry_url,
                expected_kind="registry",
            )
        )

        assert result.error is None
        assert result.value.status_code == 200
        assert result.value.content.startswith(b"{")
        assert result.runtime.transport.peak_active_requests == 1
        assert result.artifacts_unchanged


def test_fixture_harness_captures_published_bytes_for_failed_run() -> None:
    with TemporaryWorkspace() as workspace:
        changes = workspace.path / "history" / "changes.jsonl"
        changes.write_bytes(b'{"event_id":"old"}\n')
        before = workspace.capture()
        runner = FixturePipelineHarness(workspace)

        def failed_pipeline(runtime, **_):
            assert runtime.workspace.path == workspace.path
            raise RuntimeError("fixture collection failed")

        result = runner.run(failed_pipeline)

        assert isinstance(result.error, RuntimeError)
        assert result.before.files == before.files
        assert result.artifacts_unchanged
        workspace.assert_unchanged(before)


def test_fixture_harness_reports_published_change_after_callback() -> None:
    with TemporaryWorkspace() as workspace:
        runner = FixturePipelineHarness(workspace)

        def callback(runtime, **_):
            (runtime.workspace.path / "data" / "snapshots" ).mkdir()
            (runtime.workspace.path / "data" / "snapshots" / "current.json").write_bytes(b"new")
            return "ok"

        result = runner.run(callback)

        assert result.error is None
        assert result.value == "ok"
        assert not result.artifacts_unchanged
        assert result.after["data/snapshots/current.json"] == b"new"


def test_no_network_guard_blocks_http_client_and_imported_urlopen_alias() -> None:
    import http.client

    with no_network():
        with pytest.raises(OutboundNetworkError, match="outbound network access"):
            http.client.HTTPConnection("example.invalid").request("GET", "/")
        # ``urlopen`` was imported before the guard in this module.  The
        # opener-level patch must still make that alias fail explicitly.
        with pytest.raises(OutboundNetworkError, match="outbound network access"):
            urlopen("http://example.invalid")


def test_temporary_workspace_isolated_and_caller_path_is_not_removed(tmp_path) -> None:
    supplied = tmp_path / "workspace"
    with TemporaryWorkspace(supplied) as workspace:
        assert workspace.path == supplied.resolve()
        assert {name for name in ("raw", "data", "history", "docs", "work") if (workspace.path / name).is_dir()} == {
            "raw",
            "data",
            "history",
            "docs",
            "work",
        }
        (workspace.path / "history" / "changes.jsonl").write_bytes(b"existing\n")
    assert supplied.exists()
    assert (supplied / "history" / "changes.jsonl").read_bytes() == b"existing\n"


def test_fixture_harness_dry_run_preserves_published_bytes_and_exposes_mode() -> None:
    with TemporaryWorkspace() as workspace:
        (workspace.path / "history" / "changes.jsonl").write_bytes(b"old-history\n")
        (workspace.path / "data" / "snapshots").mkdir()
        (workspace.path / "data" / "snapshots" / "current.json").write_bytes(b"old-current")
        (workspace.path / "README.md").write_bytes(b"old-readme\n")
        (workspace.path / "CHANGELOG.md").write_bytes(b"old-changelog\n")
        runner = FixturePipelineHarness(workspace)

        def dry_run_pipeline(runtime, mode, **_):
            assert mode == "dry_run"
            return runtime.transport.request(
                runtime.profile.registry_url,
                expected_kind="registry",
            )

        result = runner.run(dry_run_pipeline, mode="dry_run")

        assert result.error is None
        assert result.value.status_code == 200
        result.assert_unchanged()
