"""Focused tests for the manifest-backed fixture API."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from tests.fixtures.loader import (
    ExpectedOutcome,
    FixtureGroup,
    FixtureKind,
    KNOWN_FIXTURE_KINDS,
    ManifestValidationError,
    load_manifest,
)


MANIFEST_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "manifest.json"


def _write_manifest(tmp_path: Path, document: dict) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_manifest_exposes_typed_cases_and_validated_paths() -> None:
    manifest = load_manifest(MANIFEST_PATH)

    assert set(manifest.cases) == set(KNOWN_FIXTURE_KINDS)
    assert manifest.network_required is False
    assert all(case.path.is_file() for case in manifest.all_cases)
    assert all(
        case.path.is_relative_to(manifest.fixtures_root)
        for case in manifest.all_cases
    )
    assert all(
        case.expected_outcome is ExpectedOutcome.ACCEPT
        for case in manifest.valid_cases
    )
    assert all(
        case.expected_outcome is ExpectedOutcome.REJECT
        for case in manifest.invalid_cases
    )
    assert {pair.scenario for pair in manifest.snapshot_pairs} == {
        "NEW",
        "UPDATED",
        "UNCHANGED",
        "MISSING",
        "REMOVED",
        "RESTORED",
    }


def test_manifest_declares_expected_outcome_for_every_fixture_group() -> None:
    manifest = load_manifest(MANIFEST_PATH)

    for kind in KNOWN_FIXTURE_KINDS:
        valid_cases = manifest.get_cases(kind, group=FixtureGroup.VALID)
        invalid_cases = manifest.get_cases(kind, group=FixtureGroup.INVALID)

        assert 1 <= len(valid_cases) <= 10
        assert 1 <= len(invalid_cases) <= 10
        assert all(
            case.expected_outcome is ExpectedOutcome.ACCEPT
            for case in valid_cases
        )
        assert all(
            case.expected_outcome is ExpectedOutcome.REJECT
            for case in invalid_cases
        )
        assert all(case.expected_error_paths == () for case in valid_cases)
        assert all(case.expected_error_paths for case in invalid_cases)


def test_manifest_fallback_references_are_contained_by_fixture_root() -> None:
    manifest = load_manifest(MANIFEST_PATH)
    fallback = manifest.fallback

    paths = [
        fallback["profile_path"],
        *fallback["registry_page_paths"],
        *fallback["normalized_record_paths"],
    ]
    paths.extend(fallback["detail_paths"].values())
    paths.extend(fallback["pdf_response_paths"].values())

    for relative_path in paths:
        resolved = manifest.resolve(relative_path)
        assert resolved.is_file()
        assert resolved.is_relative_to(manifest.fixtures_root)


def test_manifest_rejects_unknown_kind_before_cases_are_consumed(
    tmp_path: Path,
) -> None:
    document = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    document["fixtures"]["Unknown_Page"] = copy.deepcopy(
        document["fixtures"]["Registry_Page"]
    )

    with pytest.raises(ManifestValidationError, match="unknown fixture kind"):
        load_manifest(
            _write_manifest(tmp_path, document),
            fixtures_root=MANIFEST_PATH.parent,
        )


def test_manifest_rejects_missing_required_kind(tmp_path: Path) -> None:
    document = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    del document["fixtures"][FixtureKind.PDF_DOCUMENT.value]

    with pytest.raises(ManifestValidationError, match="missing required fixture kind"):
        load_manifest(
            _write_manifest(tmp_path, document),
            fixtures_root=MANIFEST_PATH.parent,
        )


def test_manifest_rejects_path_escape(tmp_path: Path) -> None:
    document = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    document["fixtures"]["Registry_Page"]["valid"][0]["path"] = "../manifest.json"

    with pytest.raises(ManifestValidationError, match="escapes fixtures root"):
        load_manifest(
            _write_manifest(tmp_path, document),
            fixtures_root=MANIFEST_PATH.parent,
        )


def test_manifest_rejects_fallback_path_escape(tmp_path: Path) -> None:
    document = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    document["fallback"]["profile_path"] = "../manifest.json"

    with pytest.raises(ManifestValidationError, match="escapes fixtures root"):
        load_manifest(
            _write_manifest(tmp_path, document),
            fixtures_root=MANIFEST_PATH.parent,
        )


def test_manifest_rejects_outcome_that_disagrees_with_group(tmp_path: Path) -> None:
    document = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    document["fixtures"]["Detail_Page"]["valid"][0]["expected_outcome"] = "reject"

    with pytest.raises(ManifestValidationError, match="requires 'accept'"):
        load_manifest(
            _write_manifest(tmp_path, document),
            fixtures_root=MANIFEST_PATH.parent,
        )
