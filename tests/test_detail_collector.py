"""Focused local tests for the detail-page collector."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from enbek_monitor.collectors.detail import DetailCollectionResult, DetailCollector
from enbek_monitor.errors import ParseError, SourceError
from enbek_monitor.models import RegistryEntry
from enbek_monitor.network import NetworkClient
from enbek_monitor.storage.raw import RawArtifactStore
from tests.support.fakes import FakeNetworkError, FakeResponse, FakeTransport


FIXTURES = Path(__file__).parents[1] / "fixtures" / "detail"
FETCHED_AT = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def _entry(enbek_id: int = 1001) -> RegistryEntry:
    return RegistryEntry(
        enbek_id=enbek_id,
        detail_page_url=f"https://career.enbek.kz/ru/professionalstandart/{enbek_id}",
        business_code=f"D3522000{enbek_id - 1000}",
    )


def _collector(
    workspace: Path,
    url: str,
    response: FakeResponse | BaseException,
    *,
    parser_mode: str | None = "json_api",
) -> tuple[DetailCollector, FakeTransport]:
    transport = FakeTransport({url: response})
    client = NetworkClient(transport, fixture_mode=True)
    collector = DetailCollector(
        client,
        RawArtifactStore(workspace),
        parser_mode=parser_mode,
    )
    return collector, transport


def test_valid_json_detail_is_stored_and_linked_to_registry_entry(tmp_path: Path) -> None:
    entry = _entry()
    body = (FIXTURES / "valid" / "standard-1001.json").read_bytes()
    collector, transport = _collector(
        tmp_path,
        entry.detail_page_url,
        FakeResponse(
            200,
            body=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            url=entry.detail_page_url,
            request_kind="detail",
        ),
    )

    result = collector.collect(entry, run_id="run-detail", fetched_at=FETCHED_AT)

    assert isinstance(result, DetailCollectionResult)
    assert result.success is True
    assert result.complete is True
    assert result.error is None
    assert result.enbek_id == entry.enbek_id
    assert result.page_url == entry.detail_page_url
    assert result.attributes is not None
    assert result.attributes.enbek_id == entry.enbek_id
    assert result.attributes.page_url == entry.detail_page_url
    assert result.attributes.pdf_url is not None
    assert result.artifact is not None
    assert result.artifact.enbek_id == entry.enbek_id
    assert (tmp_path / result.artifact.physical_path).read_bytes() == body
    assert len(transport.requests) == 1
    assert transport.requests[0].url == entry.detail_page_url
    assert transport.requests[0].expected_kind == "detail"


def test_parse_error_returns_incomplete_without_attributes(tmp_path: Path) -> None:
    entry = _entry()
    collector, transport = _collector(
        tmp_path,
        entry.detail_page_url,
        FakeResponse(
            200,
            body=(FIXTURES / "invalid" / "malformed.json").read_bytes(),
            headers={"Content-Type": "application/json"},
            url=entry.detail_page_url,
            request_kind="detail",
        ),
    )

    result = collector.collect(entry, run_id="run-parse", fetched_at=FETCHED_AT)

    assert result.success is False
    assert result.complete is False
    assert result.attributes is None
    assert result.detail_page is None
    assert isinstance(result.error, ParseError)
    assert result.error.url == entry.detail_page_url
    assert result.error.reason
    # The successful HTTP response remains available as an immutable raw input
    # for diagnosis, but it is never exposed as parsed normalizer input.
    assert result.artifact is not None
    assert result.artifact.enbek_id == entry.enbek_id
    assert len(transport.requests) == 1


def test_source_error_returns_incomplete_without_raw_artifact(tmp_path: Path) -> None:
    entry = _entry()
    collector, transport = _collector(
        tmp_path,
        entry.detail_page_url,
        FakeNetworkError("detail connection reset"),
    )

    result = collector.collect(entry, fetched_at=FETCHED_AT)

    assert result.success is False
    assert result.complete is False
    assert result.attributes is None
    assert result.artifact is None
    assert isinstance(result.error, SourceError)
    assert result.error.url == entry.detail_page_url
    assert result.error.attempts == 3
    assert len(transport.requests) == 3
    assert not collector.raw_store.manifest_path.exists()


def test_html_profile_selects_html_parser_and_never_requests_pdf(tmp_path: Path) -> None:
    entry = _entry(1005)
    body = (FIXTURES / "valid" / "standard-1005.html").read_bytes()
    transport = FakeTransport(
        {
            entry.detail_page_url: FakeResponse(
                200,
                body=body,
                headers={"Content-Type": "text/html"},
                url=entry.detail_page_url,
                request_kind="detail",
            ),
            "https://career.enbek.kz/ru/api/professionalstandart/1005/download": FakeResponse(
                200,
                body=b"must not be requested",
                headers={"Content-Type": "application/pdf"},
                request_kind="pdf",
            ),
        }
    )
    collector = DetailCollector(
        NetworkClient(transport, fixture_mode=True),
        RawArtifactStore(tmp_path),
        parser_mode="html",
    )

    result = collector.collect(entry, fetched_at=FETCHED_AT)

    assert result.success is True
    assert result.attributes is not None
    assert result.attributes.title == "Специалист по складской логистике"
    assert [request.expected_kind for request in transport.requests] == ["detail"]
    assert not (tmp_path / "raw" / "pdf").exists()


@pytest.mark.parametrize("invalid_entry", [None, {"enbek_id": 1001}])
def test_only_validated_registry_entries_are_accepted(
    invalid_entry,
    tmp_path: Path,
) -> None:
    transport = FakeTransport({})
    collector = DetailCollector(
        NetworkClient(transport, fixture_mode=True),
        RawArtifactStore(tmp_path),
    )

    with pytest.raises(TypeError, match="RegistryEntry"):
        collector.collect(invalid_entry)
    assert transport.requests == []
