from __future__ import annotations

import json
from pathlib import Path

from enbek_monitor.network import NetworkClient
from enbek_monitor.storage.raw import RawArtifactStore
from enbek_monitor.vacancies.collector import VacancyCollector
from tests.support.fakes import FakeResponse, FakeTransport


SEARCH_URL = "https://api.hh.ru/vacancies"
DETAIL_ONE = "https://api.hh.ru/vacancies/101"
DETAIL_TWO = "https://api.hh.ru/vacancies/202"


def test_collector_collects_details_deduplicates_and_stores_hh_raw(tmp_path: Path) -> None:
    transport = FakeTransport(
        {
            SEARCH_URL: FakeResponse(
                json_data={
                    "items": [
                        {"id": "101", "url": DETAIL_ONE},
                        {"id": "101", "url": DETAIL_ONE},
                        {"id": "202", "url": DETAIL_TWO},
                    ]
                },
                headers={"Content-Type": "application/json"},
            ),
            DETAIL_ONE: FakeResponse(
                json_data={
                    "id": "101",
                    "name": "QA engineer",
                    "description": "<p>SQL &amp; API testing</p>",
                    "key_skills": [{"name": "SQL"}, {"name": "Postman"}],
                    "area": {"name": "Almaty"},
                },
                headers={"Content-Type": "application/json"},
            ),
            DETAIL_TWO: FakeResponse(
                json_data={
                    "id": "202",
                    "name": "Manual tester",
                    "description": "<p>Regression testing</p>",
                    "key_skills": [],
                },
                headers={"Content-Type": "application/json"},
            ),
        }
    )
    client = NetworkClient(transport, fixture_mode=True)
    result = VacancyCollector(client, RawArtifactStore(tmp_path)).collect(
        [{"url": SEARCH_URL, "params": {"text": "tester", "per_page": 20}}],
        run_id="weekly-1",
        fetched_at="2026-09-18T00:00:00Z",
    )

    assert result.success
    assert [record.vacancy_id for record in result.records] == ["101", "202"]
    assert result.records[0].description == "SQL & API testing"
    assert result.records[0].key_skills == ("SQL", "Postman")
    assert len(result.raw_artifacts) == 3
    assert all("raw/hh/" in artifact.physical_path for artifact in result.raw_artifacts)
    assert len(client.transport.request_log) == 3
    assert json.loads((tmp_path / result.raw_artifacts[0].physical_path).read_text())