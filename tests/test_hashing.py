"""Unit tests for deterministic content hashing."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from enbek_monitor.hashing import HashingService
from enbek_monitor.models import Hashes, NormalizedRecord, Timestamps


FIXTURE = Path(__file__).parents[1] / "fixtures" / "normalized" / "valid" / "standard-1001.json"


def _record() -> NormalizedRecord:
    return NormalizedRecord.from_dict(json.loads(FIXTURE.read_text(encoding="utf-8")))


def test_pdf_sha256_hashes_exact_bytes_and_returns_lowercase_hex() -> None:
    service = HashingService()
    payload = b"%PDF\x00\xff\x01\n"

    digest = service.pdf_sha256(payload)

    assert digest == hashlib.sha256(payload).hexdigest()
    assert len(digest) == 64
    assert digest == digest.lower()
    assert service.pdf_sha256(payload + b"different") != digest


def test_metadata_hash_is_independent_of_mapping_key_order() -> None:
    service = HashingService()
    record = _record()
    source = record.to_dict()
    reordered = {name: source[name] for name in reversed(tuple(source))}
    reordered["categories"] = {
        "official": {
            name: source["categories"]["official"][name]
            for name in reversed(tuple(source["categories"]["official"]))
        }
    }

    assert service.metadata_sha256(record) == service.metadata_sha256(reordered)


def test_normalized_hash_excludes_hashes_and_technical_timestamps() -> None:
    service = HashingService()
    record = _record()
    changed = copy.deepcopy(record)
    changed.hashes = Hashes(
        metadata_sha256="a" * 64,
        pdf_sha256="b" * 64,
        normalized_sha256="c" * 64,
    )
    changed.timestamps = Timestamps(
        first_seen_at="2030-01-01T00:00:00Z",
        last_seen_at="2030-01-02T00:00:00Z",
        last_changed_at="2030-01-03T00:00:00Z",
    )

    assert service.normalized_sha256(record) == service.normalized_sha256(changed)


def test_included_field_changes_metadata_and_normalized_hash() -> None:
    service = HashingService()
    record = _record()
    changed = copy.deepcopy(record)
    changed.title = "Изменённое название"

    assert service.metadata_sha256(changed) != service.metadata_sha256(record)
    assert service.normalized_sha256(changed) != service.normalized_sha256(record)


def test_with_hashes_returns_new_record_and_does_not_mutate_input() -> None:
    service = HashingService()
    record = _record()
    original = copy.deepcopy(record)
    pdf_bytes = b"exact PDF bytes"

    result = service.with_hashes(record, pdf_bytes)

    assert result is not record
    assert record.to_dict() == original.to_dict()
    assert result.hashes.to_dict() == {
        "metadata_sha256": service.metadata_sha256(record),
        "pdf_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
        "normalized_sha256": service.normalized_sha256(record),
    }
    assert all(
        isinstance(value, str) and len(value) == 64 and value == value.lower()
        for value in result.hashes.to_dict().values()
    )


def test_compute_hashes_can_reuse_existing_verified_pdf_digest() -> None:
    service = HashingService()
    record = _record()
    hashes = service.compute_hashes(record)

    assert hashes.pdf_sha256 == record.hashes.pdf_sha256
    assert hashes.metadata_sha256 == service.metadata_sha256(record)
    assert hashes.normalized_sha256 == service.normalized_sha256(record)


def test_pdf_bytes_are_required_when_record_has_no_pdf_digest() -> None:
    service = HashingService()
    record = _record()
    record.hashes = Hashes()

    with pytest.raises(ValueError, match="pdf_bytes is required"):
        service.compute_hashes(record)
