"""Focused local tests for ConfigLoader and safe source configuration."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from enbek_monitor.config import ConfigError, ConfigLoader


REGISTRY_URL = "https://career.enbek.kz/ru/ps-registry?captured=exact"


def make_profile(*, kind: str = "fixture", confirmed: bool = False) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "kind": kind,
        "confirmed": confirmed,
        "parser_mode": "json_api",
        "registry": {
            "url": REGISTRY_URL,
            "method": "GET",
            "query": {"page": 1, "page_size": 2},
            "body": None,
            "headers": {"Accept": "application/json"},
        },
        "pagination": {
            "records_selector": {"type": "json_path", "value": "$.data.items"},
            "next_selector": {"type": "json_path", "value": "$.data.next"},
            "terminal_condition": "null_or_missing",
        },
        "detail_link_selector": {"type": "json_path", "value": "$.detail_url"},
        "pdf_link_selector": {"type": "json_path", "value": "$.pdf_url"},
    }


def test_defaults_and_exact_registry_url_are_available_in_run_context(tmp_path: Path) -> None:
    config = ConfigLoader(
        workspace=tmp_path,
        config={
            "source": {"transport": "fixture", "profile": make_profile()},
        },
    ).load()

    assert config.workspace == tmp_path.resolve()
    assert config.network.max_attempts == 3
    assert config.network.retry_delays == (2.0, 5.0)
    assert config.network.max_concurrency == 5
    assert config.validation.pdf_min_bytes == 1024
    assert config.validation.anomaly_threshold == 0.30
    assert config.storage.to_dict() == {
        "raw_dir": "raw",
        "data_dir": "data",
        "history_dir": "history",
        "docs_dir": "docs",
        "work_dir": "work",
    }

    context = config.create_run_context(
        "dry_run",
        now=datetime(2025, 1, 2, 3, 4, 5, 900000, tzinfo=timezone.utc),
        run_id="20250102T030405Z-test",
    )
    assert config.registry_url == REGISTRY_URL
    assert context.registry_url == REGISTRY_URL
    assert context.exact_registry_url == REGISTRY_URL
    assert context.started_at_utc == "2025-01-02T03:04:05Z"
    assert context.mode == "dry_run"
    assert context.staging_root == tmp_path.resolve() / "work" / "runs" / context.run_id


def test_fixture_transport_can_be_selected_from_local_json_profile(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(make_profile()), encoding="utf-8")
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "workspace": str(tmp_path),
                "source": {"transport": "fixture", "profile": "profile.json"},
            }
        ),
        encoding="utf-8",
    )

    config = ConfigLoader(config_path).load()

    assert config.source.transport == "fixture"
    assert config.source.is_fixture
    assert config.source.profile_path == profile_path.resolve()
    assert config.source.profile.registry_url == REGISTRY_URL


def test_toml_configuration_is_supported(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(make_profile()), encoding="utf-8")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'''workspace = "{tmp_path.as_posix()}"\n\n[source]\ntransport = "fixture"\nprofile = "profile.json"\n\n[network]\nmax_attempts = 3\nretry_delays = [2, 5]\nmax_concurrency = 5\n\n[validation]\npdf_min_bytes = 1024\nanomaly_threshold = 0.30\n''',
        encoding="utf-8",
    )

    config = ConfigLoader(config_path).load()

    assert config.source.is_fixture
    assert config.network.retry_delays == (2.0, 5.0)
    assert config.validation.anomaly_threshold == 0.30


def test_forbidden_remote_or_credential_field_is_structured_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as raised:
        ConfigLoader(
            workspace=tmp_path,
            config={
                "github": {"repository": "not-used"},
                "source": {"transport": "fixture", "profile": make_profile()},
            },
        ).load()

    error = raised.value
    assert error.stage == "configuration"
    assert "github" in error.invalid_fields[0]
    payload = error.to_report_dict()
    assert payload["error_type"] == "ConfigError"
    assert payload["reason"]
    assert payload["stage"] == "configuration"


def test_path_outside_workspace_is_rejected_before_profile_loading(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="outside Local_Workspace"):
        ConfigLoader(
            workspace=tmp_path,
            config={
                "source": {
                    "transport": "fixture",
                    "profile": "../profile.json",
                },
            },
        ).load()


def test_missing_source_profile_is_rejected_without_network_access(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as raised:
        ConfigLoader(workspace=tmp_path, config={}).load()

    assert raised.value.path == "$.source"
    assert raised.value.to_report_dict()["url"] is None
    assert raised.value.to_report_dict()["stage"] == "configuration"


def test_explicit_workspace_must_not_conflict_with_config_workspace(tmp_path: Path) -> None:
    other = tmp_path / "other"
    with pytest.raises(ConfigError, match="conflicts"):
        ConfigLoader(
            workspace=tmp_path,
            config={
                "workspace": str(other),
                "source": {"transport": "fixture", "profile": make_profile()},
            },
        ).load()
