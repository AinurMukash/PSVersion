"""Fixture-backed unit tests for pure canonical normalization."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from enbek_monitor.errors import NormalizationError
from enbek_monitor.models import (
    LifecycleState,
    OkedAttributes,
    ProfessionRecord,
    RegistryAttributes,
    RegistryEntry,
    SourceAttributes,
)
from enbek_monitor.normalize import Normalizer, OfficialCategorizer
from enbek_monitor.parsers.detail_json import DetailJsonParser
from enbek_monitor.network import SourceResponse


FIXTURES = Path(__file__).parents[1] / "fixtures"
REGISTRY_URL = "https://career.enbek.kz/ru/ps-registry"


def _detail(enbek_id: int) -> SourceAttributes:
    path = FIXTURES / "detail" / "valid" / f"standard-{enbek_id}.json"
    url = f"https://career.enbek.kz/ru/professionalstandart/{enbek_id}"
    response = SourceResponse(
        url=url,
        status_code=200,
        headers={"Content-Type": "application/json"},
        content=path.read_bytes(),
    )
    result = DetailJsonParser().parse(url, response)
    assert not isinstance(result, NormalizationError)
    return result.attributes


def _registry(enbek_id: int, code: str | None = None) -> RegistryAttributes:
    return RegistryAttributes(
        enbek_id=enbek_id,
        detail_page_url=f"https://career.enbek.kz/ru/professionalstandart/{enbek_id}",
        registry_url=REGISTRY_URL,
        code=code,
    )


def _registry_entry(enbek_id: int, code: str | None = None) -> RegistryEntry:
    return RegistryEntry(
        enbek_id=enbek_id,
        detail_page_url=f"https://career.enbek.kz/ru/professionalstandart/{enbek_id}",
        business_code=code,
    )


def test_normalizer_matches_valid_fixture_shape_and_exact_source_urls() -> None:
    detail = _detail(1001)
    pdf_bytes = (FIXTURES / "pdf" / "valid" / "standard-1001.pdf").read_bytes()
    record = Normalizer().normalize(
        _registry(1001, "D35220001"),
        detail,
        pdf_descriptor={"enbek_id": 1001, "content": pdf_bytes},
        lifecycle_context={"state": "ACTIVE"},
    )

    assert not isinstance(record, NormalizationError)
    assert record.schema_version == "1"
    assert record.enbek_id == 1001
    assert record.description == "Организация безопасной эксплуатации газораспределительных систем."
    assert record.categories.official.oked == ["D", "35", "35.2", "35.22", "35.22.0"]
    assert record.categories.official.ork_levels == [5, 6]
    assert record.source.to_dict() == {
        "registry_url": REGISTRY_URL,
        "page_url": "https://career.enbek.kz/ru/professionalstandart/1001",
        "pdf_url": "https://career.enbek.kz/ru/api/professionalstandart/1001/download",
    }
    assert record.hashes.pdf_sha256 == hashlib.sha256(pdf_bytes).hexdigest()
    assert record.hashes.metadata_sha256 is None
    assert record.hashes.normalized_sha256 is None


def test_normalizer_applies_html_unicode_date_number_and_missing_collection_rules() -> None:
    detail = _detail(1004)
    record = Normalizer().normalize(
        # Registry code is the source-of-truth code and is preserved as the
        # exact published value rather than inferred from another field.
        _registry(1004, "C10110004"),
        detail,
        lifecycle_context={"state": LifecycleState.ACTIVE},
    )

    assert not isinstance(record, NormalizationError)
    assert record.title == "Оператор технологической линии"
    assert record.description == "Управление линией и контроль качества."
    assert record.revision_date == "2024-02-29"
    assert record.version == 1
    assert record.professions == [ProfessionRecord("Оператор технологической линии", 4)]
    assert record.timestamps.to_dict() == {
        "first_seen_at": None,
        "last_seen_at": None,
        "last_changed_at": None,
    }

    empty = Normalizer().normalize(_registry(1003, "C10110003"), _detail(1003))
    assert not isinstance(empty, NormalizationError)
    assert empty.oked is None
    assert empty.professions == []
    assert empty.categories.official.oked == []


def test_normalizer_accepts_registry_entry_without_inventing_registry_url() -> None:
    record = Normalizer().normalize(_registry_entry(1003, "C10110003"), _detail(1003))

    assert not isinstance(record, NormalizationError)
    assert record.source.registry_url is None
    assert record.source.page_url == "https://career.enbek.kz/ru/professionalstandart/1003"


    result = Normalizer().normalize(_registry(1001), _detail(1002))

    assert isinstance(result, NormalizationError)
    assert "mismatch" in result.reason.lower()
    assert result.stage == "normalization"
    assert result.path == "$"
    assert not hasattr(result, "record")


def test_normalizer_accepts_detail_page_and_pdf_hash_descriptor() -> None:
    detail_id = 1002
    detail = _detail(detail_id)
    detail_page_url = f"https://career.enbek.kz/ru/professionalstandart/{detail_id}"
    from enbek_monitor.parsers.source_schema import DetailPage, ResponseFormat

    page = DetailPage(
        source_url=detail_page_url,
        attributes=detail,
        response_format=ResponseFormat.JSON,
        pdf_published=True,
    )
    digest = "A" * 64
    record = Normalizer().normalize(
        _registry(detail_id, "00d35220002"),
        page,
        pdf_descriptor={"enbek_id": detail_id, "pdf_sha256": digest},
    )

    assert not isinstance(record, NormalizationError)
    assert record.hashes.pdf_sha256 == digest.lower()
    assert record.categories.official.government_body is None
    assert record.categories.official.version is None
    assert record.categories.official.lifecycle_state is LifecycleState.ACTIVE


def test_official_categorizer_uses_only_published_source_fields() -> None:
    source = SourceAttributes(
        enbek_id=77,
        title="<b>Title</b>",
        government_body=" <i>Agency</i> ",
        oked=OkedAttributes(section_code="Z", division_code="01"),
        professions=[ProfessionRecord("Role", 6), ProfessionRecord("Other", 4)],
        year=2024,
        version=2.0,
    )

    category = OfficialCategorizer().categorize(source, lifecycle_state="MISSING")

    assert category.to_dict() == {
        "oked": ["Z", "01"],
        "government_body": "Agency",
        "ork_levels": [4, 6],
        "year": 2024,
        "version": 2,
        "lifecycle_state": "MISSING",
    }
    assert "Title" not in category.to_dict()
