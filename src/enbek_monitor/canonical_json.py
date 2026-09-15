"""Deterministic JSON encoding and decoding for :class:`NormalizedRecord`.

The canonical JSON boundary is deliberately pure: it accepts an already
materialised record (or a JSON mapping), performs no I/O, and either returns a
complete record/byte document or a structured :class:`ParseError`.  The
serializer uses the schema order declared by ``models.py`` and rejects fields
which are not part of that schema.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from json import JSONDecodeError
from typing import Any, TypeAlias
from urllib.parse import urlsplit

from .errors import ParseError
from .models import (
    CATEGORIES_OFFICIAL_FIELDS,
    HASH_FIELDS,
    NORMALIZED_RECORD_FIELDS,
    NormalizedRecord,
    OkedAttributes,
    ProfessionRecord,
    SOURCE_FIELDS,
    TIMESTAMP_FIELDS,
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

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_LIFECYCLE_STATES = frozenset({"BASELINE", "ACTIVE", "MISSING", "REMOVED"})


class _SchemaIssue:
    """Internal schema violation with a stable JSON path."""

    __slots__ = ("path", "reason", "missing")

    def __init__(self, path: str, reason: str, *, missing: bool = False) -> None:
        self.path = path
        self.reason = reason
        self.missing = missing


class _DuplicateKey(ValueError):
    def __init__(self, key: Any) -> None:
        self.key = key
        super().__init__(f"duplicate object key {key!r}")


def _path_field(path: str, name: str) -> str:
    return f"{path}.{name}"


def _path_index(path: str, index: int) -> str:
    return f"{path}[{index}]"


def _object_issues(
    value: Any,
    path: str,
    expected: Sequence[str],
    issues: list[_SchemaIssue],
) -> bool:
    """Check object shape and collect unknown/missing keys without partial use."""

    if not isinstance(value, Mapping):
        issues.append(_SchemaIssue(path, "expected an object"))
        return False
    expected_set = set(expected)
    for key in value:
        if not isinstance(key, str):
            issues.append(_SchemaIssue(path, "object keys must be strings"))
        elif key not in expected_set:
            issues.append(_SchemaIssue(_path_field(path, key), "unknown field"))
    for key in expected:
        if key not in value:
            issues.append(_SchemaIssue(_path_field(path, key), "field is required", missing=True))
    return True


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and (not isinstance(value, float) or math.isfinite(value))
    )


def _check_optional_text(value: Any, path: str, issues: list[_SchemaIssue]) -> None:
    if value is not None and not isinstance(value, str):
        issues.append(_SchemaIssue(path, "expected a string or null"))


def _check_optional_number(value: Any, path: str, issues: list[_SchemaIssue]) -> None:
    if value is not None and not _is_number(value):
        issues.append(_SchemaIssue(path, "expected a finite number or null"))


def _check_date(value: Any, path: str, issues: list[_SchemaIssue]) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        issues.append(_SchemaIssue(path, "expected YYYY-MM-DD or null"))
        return
    try:
        date.fromisoformat(value)
    except ValueError:
        issues.append(_SchemaIssue(path, "expected a valid calendar date"))


def _check_datetime(value: Any, path: str, issues: list[_SchemaIssue]) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not _DATETIME_RE.fullmatch(value):
        issues.append(_SchemaIssue(path, "expected UTC YYYY-MM-DDTHH:mm:ssZ or null"))
        return
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        issues.append(_SchemaIssue(path, "expected a valid UTC date-time"))
        return
    if parsed.utcoffset() is None:
        issues.append(_SchemaIssue(path, "expected a UTC date-time"))


def _check_source_url(value: Any, path: str, issues: list[_SchemaIssue]) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value or value != value.strip():
        issues.append(_SchemaIssue(path, "expected an absolute source URL or null"))
        return
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https", "fixture"} or not parsed.netloc:
        issues.append(_SchemaIssue(path, "expected an absolute source URL or null"))


def _check_oked(value: Any, path: str, issues: list[_SchemaIssue]) -> None:
    if value is None:
        return
    if not _object_issues(value, path, OkedAttributes.__schema_fields__, issues):
        return
    for name in OkedAttributes.__schema_fields__:
        _check_optional_text(value[name], _path_field(path, name), issues)


def _check_professions(value: Any, path: str, issues: list[_SchemaIssue]) -> None:
    if not isinstance(value, list):
        issues.append(_SchemaIssue(path, "expected an array"))
        return
    for index, profession in enumerate(value):
        item_path = _path_index(path, index)
        if not _object_issues(profession, item_path, ProfessionRecord.__schema_fields__, issues):
            continue
        name_path = _path_field(item_path, "name")
        _check_optional_text(profession["name"], name_path, issues)
        level = profession["ork_level"]
        if level is not None and (
            isinstance(level, bool) or not isinstance(level, int) or level < 0
        ):
            issues.append(_SchemaIssue(_path_field(item_path, "ork_level"), "expected a non-negative integer or null"))


def _check_official(value: Any, path: str, issues: list[_SchemaIssue]) -> None:
    if not _object_issues(value, path, CATEGORIES_OFFICIAL_FIELDS, issues):
        return
    oked = value["oked"]
    if not isinstance(oked, list):
        issues.append(_SchemaIssue(_path_field(path, "oked"), "expected an array"))
    else:
        for index, item in enumerate(oked):
            if not isinstance(item, str) or not item.strip():
                issues.append(_SchemaIssue(_path_index(_path_field(path, "oked"), index), "expected a non-empty string"))
    _check_optional_text(value["government_body"], _path_field(path, "government_body"), issues)
    levels = value["ork_levels"]
    if not isinstance(levels, list):
        issues.append(_SchemaIssue(_path_field(path, "ork_levels"), "expected an array"))
    else:
        for index, item in enumerate(levels):
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                issues.append(_SchemaIssue(_path_index(_path_field(path, "ork_levels"), index), "expected a non-negative integer"))
    _check_optional_number(value["year"], _path_field(path, "year"), issues)
    _check_optional_number(value["version"], _path_field(path, "version"), issues)
    state_path = _path_field(path, "lifecycle_state")
    if not isinstance(value["lifecycle_state"], str) or value["lifecycle_state"] not in _LIFECYCLE_STATES:
        issues.append(_SchemaIssue(state_path, "unknown lifecycle state"))


def _check_nested_objects(document: Mapping[str, Any], issues: list[_SchemaIssue]) -> None:
    categories_path = "$.categories"
    if "categories" in document and _object_issues(
        document["categories"], categories_path, ("official",), issues
    ):
        if "official" in document["categories"]:
            _check_official(document["categories"]["official"], "$.categories.official", issues)

    source_path = "$.source"
    if "source" in document and _object_issues(
        document["source"], source_path, SOURCE_FIELDS, issues
    ):
        for name in SOURCE_FIELDS:
            if name in document["source"]:
                source_value = document["source"][name]
                _check_optional_text(source_value, _path_field(source_path, name), issues)
                _check_source_url(source_value, _path_field(source_path, name), issues)

    hashes_path = "$.hashes"
    if "hashes" in document and _object_issues(
        document["hashes"], hashes_path, HASH_FIELDS, issues
    ):
        for name in HASH_FIELDS:
            if name in document["hashes"]:
                value = document["hashes"][name]
                if value is not None and (
                    not isinstance(value, str) or not _SHA256_RE.fullmatch(value)
                ):
                    issues.append(_SchemaIssue(_path_field(hashes_path, name), "expected a 64-character hexadecimal SHA-256 digest or null"))

    timestamps_path = "$.timestamps"
    if "timestamps" in document and _object_issues(
        document["timestamps"], timestamps_path, TIMESTAMP_FIELDS, issues
    ):
        for name in TIMESTAMP_FIELDS:
            if name in document["timestamps"]:
                _check_datetime(document["timestamps"][name], _path_field(timestamps_path, name), issues)


def _validate_document(document: Any) -> tuple[_SchemaIssue, ...]:
    """Validate the complete NormalizedRecord JSON shape and all leaf types."""

    issues: list[_SchemaIssue] = []
    if not _object_issues(document, "$", NORMALIZED_RECORD_FIELDS, issues):
        return tuple(issues)

    if "schema_version" in document:
        schema_version = document["schema_version"]
        if not isinstance(schema_version, str) or not schema_version.strip():
            issues.append(_SchemaIssue("$.schema_version", "expected a non-empty string"))

    if "enbek_id" in document:
        enbek_id = document["enbek_id"]
        if isinstance(enbek_id, bool) or not isinstance(enbek_id, int) or enbek_id <= 0:
            issues.append(_SchemaIssue("$.enbek_id", "expected an integer greater than zero"))

    for name in ("code", "title", "government_body", "description"):
        if name in document:
            _check_optional_text(document[name], f"$.{name}", issues)
    if "title" in document and isinstance(document["title"], str) and not document["title"].strip():
        issues.append(_SchemaIssue("$.title", "expected a non-blank string or null"))
    if "version" in document:
        _check_optional_number(document["version"], "$.version", issues)
    if "year" in document:
        _check_optional_number(document["year"], "$.year", issues)
    if "revision_date" in document:
        _check_date(document["revision_date"], "$.revision_date", issues)
    if "oked" in document:
        _check_oked(document["oked"], "$.oked", issues)
    if "professions" in document:
        _check_professions(document["professions"], "$.professions", issues)
    _check_nested_objects(document, issues)
    return tuple(issues)


def _parse_error(
    reason: str,
    *,
    path: str = "$",
    missing: Sequence[str] = (),
    invalid: Sequence[str] = (),
) -> ParseError:
    return ParseError(
        reason,
        path=path,
        missing_fields=tuple(missing),
        invalid_fields=tuple(invalid),
        stage="canonical_json_parser",
    )


def _record_document(record: NormalizedRecord | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(record, NormalizedRecord):
        return record.to_dict()
    if isinstance(record, Mapping):
        return record
    raise _parse_error(
        "canonical JSON printer requires a NormalizedRecord or object mapping",
        invalid=("$",),
    )


def _ordered(value: Any, key_order: Sequence[str] | None = None) -> Any:
    """Copy values into schema order, using Unicode lexical order as fallback."""

    if isinstance(value, Mapping):
        keys = list(value)
        if key_order is not None:
            rank = {name: index for index, name in enumerate(key_order)}
            keys.sort(key=lambda key: (rank.get(key, len(rank)), key))
        else:
            keys.sort()
        return {key: _ordered(value[key]) for key in keys}
    if isinstance(value, list):
        return [_ordered(item) for item in value]
    if isinstance(value, tuple):
        return [_ordered(item) for item in value]
    return value


def _ordered_record(document: Mapping[str, Any]) -> dict[str, Any]:
    """Apply the complete schema order recursively after validation."""

    result: dict[str, Any] = {}
    for name in NORMALIZED_RECORD_FIELDS:
        value = document[name]
        if name == "oked" and isinstance(value, Mapping):
            result[name] = _ordered(value, OkedAttributes.__schema_fields__)
        elif name == "professions":
            result[name] = [
                _ordered(item, ProfessionRecord.__schema_fields__)
                for item in value
            ]
        elif name == "categories":
            result[name] = {
                "official": _ordered(
                    value["official"], CATEGORIES_OFFICIAL_FIELDS
                )
            }
        elif name == "source":
            result[name] = _ordered(value, SOURCE_FIELDS)
        elif name == "hashes":
            result[name] = _ordered(value, HASH_FIELDS)
        elif name == "timestamps":
            result[name] = _ordered(value, TIMESTAMP_FIELDS)
        else:
            result[name] = _ordered(value)
    return result


def _json_loads_no_duplicates(text: str) -> Any:
    def object_pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise _DuplicateKey(key)
            result[key] = value
        return result

    return json.loads(
        text,
        object_pairs_hook=object_pairs_hook,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"non-standard JSON constant {value}")
        ),
    )


class CanonicalJsonPrinter:
    """Print a validated ``NormalizedRecord`` in deterministic UTF-8 JSON."""

    def print(self, record: NormalizedRecord | Mapping[str, Any]) -> bytes:
        document = _record_document(record)
        issues = _validate_document(document)
        if issues:
            missing = tuple(issue.path for issue in issues if issue.missing)
            invalid = tuple(issue.path for issue in issues if not issue.missing)
            first = issues[0]
            raise _parse_error(
                "normalized record does not conform to the canonical schema: "
                + "; ".join(f"{issue.path}: {issue.reason}" for issue in issues),
                path=first.path,
                missing=missing,
                invalid=invalid,
            )
        ordered = _ordered_record(document)
        text = json.dumps(
            ordered,
            ensure_ascii=False,
            indent=2,
            sort_keys=False,
            allow_nan=False,
            separators=(",", ": "),
        )
        # json.dumps(indent=2) emits no final newline.  Appending exactly one
        # gives LF regardless of the host platform and cannot create trailing
        # spaces or a BOM.
        return (text + "\n").encode("utf-8")

    dumps = print
    to_bytes = print
    encode = print
    serialize = print
    __call__ = print


class CanonicalJsonParser:
    """Parse canonical JSON bytes into one complete ``NormalizedRecord``."""

    def parse(self, payload: bytes | bytearray | memoryview) -> NormalizedRecord | ParseError:
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            return _parse_error(
                "canonical JSON input must be UTF-8 bytes",
                invalid=("$",),
            )
        raw = bytes(payload)
        if raw.startswith(b"\xef\xbb\xbf"):
            return _parse_error(
                "UTF-8 BOM is not permitted in canonical JSON",
                invalid=("$",),
            )
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            return _parse_error(
                f"canonical JSON is not valid UTF-8: {exc.reason}",
                invalid=("$",),
            )
        try:
            document = _json_loads_no_duplicates(text)
        except _DuplicateKey as exc:
            return _parse_error(
                f"canonical JSON contains {exc}",
                invalid=("$",),
            )
        except (JSONDecodeError, ValueError, TypeError) as exc:
            return _parse_error(
                f"canonical JSON syntax is invalid: {exc}",
                invalid=("$",),
            )

        issues = _validate_document(document)
        if issues:
            missing = tuple(issue.path for issue in issues if issue.missing)
            invalid = tuple(issue.path for issue in issues if not issue.missing)
            first = issues[0]
            return _parse_error(
                "normalized record does not conform to the canonical schema: "
                + "; ".join(f"{issue.path}: {issue.reason}" for issue in issues),
                path=first.path,
                missing=missing,
                invalid=invalid,
            )
        try:
            return NormalizedRecord.from_dict(document)
        except (TypeError, ValueError) as exc:
            # The complete validation above normally makes this unreachable;
            # retaining the guard prevents any partial model escaping if the
            # dataclass contract becomes stricter in a future version.
            return _parse_error(
                f"normalized record schema validation failed: {exc}",
                invalid=("$",),
            )

    loads = parse
    from_bytes = parse
    decode = parse
    deserialize = parse
    __call__ = parse

    def parse_or_raise(self, payload: bytes | bytearray | memoryview) -> NormalizedRecord:
        result = self.parse(payload)
        if isinstance(result, ParseError):
            raise result
        return result


# Design-document spellings and convenient functional APIs.
Canonical_Json_Printer = CanonicalJsonPrinter
Canonical_Json_Parser = CanonicalJsonParser
CanonicalJSONPrinter = CanonicalJsonPrinter
CanonicalJSONParser = CanonicalJsonParser


def print_canonical_json(record: NormalizedRecord | Mapping[str, Any]) -> bytes:
    return CanonicalJsonPrinter().print(record)


def parse_canonical_json(payload: bytes | bytearray | memoryview) -> NormalizedRecord | ParseError:
    return CanonicalJsonParser().parse(payload)


# Short aliases mirror json.dumps/json.loads naming without shadowing stdlib
# functions in the module implementation.
dumps = print_canonical_json
loads = parse_canonical_json


__all__ = [
    "CanonicalJsonParser",
    "CanonicalJsonPrinter",
    "CanonicalJSONParser",
    "CanonicalJSONPrinter",
    "Canonical_Json_Parser",
    "Canonical_Json_Printer",
    "dumps",
    "loads",
    "parse_canonical_json",
    "print_canonical_json",
]
