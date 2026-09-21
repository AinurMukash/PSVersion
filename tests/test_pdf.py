"""Focused fixture-backed tests for the PDF collector."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from enbek_monitor.collectors.pdf import PdfCollectionResult, PdfCollector
from enbek_monitor.errors import SourceError
from enbek_monitor.network import NetworkClient
from enbek_monitor.storage.raw import RawArtifactStore
from tests.support.fakes import FakeNetworkError, FakeResponse, FakeSleeper, FakeClock, FakeTransport


FETCHED_AT = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def _build_collector(
    workspace: Path,
    *,
    url: str,
    body: bytes,
    status: int,
    content_type: str | None,
) -> tuple[PdfCollector, FakeTransport]:
    workspace.mkdir(parents=True, exist_ok=True)
    headers = {} if content_type is None else {"Content-Type": content_type}
    transport = FakeTransport(
        {url: FakeResponse(status, body=body, headers=headers, url=url)},
    )
    client = NetworkClient(transport, fixture_mode=True)
    return PdfCollector(client, RawArtifactStore(workspace)), transport


def test_valid_pdf_fixtures_are_stored_with_exact_bytes_and_stable_key(
    fixture_manifest,
    tmp_path: Path,
) -> None:
    valid_cases = fixture_manifest.get_cases("PDF_Document", group="valid")
    assert valid_cases

    for case in valid_cases:
        descriptor = case.load_json()
        body = (fixture_manifest.fixtures_root / descriptor["body_path"]).read_bytes()
        url = descriptor["url"]
        collector, transport = _build_collector(
            tmp_path / str(descriptor["enbek_id"]),
            url=url,
            body=body,
            status=descriptor["status"],
            content_type=descriptor["content_type"],
        )

        result = collector.collect(
            descriptor["enbek_id"],
            url,
            fetched_at=FETCHED_AT,
        )

        assert isinstance(result, PdfCollectionResult)
        assert result.success is True
        assert result.complete is True
        assert result.error is None
        assert result.artifact is not None
        assert result.artifact.logical_key == f"enbek-{descriptor['enbek_id']}"
        assert result.logical_key == f"enbek-{descriptor['enbek_id']}"
        assert result.artifact.enbek_id == descriptor["enbek_id"]
        assert result.artifact.byte_length == len(body)
        assert (collector.raw_store.guard.root / result.artifact.physical_path).read_bytes() == body
        assert len(transport.requests) == 1
        assert transport.requests[0].expected_kind == "pdf"


def test_invalid_pdf_fixtures_fail_before_publishing_valid_artifact(
    fixture_manifest,
    tmp_path: Path,
) -> None:
    invalid_cases = fixture_manifest.get_cases("PDF_Document", group="invalid")
    assert invalid_cases

    for case in invalid_cases:
        descriptor = case.load_json()
        body_path = descriptor.get("body_path")
        body = (
            (fixture_manifest.fixtures_root / body_path).read_bytes()
            if isinstance(body_path, str)
            else b""
        )
        url = descriptor["url"]
        collector, _ = _build_collector(
            tmp_path / case.id,
            url=url,
            body=body,
            status=descriptor["status"],
            content_type=descriptor.get("content_type"),
        )

        result = collector.collect(
            descriptor["enbek_id"],
            url,
            fetched_at=FETCHED_AT,
        )

        assert result.success is False
        assert result.complete is False
        assert result.incomplete is True
        assert result.artifact is None
        assert isinstance(result.error, SourceError)
        assert result.error.url == url
        assert result.error.reason
        assert not (collector.raw_store.guard.root / "raw" / "pdf").exists()
        assert not collector.raw_store.manifest_path.exists()


def test_network_no_response_returns_incomplete_source_error_without_artifact(
    tmp_path: Path,
) -> None:
    url = "https://career.enbek.kz/ru/api/professionalstandart/1999/download"
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    transport = FakeTransport({url: FakeNetworkError("connection reset")})
    collector = PdfCollector(
        NetworkClient(transport, sleeper=sleeper, fixture_mode=True),
        RawArtifactStore(tmp_path),
    )

    result = collector.collect(1999, url, fetched_at=FETCHED_AT)

    assert result.success is False
    assert result.complete is False
    assert result.artifact is None
    assert isinstance(result.error, SourceError)
    assert result.error.url == url
    assert result.error.attempts == 3
    assert result.error.reason == "connection reset"
    assert sleeper.delays == [2.0, 5.0]
    assert not collector.raw_store.manifest_path.exists()


def test_logical_key_is_stable_and_distinct_for_positive_ids() -> None:
    assert PdfCollector.logical_key_for(1) == "enbek-1"
    assert PdfCollector.logical_key_for(1) == PdfCollector.logical_key_for(1)
    assert PdfCollector.logical_key_for(1) != PdfCollector.logical_key_for(2)


@pytest.mark.parametrize("invalid_id", [0, -1, True, "1001"])
def test_invalid_id_is_rejected_before_network_request(
    invalid_id,
    tmp_path: Path,
) -> None:
    url = "https://career.enbek.kz/ru/api/professionalstandart/1001/download"
    transport = FakeTransport({url: FakeResponse(200, body=b"unused")})
    collector = PdfCollector(
        NetworkClient(transport, fixture_mode=True),
        RawArtifactStore(tmp_path),
    )

    with pytest.raises(ValueError, match="positive integer"):
        collector.collect(invalid_id, url)

    assert transport.requests == []
