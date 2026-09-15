"""Fixture-backed integration coverage for the detail collection boundary."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from enbek_monitor.collectors.detail import DetailCollector
from enbek_monitor.models import RegistryEntry
from enbek_monitor.network import NetworkClient
from enbek_monitor.storage.raw import RawArtifactStore
from tests.support.fakes import FakeResponse, FakeTransport


FIXTURES = Path(__file__).parents[1] / "fixtures" / "detail"
FETCHED_AT = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def test_fixture_detail_pipeline_persists_raw_and_returns_complete_attributes(
    tmp_path: Path,
) -> None:
    entry = RegistryEntry(
        enbek_id=1001,
        detail_page_url="https://career.enbek.kz/ru/professionalstandart/1001",
        business_code="D35220001",
    )
    body = (FIXTURES / "valid" / "standard-1001.json").read_bytes()
    transport = FakeTransport(
        {
            entry.detail_page_url: FakeResponse(
                200,
                body=body,
                headers={"Content-Type": "application/json"},
                url=entry.detail_page_url,
                request_kind="detail",
            )
        }
    )
    collector = DetailCollector(
        NetworkClient(transport, fixture_mode=True),
        RawArtifactStore(tmp_path),
        parser_mode="json_api",
        run_id="integration-run",
    )

    result = collector.collect(entry, fetched_at=FETCHED_AT)

    assert result.success is True
    assert result.complete is True
    assert result.attributes is not None
    assert result.attributes.enbek_id == entry.enbek_id
    assert result.attributes.page_url == entry.detail_page_url
    assert result.artifact is not None
    assert result.artifact.physical_path.startswith("raw/detail/runs/integration-run/")
    assert (tmp_path / result.artifact.physical_path).read_bytes() == body
    assert len(transport.requests) == 1
    assert transport.requests[0].url == entry.detail_page_url
    assert transport.requests[0].expected_kind == "detail"
