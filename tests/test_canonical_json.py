"""Unit coverage for deterministic NormalizedRecord JSON."""

from __future__ import annotations

import json
from collections import OrderedDict

import pytest

from enbek_monitor.canonical_json import (
    CanonicalJsonParser,
    CanonicalJsonPrinter,
)
from enbek_monitor.errors import ParseError
from enbek_monitor.models import NormalizedRecord


@pytest.fixture
def canonical_pair(fixture_manifest):
    case = fixture_manifest.get_case("Normalized_Record", "normalized-complete-1001")
    payload = case.read_bytes()
    parsed = CanonicalJsonParser().parse(payload)
    assert isinstance(parsed, NormalizedRecord)
    return parsed


def test_valid_fixture_round_trips_without_value_loss(fixture_manifest) -> None:
    printer = CanonicalJsonPrinter()
    parser = CanonicalJsonParser()

    for case in fixture_manifest.get_cases("Normalized_Record", group="valid"):
        original_document = case.load_json()
        record = parser.parse(case.read_bytes())
        assert isinstance(record, NormalizedRecord), case.id
        printed = printer.print(record)
        reparsed = parser.parse(printed)
        assert isinstance(reparsed, NormalizedRecord), case.id
        assert reparsed.to_dict() == record.to_dict() == original_document


def test_printer_is_byte_deterministic_and_uses_schema_order(canonical_pair) -> None:
    printer = CanonicalJsonPrinter()
    first = printer.print(canonical_pair)

    reordered = OrderedDict(reversed(list(canonical_pair.to_dict().items())))
    second = printer.print(reordered)
    assert first == second
    assert first.startswith(b"{\n  \"schema_version\"")
    assert not first.startswith(b"\xef\xbb\xbf")
    assert first.endswith(b"\n") and not first.endswith(b"\n\n")
    assert all(not line.endswith((b" ", b"\t")) for line in first.split(b"\n")[:-1])
    assert list(json.loads(first).keys()) == [
        "schema_version",
        "enbek_id",
        "code",
        "title",
        "government_body",
        "description",
        "version",
        "year",
        "revision_date",
        "oked",
        "professions",
        "categories",
        "source",
        "hashes",
        "timestamps",
    ]


def test_round_trip_preserves_nulls_empty_arrays_dates_and_numbers(fixture_manifest) -> None:
    case = fixture_manifest.get_case("Normalized_Record", "normalized-null-scalars-1002")
    parser = CanonicalJsonParser()
    printer = CanonicalJsonPrinter()
    result = parser.parse(printer.print(parser.parse(case.read_bytes())))

    assert isinstance(result, NormalizedRecord)
    assert result.code is None
    assert result.professions == []
    assert result.revision_date is None
    assert result.year == 2024
    assert isinstance(result.year, int)
    assert result.timestamps.first_seen_at == "2025-01-15T08:00:00Z"


@pytest.mark.parametrize(
    ("payload", "expected_path"),
    [
        (b"\xef\xbb\xbf{}", "$"),
        (b"{not-json", "$"),
        (b"\xff", "$"),
        (b"{}", "$.schema_version"),
        (b'{"schema_version":"1","enbek_id":1}', "$.code"),
    ],
)
def test_parser_rejects_encoding_syntax_and_missing_schema(payload: bytes, expected_path: str) -> None:
    result = CanonicalJsonParser().parse(payload)
    assert isinstance(result, ParseError)
    assert result.path == expected_path
    assert result.path != "$.partial"


def test_parser_reports_exact_nested_field_path(fixture_manifest) -> None:
    case = fixture_manifest.get_case("Normalized_Record", "normalized-complete-1001")
    parsed = CanonicalJsonParser().parse(case.read_bytes())
    assert isinstance(parsed, NormalizedRecord)
    document = json.loads(CanonicalJsonPrinter().print(parsed))
    document["timestamps"]["last_seen_at"] = "not-a-date"
    result = CanonicalJsonParser().parse(json.dumps(document).encode("utf-8"))

    assert isinstance(result, ParseError)
    assert result.path == "$.timestamps.last_seen_at"
    assert "$.timestamps.last_seen_at" in result.invalid_fields


def test_parser_rejects_unknown_fields_and_invalid_fixture_cases(fixture_manifest) -> None:
    parser = CanonicalJsonParser()
    for case in fixture_manifest.get_cases("Normalized_Record", group="invalid"):
        result = parser.parse(case.read_bytes())
        assert isinstance(result, ParseError), case.id
        for expected_path in case.expected_error_paths:
            assert expected_path in result.invalid_fields or result.path == expected_path


def test_printer_rejects_unknown_fields_without_emitting_bytes(canonical_pair) -> None:
    document = canonical_pair.to_dict()
    document["unexpected_field"] = True
    with pytest.raises(ParseError) as raised:
        CanonicalJsonPrinter().print(document)

    assert raised.value.path == "$.unexpected_field"
    assert raised.value.invalid_fields == ("$.unexpected_field",)


def test_parser_never_returns_partial_record_for_wrong_types() -> None:
    payload = b'{"schema_version":"1","enbek_id":true}'
    result = CanonicalJsonParser().parse(payload)
    assert isinstance(result, ParseError)
    assert not isinstance(result, NormalizedRecord)
    assert result.path == "$.enbek_id"
