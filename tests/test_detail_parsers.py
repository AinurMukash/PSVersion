"""Unit coverage for pure JSON and HTML detail source adapters."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from enbek_monitor.errors import ParseError
from enbek_monitor.models import SourceAttributes
from enbek_monitor.network import SourceResponse
from enbek_monitor.parsers.detail_html import DetailHtmlParser
from enbek_monitor.parsers.detail_json import DetailJsonParser
from enbek_monitor.parsers.source_schema import ResponseFormat


FIXTURES = Path(__file__).parents[1] / "fixtures" / "detail"
BASE_URL = "https://career.enbek.kz/ru/professionalstandart/1001"


def source_response(path: Path, content_type: str = "application/json") -> SourceResponse:
    return SourceResponse(
        url=BASE_URL,
        status_code=200,
        headers={"Content-Type": content_type},
        content=path.read_bytes(),
    )


def test_json_parser_accepts_all_valid_detail_fixtures() -> None:
    parser = DetailJsonParser()
    for path in sorted((FIXTURES / "valid").glob("*.json")):
        result = parser.parse(BASE_URL, source_response(path))
        assert isinstance(result, object)
        assert not isinstance(result, ParseError), path
        assert result.response_format is ResponseFormat.JSON
        assert result.attributes.page_url == BASE_URL
        assert result.attributes.enbek_id > 0
        assert result.attributes.title


def test_json_parser_preserves_null_scalars_and_maps_null_collections() -> None:
    result = DetailJsonParser().parse(
        "https://career.enbek.kz/ru/professionalstandart/1002",
        source_response(FIXTURES / "valid" / "standard-1002.json"),
    )
    assert isinstance(result, object) and not isinstance(result, ParseError)
    assert result.attributes.government_body is None
    assert result.attributes.description is None
    assert result.attributes.version is None
    assert result.attributes.revision_date is None
    assert result.attributes.professions == []
    assert result.attributes.pdf_url.endswith("/1002/download")


def test_json_parser_distinguishes_optional_empty_values_from_wrong_types() -> None:
    payload = {
        "data": {
            "enbek_id": 2201,
            "title": "Стандарт без опубликованного PDF",
            "code": "",
            "government_body": None,
            "description": "",
            "revision_date": "",
            "oked": {},
            "professions": [],
            "pdf_published": False,
            "pdf_url": None,
        }
    }
    url = "https://career.enbek.kz/ru/professionalstandart/2201"
    result = DetailJsonParser().parse(
        url,
        SourceResponse(
            url=url,
            status_code=200,
            headers={"Content-Type": "application/json"},
            content=json.dumps(payload).encode("utf-8"),
        ),
    )
    assert not isinstance(result, ParseError)
    assert result.attributes.code == ""
    assert result.attributes.description == ""
    assert result.attributes.government_body is None
    assert result.attributes.revision_date is None
    assert result.attributes.oked is not None
    assert result.attributes.professions == []
    assert result.attributes.pdf_url is None


def test_json_parser_reports_all_wrong_type_paths_without_partial_output() -> None:
    url = "https://career.enbek.kz/ru/professionalstandart/2103"
    result = DetailJsonParser().parse(
        url,
        source_response(FIXTURES / "invalid" / "wrong-types.json"),
    )
    assert isinstance(result, ParseError)
    assert result.url == url
    assert {
        "$.data.enbek_id",
        "$.data.government_body",
        "$.data.description",
        "$.data.version",
        "$.data.year",
        "$.data.revision_date",
        "$.data.oked",
        "$.data.professions",
    }.issubset(set(result.invalid_fields))
    assert not hasattr(result, "attributes")


@pytest.mark.parametrize(
    ("fixture_name", "expected_path"),
    [
        ("missing-enbek-id.json", "$.data.enbek_id"),
        ("missing-title.json", "$.data.title"),
        ("relative-pdf-url.json", "$.data.pdf_url"),
    ],
)
def test_json_parser_rejects_missing_required_or_invalid_pdf_fields(
    fixture_name: str, expected_path: str
) -> None:
    result = DetailJsonParser().parse(
        BASE_URL,
        source_response(FIXTURES / "invalid" / fixture_name),
    )
    assert isinstance(result, ParseError)
    assert expected_path in result.invalid_fields or expected_path in result.missing_fields
    assert result.url == BASE_URL


def test_json_parser_returns_parse_error_for_malformed_json() -> None:
    result = DetailJsonParser().parse(
        BASE_URL,
        source_response(FIXTURES / "invalid" / "malformed.json"),
    )
    assert isinstance(result, ParseError)
    assert result.path == "$"
    assert result.invalid_fields == ("$",)


def test_html_parser_uses_data_attributes_and_data_fields() -> None:
    url = "https://career.enbek.kz/ru/professionalstandart/1005"
    response = SourceResponse(
        url=url,
        status_code=200,
        headers={"Content-Type": "text/html"},
        content=(FIXTURES / "valid" / "standard-1005.html").read_bytes(),
    )
    result = DetailHtmlParser().parse(url, response)
    assert not isinstance(result, ParseError)
    assert result.response_format is ResponseFormat.HTML
    assert result.attributes.enbek_id == 1005
    assert result.attributes.code == "F43210005"
    assert result.attributes.description == "Организация складских операций."
    assert result.attributes.version == 1
    assert result.attributes.year == 2022
    assert result.attributes.oked.section_code == "F"
    assert [profession.name for profession in result.attributes.professions] == [
        "Кладовщик",
        "Логист",
    ]


def test_parser_never_fetches_pdf_or_calls_network() -> None:
    document = json.loads((FIXTURES / "valid" / "standard-1001.json").read_text())
    response = SourceResponse(
        url=BASE_URL,
        status_code=200,
        headers={"Content-Type": "application/json"},
        content=json.dumps(document).encode(),
    )
    result = DetailJsonParser().parse(BASE_URL, response)
    assert not isinstance(result, ParseError)
    assert result.attributes.pdf_url.endswith("/1001/download")


def test_detail_attributes_are_structured_source_attributes() -> None:
    result = DetailJsonParser().parse(
        BASE_URL,
        source_response(FIXTURES / "valid" / "standard-1004.json"),
    )
    assert not isinstance(result, ParseError)
    assert isinstance(result.attributes, SourceAttributes)
    # Source parser accepts a source datetime and exposes the calendar date to
    # the typed contract; canonical normalization owns further transformations.
    assert result.attributes.revision_date == "2024-02-29"


@pytest.mark.parametrize(
    ("raw_version", "expected_version"),
    [
        (" 1 ", 1),
        ("1 ", 1),
        ("2 нұсқа", 2),
        ("1 версия", 1),
        ("Версия 2", 2),
        ("версия 3", 3),
        (4, 4),
    ],
)
def test_detail_json_parser_normalizes_text_versions(
    raw_version: str | int, expected_version: int
) -> None:
    document = json.loads((FIXTURES / "valid" / "standard-1001.json").read_text())
    document["data"]["version"] = raw_version
    response = SourceResponse(
        url=BASE_URL,
        status_code=200,
        headers={"Content-Type": "application/json"},
        content=json.dumps(document).encode(),
    )
    result = DetailJsonParser().parse(BASE_URL, response)
    assert not isinstance(result, ParseError)
    assert result.attributes.version == expected_version

