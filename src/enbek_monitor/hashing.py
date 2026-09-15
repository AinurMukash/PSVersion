"""Deterministic content hashing for normalized professional-standard records.

The service is intentionally pure: it does not access the network or
filesystem.  Metadata and normalized-record digests are calculated from
canonical JSON projections, while PDF digests are calculated from the exact
bytes supplied by the caller.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any, TypeAlias

from .models import (
    CATEGORIES_OFFICIAL_FIELDS,
    HASH_FIELDS,
    NORMALIZED_RECORD_FIELDS,
    Hashes,
    NormalizedRecord,
    OkedAttributes,
    ProfessionRecord,
    SOURCE_FIELDS,
)

JSONValue: TypeAlias = (
    str
    | int
    | float
    | bool
    | None
    | list["JSONValue"]
    | dict[str, "JSONValue"]
)

# This is the Metadata_Field_Set from the design.  The names deliberately use
# the JSON model spelling (``oked`` and ``categories.official``).
METADATA_FIELD_NAMES: tuple[str, ...] = (
    "code",
    "title",
    "government_body",
    "version",
    "year",
    "revision_date",
    "description",
    "oked",
    "professions",
    "categories",
)

# The canonical JSON printer uses this formatting.  Reusing the same settings
# means hashes remain byte-stable with the project's canonical JSON boundary:
# UTF-8, two-space indentation, LF and one final LF.
_JSON_KWARGS = {
    "ensure_ascii": False,
    "indent": 2,
    "sort_keys": False,
    "allow_nan": False,
    "separators": (",", ": "),
}


def _as_mapping(value: NormalizedRecord | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(value, NormalizedRecord):
        return value.to_dict()
    if isinstance(value, Mapping):
        return value
    raise TypeError("record or metadata must be a NormalizedRecord or mapping")


def _canonical_value(value: Any, key_order: Sequence[str] | None = None) -> JSONValue:
    """Return a detached JSON value with deterministic object-key ordering."""

    if isinstance(value, NormalizedRecord):
        value = value.to_dict()
    elif hasattr(value, "to_dict") and callable(value.to_dict):
        value = value.to_dict()

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("hash input contains a non-finite JSON number")
        return value
    if isinstance(value, Mapping):
        keys = list(value)
        if any(not isinstance(key, str) for key in keys):
            raise TypeError("hash input object keys must be strings")
        if key_order is not None:
            rank = {name: index for index, name in enumerate(key_order)}
            keys.sort(key=lambda key: (rank.get(key, len(rank)), key))
        else:
            # Python's string ordering is Unicode code-point ordering, which
            # is the fallback required by the canonical JSON contract.
            keys.sort()
        return {
            key: _canonical_value(value[key])
            for key in keys
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    raise TypeError(f"hash input contains unsupported value {type(value).__name__}")


def _canonical_record_value(name: str, value: Any) -> JSONValue:
    """Canonicalize a record field using the same nested schema order."""

    if name == "oked":
        return _canonical_value(value, OkedAttributes.__schema_fields__)
    if name == "professions":
        return [
            _canonical_value(item, ProfessionRecord.__schema_fields__)
            for item in (value or [])
        ]
    if name == "categories" and isinstance(value, Mapping):
        official = value.get("official")
        return {
            "official": _canonical_value(official, CATEGORIES_OFFICIAL_FIELDS)
        }
    if name == "source":
        return _canonical_value(value, SOURCE_FIELDS)
    return _canonical_value(value)


def _canonical_bytes(value: Mapping[str, Any], key_order: Sequence[str]) -> bytes:
    """Encode an already canonicalized projection in explicit root order."""

    keys = list(value)
    rank = {name: index for index, name in enumerate(key_order)}
    keys.sort(key=lambda key: (rank.get(key, len(rank)), key))
    ordered = {key: value[key] for key in keys}
    text = json.dumps(ordered, **_JSON_KWARGS)
    return (text + "\n").encode("utf-8")


def _metadata_projection(
    metadata: NormalizedRecord | Mapping[str, Any],
) -> dict[str, JSONValue]:
    """Select Metadata_Field_Set and retain its null/empty states."""

    source = _as_mapping(metadata)
    projection: dict[str, JSONValue] = {}
    for name in METADATA_FIELD_NAMES:
        if name == "categories":
            categories = source.get("categories", {})
            if isinstance(categories, Mapping):
                # Metadata includes only the official category projection, not
                # any future operational/non-official category fields.
                projection[name] = {
                    "official": _canonical_value(
                        categories.get("official"), CATEGORIES_OFFICIAL_FIELDS
                    )
                }
            else:
                projection[name] = _canonical_value(categories)
        else:
            projection[name] = _canonical_record_value(
                name, source.get(name)
            )
    return projection


def _normalized_projection(record: NormalizedRecord | Mapping[str, Any]) -> dict[str, JSONValue]:
    """Remove only hashes and technical timestamps from a record projection."""

    source = _as_mapping(record)
    return {
        name: _canonical_record_value(name, source[name])
        for name in NORMALIZED_RECORD_FIELDS
        if name not in {"hashes", "timestamps"} and name in source
    }


class HashingService:
    """Calculate deterministic content hashes without mutating input records."""

    metadata_fields = METADATA_FIELD_NAMES

    def metadata_sha256(
        self, metadata: NormalizedRecord | Mapping[str, Any]
    ) -> str:
        """Hash the canonical Metadata_Field_Set projection."""

        return hashlib.sha256(
            _canonical_bytes(_metadata_projection(metadata), METADATA_FIELD_NAMES)
        ).hexdigest()

    def pdf_sha256(self, pdf_bytes: bytes | bytearray | memoryview) -> str:
        """Hash the exact original PDF byte sequence, without decoding it."""

        if not isinstance(pdf_bytes, (bytes, bytearray, memoryview)):
            raise TypeError("pdf_bytes must be bytes-like")
        return hashlib.sha256(bytes(pdf_bytes)).hexdigest()

    def normalized_sha256(
        self, record: NormalizedRecord | Mapping[str, Any]
    ) -> str:
        """Hash all canonical record fields except hashes and timestamps."""

        return hashlib.sha256(
            _canonical_bytes(
                _normalized_projection(record),
                tuple(name for name in NORMALIZED_RECORD_FIELDS if name not in HASH_FIELDS and name != "timestamps"),
            )
        ).hexdigest()

    def compute_hashes(
        self,
        record: NormalizedRecord,
        pdf_bytes: bytes | bytearray | memoryview | None = None,
    ) -> Hashes:
        """Return all three hashes for ``record`` without changing it.

        ``pdf_bytes`` is required unless the record already contains a PDF
        digest, allowing a normalized record produced by the PDF collector to
        be completed after the exact bytes have been verified.
        """

        if not isinstance(record, NormalizedRecord):
            raise TypeError("record must be a NormalizedRecord")
        if pdf_bytes is None:
            pdf_digest = record.hashes.pdf_sha256
            if pdf_digest is None:
                raise ValueError("pdf_bytes is required when record has no pdf_sha256")
        else:
            pdf_digest = self.pdf_sha256(pdf_bytes)
        return Hashes(
            metadata_sha256=self.metadata_sha256(record),
            pdf_sha256=pdf_digest,
            normalized_sha256=self.normalized_sha256(record),
        )

    def with_hashes(
        self,
        record: NormalizedRecord,
        pdf_bytes: bytes | bytearray | memoryview | None = None,
    ) -> NormalizedRecord:
        """Return a deep-copied record populated with all three hashes."""

        if not isinstance(record, NormalizedRecord):
            raise TypeError("record must be a NormalizedRecord")
        hashes = self.compute_hashes(record, pdf_bytes)
        result = copy.deepcopy(record)
        result.hashes = hashes
        return result

    # Names used by callers that prefer the design-document terminology.
    hash_record = with_hashes
    fill_hashes = with_hashes
    calculate = compute_hashes


# Design-document spelling and functional conveniences.
Hashing_Service = HashingService
Hashing = HashingService


def metadata_sha256(metadata: NormalizedRecord | Mapping[str, Any]) -> str:
    return HashingService().metadata_sha256(metadata)


def pdf_sha256(pdf_bytes: bytes | bytearray | memoryview) -> str:
    return HashingService().pdf_sha256(pdf_bytes)


def normalized_sha256(record: NormalizedRecord | Mapping[str, Any]) -> str:
    return HashingService().normalized_sha256(record)


def compute_hashes(
    record: NormalizedRecord,
    pdf_bytes: bytes | bytearray | memoryview | None = None,
) -> Hashes:
    return HashingService().compute_hashes(record, pdf_bytes)


def with_hashes(
    record: NormalizedRecord,
    pdf_bytes: bytes | bytearray | memoryview | None = None,
) -> NormalizedRecord:
    return HashingService().with_hashes(record, pdf_bytes)


def fill_hashes(
    record: NormalizedRecord,
    pdf_bytes: bytes | bytearray | memoryview | None = None,
) -> NormalizedRecord:
    return with_hashes(record, pdf_bytes)


__all__ = [
    "HashingService",
    "Hashing_Service",
    "Hashing",
    "METADATA_FIELD_NAMES",
    "compute_hashes",
    "fill_hashes",
    "metadata_sha256",
    "normalized_sha256",
    "pdf_sha256",
    "with_hashes",
]
