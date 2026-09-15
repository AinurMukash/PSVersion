"""Fixture-backed traversal tests for the paginated registry collector."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from enbek_monitor.collectors.registry import RegistryCollector
from enbek_monitor.errors import ParseError, SourceError
from enbek_monitor.network import NetworkClient
from enbek_monitor.source_discovery import SourceProfile
from enbek_monitor.storage.raw import RawArtifactStore
from tests.support.fakes import FakeClock, FakeResponse, FakeSleeper, FakeTransport
from tests.support.harness import build_fixture_transport


REGISTRY_URL = "https://career.enbek.kz/ru/ps-registry"


def _fallback_profile(fixture_manifest) -> SourceProfile:
    path = fixture_manifest.fixtures_root / "profiles" / "fallback.json"
    return SourceProfile.from_dict(json.loads(path.read_text(encoding="utf-8")))


def _collector(profile: SourceProfile, transport: FakeTransport, workspace: Path) -> RegistryCollector:
    client_clock = FakeClock()
    client = NetworkClient(
        transport,
        sleeper=FakeSleeper(client_clock),
        fixture_mode=True,
    )
    return RegistryCollector(client, RawArtifactStore(workspace), profile=profile)


def test_fallback_two_page_registry_is_complete_and_raw_is_staged(
    fixture_manifest,
    tmp_path: Path,
) -> None:
    profile = _fallback_profile(fixture_manifest)
    transport = build_fixture_transport(fixture_manifest, profile)
    result = _collector(profile, transport, tmp_path).collect(run_id="run-fallback")

    assert result.complete is True
    assert result.success is True
    assert result.eligible_for_lifecycle is True
    assert [entry.enbek_id for entry in result.entries] == [1001, 1002, 1003, 1004]
    assert result.requested_urls == (
        REGISTRY_URL,
        "fixture://enbek/registry/page-002.json",
    )
    assert len(result.pages) == 2
    assert len(result.raw_artifacts) == 2
    assert all((tmp_path / artifact.physical_path).is_file() for artifact in result.raw_artifacts)
    assert [request.params for request in transport.request_log] == [
        {"page": 1, "page_size": 2},
        {"page": 1, "page_size": 2},
    ]


def test_terminal_page_stops_without_requesting_another_page(
    fixture_manifest,
    tmp_path: Path,
) -> None:
    profile = replace(
        _fallback_profile(fixture_manifest),
        registry_url=REGISTRY_URL,
        query={"page": 1, "page_size": 1},
    )
    body = json.dumps(
        {
            "data": {
                "items": [
                    {
                        "enbek_id": 7,
                        "detail_url": "https://career.enbek.kz/standard/7",
                        "code": "C7",
                    }
                ],
                "next": None,
                "page": 1,
                "page_size": 1,
                "total": 1,
            }
        },
        ensure_ascii=False,
    ).encode("utf-8")
    transport = FakeTransport(
        {
            REGISTRY_URL: FakeResponse(
                200,
                body=body,
                headers={"Content-Type": "application/json"},
                url=REGISTRY_URL,
            )
        }
    )

    result = _collector(profile, transport, tmp_path).collect()

    assert result.complete is True
    assert len(transport.request_log) == 1
    assert result.pages[0].pagination.terminal is True


def test_failed_mandatory_page_is_incomplete_and_has_no_lifecycle_eligibility(
    fixture_manifest,
    tmp_path: Path,
) -> None:
    profile = _fallback_profile(fixture_manifest)
    transport = FakeTransport(
        {
            profile.registry_url: FakeResponse(
                503,
                body=b"temporary outage",
                headers={"Content-Type": "application/json"},
                url=profile.registry_url,
            )
        }
    )

    result = _collector(profile, transport, tmp_path).collect(run_id="run-failed")

    assert result.complete is False
    assert result.success is False
    assert result.eligible_for_lifecycle is False
    assert result.entries == ()
    assert len(result.errors) == 1
    assert isinstance(result.errors[0], SourceError)
    assert result.errors[0].url == profile.registry_url
    assert result.errors[0].attempts == 3
    assert not (tmp_path / "raw" / "manifest.jsonl").exists()


def test_repeated_next_url_is_parse_error_and_does_not_loop(
    fixture_manifest,
    tmp_path: Path,
) -> None:
    url = "fixture://enbek/registry/loop.json"
    body = json.dumps(
        {
            "data": {
                "items": [
                    {
                        "enbek_id": 8,
                        "detail_url": "https://career.enbek.kz/standard/8",
                    }
                ],
                "next": url,
                "page": 1,
                "page_size": 1,
                "total": 1,
            }
        }
    ).encode("utf-8")
    profile = replace(
        _fallback_profile(fixture_manifest),
        registry_url=url,
        query={},
    )
    transport = FakeTransport(
        {url: FakeResponse(200, body=body, headers={"Content-Type": "application/json"}, url=url)}
    )

    result = _collector(profile, transport, tmp_path).collect()

    assert result.complete is False
    assert result.eligible_for_lifecycle is False
    assert len(transport.request_log) == 1
    assert isinstance(result.error, ParseError)
    assert "cycle" in result.error.reason or "repeated" in result.error.reason
    assert len(result.pages) == 1
    assert len(result.raw_artifacts) == 1
