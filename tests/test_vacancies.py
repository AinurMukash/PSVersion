from __future__ import annotations

import json
from pathlib import Path

from enbek_monitor.models import ArtifactKind
from enbek_monitor.network import NetworkClient
from enbek_monitor.storage.raw import RawArtifactStore
from enbek_monitor.vacancies import parsers
from enbek_monitor.vacancies.analytics import summarize
from enbek_monitor.vacancies.collector import VacancyCollector
from enbek_monitor.vacancies.history import diff_snapshots, load_previous, persist_snapshot
from enbek_monitor.vacancies.matching import match_records
from enbek_monitor.vacancies.models import VacancyRecord
from enbek_monitor.vacancies.pipeline import HH_KZ_AREA_ID
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


def test_hh_raw_store_uses_paginated_search_and_vacancy_detail_paths(tmp_path: Path) -> None:
    store = RawArtifactStore(tmp_path)
    search = store.store(
        ArtifactKind.HH_SEARCH,
        SEARCH_URL,
        b'{"items":[]}',
        content_type="application/json",
        run_id="weekly-1",
    )
    assert search.physical_path == "raw/hh/search/runs/weekly-1/page-000001.json"
    assert search.logical_key == "hh-search-weekly-1-page-000001"
    assert search.vacancy_id is None

    detail = store.store(
        ArtifactKind.HH_DETAIL,
        DETAIL_ONE,
        b'{"id":"101"}',
        vacancy_id="101",
        content_type="application/json",
        run_id="weekly-1",
    )
    assert detail.physical_path == "raw/hh/detail/runs/weekly-1/101.json"
    assert detail.vacancy_id == "101"


def test_collector_normalizes_salary_and_professional_roles(tmp_path: Path) -> None:
    transport = FakeTransport(
        {
            SEARCH_URL: FakeResponse(
                json_data={"items": [{"id": "101", "url": DETAIL_ONE}]},
                headers={"Content-Type": "application/json"},
            ),
            DETAIL_ONE: FakeResponse(
                json_data={
                    "id": "101",
                    "name": "QA engineer",
                    "salary": {"from": 100, "to": None, "currency": "kzt", "gross": False, "extra": "drop"},
                    "professional_roles": [{"name": "Tester"}, {"name": " "}, "bad"],
                    "key_skills": [],
                },
                headers={"Content-Type": "application/json"},
            ),
        }
    )
    client = NetworkClient(transport, fixture_mode=True)
    result = VacancyCollector(client, RawArtifactStore(tmp_path)).collect(
        [{"url": SEARCH_URL, "params": {"text": "tester"}}],
        run_id="weekly-1",
        fetched_at="2026-09-18T00:00:00Z",
    )

    assert result.success
    assert result.records[0].salary == {"from": 100, "currency": "KZT", "gross": False}
    assert result.records[0].requirements == ("Tester",)


def test_kz_area_is_default_for_weekly_snapshots() -> None:
    assert HH_KZ_AREA_ID == "40"


def _record(vacancy_id: str, **overrides) -> VacancyRecord:
    payload = {
        "vacancy_id": vacancy_id,
        "title": f"Title {vacancy_id}",
        "fetched_at": "2026-09-19T00:00:00Z",
    }
    payload.update(overrides)
    return VacancyRecord(**payload)


def test_parsers_reject_search_without_items() -> None:
    import pytest

    from enbek_monitor.errors import ParseError

    with pytest.raises(ParseError):
        parsers.parse_search({"found": 0}, url=SEARCH_URL)


def test_collector_stops_at_api_declared_last_page(tmp_path: Path) -> None:
    transport = FakeTransport(
        {
            SEARCH_URL: FakeResponse(
                json_data={
                    "items": [{"id": "101", "url": DETAIL_ONE}],
                    "found": 1,
                    "pages": 1,
                    "page": 0,
                    "per_page": 1,
                },
                headers={"Content-Type": "application/json"},
            ),
            DETAIL_ONE: FakeResponse(
                json_data={"id": "101", "name": "QA"},
                headers={"Content-Type": "application/json"},
            ),
        }
    )
    client = NetworkClient(transport, fixture_mode=True)
    result = VacancyCollector(client, RawArtifactStore(tmp_path)).collect(
        [{"url": SEARCH_URL, "params": {"text": "qa", "per_page": 1}}],
        run_id="run-pages",
        fetched_at="2026-09-19T00:00:00Z",
        max_pages=10,
    )
    assert result.success
    assert [record.vacancy_id for record in result.records] == ["101"]
    assert len(client.transport.request_log) == 2


def test_collector_enforces_limit_across_pages(tmp_path: Path) -> None:
    transport = FakeTransport(
        {
            SEARCH_URL: FakeResponse(
                json_data={
                    "items": [
                        {"id": "101", "url": DETAIL_ONE},
                        {"id": "202", "url": DETAIL_TWO},
                    ],
                    "found": 2,
                    "pages": 1,
                    "page": 0,
                    "per_page": 2,
                },
                headers={"Content-Type": "application/json"},
            ),
            DETAIL_ONE: FakeResponse(
                json_data={"id": "101", "name": "One"},
                headers={"Content-Type": "application/json"},
            ),
            DETAIL_TWO: FakeResponse(
                json_data={"id": "202", "name": "Two"},
                headers={"Content-Type": "application/json"},
            ),
        }
    )
    client = NetworkClient(transport, fixture_mode=True)
    result = VacancyCollector(client, RawArtifactStore(tmp_path)).collect(
        [{"url": SEARCH_URL, "params": {"text": "qa", "per_page": 2}}],
        run_id="run-limit",
        fetched_at="2026-09-19T00:00:00Z",
        max_pages=5,
        limit=1,
    )
    assert result.success
    assert [record.vacancy_id for record in result.records] == ["101"]


def test_collector_detail_error_keeps_search_complete(tmp_path: Path) -> None:
    from enbek_monitor.errors import SourceError

    transport = FakeTransport(
        {
            SEARCH_URL: FakeResponse(
                json_data={"items": [{"id": "101", "url": DETAIL_ONE}]},
                headers={"Content-Type": "application/json"},
            ),
            DETAIL_ONE: SourceError("boom", url=DETAIL_ONE, stage="hh detail"),
        }
    )
    client = NetworkClient(transport, fixture_mode=True)
    result = VacancyCollector(client, RawArtifactStore(tmp_path)).collect(
        [{"url": SEARCH_URL, "params": {"text": "qa"}}],
        run_id="run-partial",
        fetched_at="2026-09-19T00:00:00Z",
    )
    assert result.complete is True
    assert result.success is False
    assert result.partial is False
    assert result.records == ()
    assert len(result.errors) == 1


def test_history_diff_tracks_new_updated_removed() -> None:
    old = _record("101", title="Old title")
    previous = {old.vacancy_id: old.to_dict()}
    same = _record("101", title="Old title")
    same.__class__  # touch
    # Force identical hash by reusing stored payload hash semantics:
    assert same.content_sha256 == old.content_sha256
    changed = _record("101", title="New title")
    fresh = _record("202", title="Fresh")
    diff = diff_snapshots(previous, [changed, fresh], run_id="hh-1", detected_at="2026-09-19T00:00:00Z")
    assert diff["counts"] == {"new": 1, "updated": 1, "removed": 0, "unchanged": 0, "total": 2}
    assert diff["updated"][0]["changed_fields"] == ["title"]

    diff2 = diff_snapshots(previous, [], run_id="hh-2", detected_at="2026-09-19T00:00:00Z")
    assert diff2["counts"]["removed"] == 1
    assert diff2["removed"][0]["vacancy_id"] == "101"


def test_history_persist_writes_current_and_appends_changes(tmp_path: Path) -> None:
    data_dir = tmp_path / "data" / "vacancies"
    first = [_record("101", title="One")]
    changes = diff_snapshots({}, first, run_id="hh-1", detected_at="2026-09-19T00:00:00Z")
    persist_snapshot(data_dir, first, changes)
    assert (data_dir / "current" / "101.json").exists()
    assert (data_dir / "changes.jsonl").exists()

    previous = load_previous(data_dir / "current")
    second = [_record("202", title="Two")]
    changes2 = diff_snapshots(previous, second, run_id="hh-2", detected_at="2026-09-20T00:00:00Z")
    persist_snapshot(data_dir, second, changes2)
    assert not (data_dir / "current" / "101.json").exists()
    assert (data_dir / "current" / "202.json").exists()
    lines = (data_dir / "changes.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3  # NEW 101, NEW 202, REMOVED 101


def test_summary_includes_area_and_employer_breakdowns() -> None:
    records = [
        _record("1", area="Almaty", employer="Acme", key_skills=("SQL",)),
        _record("2", area="Almaty", employer="Acme", key_skills=("SQL",)),
    ]
    summary = summarize(records)
    assert summary["vacancies"] == 2
    assert summary["areas"][0] == {"name": "Almaty", "count": 2, "share": 1.0}
    assert summary["employers"][0]["name"] == "Acme"
    assert summary["skills"][0]["name"] == "sql"


def test_matching_uses_skills_and_precomputed_standard_tokens(tmp_path: Path) -> None:
    standards = tmp_path / "standards"
    standards.mkdir()
    (standards / "1.json").write_text(
        json.dumps({"enbek_id": 1, "title": "Software testing", "professions": [{"name": "Test engineer"}]}),
        encoding="utf-8",
    )
    record = _record("101", title="Unrelated", key_skills=("Testing",))
    matches = match_records([record], standards)
    assert len(matches) == 1
    assert matches[0]["standard_id"] == 1