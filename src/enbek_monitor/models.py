"""Typed domain models and schema constants for the local Enbek monitor.

The module contains data-only contracts shared by collectors, normalisation,
snapshot/lifecycle code and persistence.  Models deliberately do not perform
network or filesystem work.  They keep absent scalar values as ``None`` and
absent collections as empty lists, while ``to_dict`` always emits the complete
model shape so a validator can detect missing or unknown fields explicitly.
"""

from __future__ import annotations

import copy
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, Literal, TypeAlias, TypeVar


# ---------------------------------------------------------------------------
# JSON and schema constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "1"
NORMALIZED_RECORD_SCHEMA_VERSION = SCHEMA_VERSION
SOURCE_PROFILE_SCHEMA_VERSION = SCHEMA_VERSION

JSONScalar: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]
JSONMapping: TypeAlias = dict[str, JSONValue]
Number: TypeAlias = int | float
ISODate: TypeAlias = str
ISODateTime: TypeAlias = str
SHA256Digest: TypeAlias = str

MISSING_SCALAR: None = None
MISSING_COLLECTION: tuple[()] = ()

CATEGORIES_OFFICIAL_FIELDS: tuple[str, ...] = (
    "oked",
    "government_body",
    "ork_levels",
    "year",
    "version",
    "lifecycle_state",
)
SOURCE_FIELDS: tuple[str, ...] = ("registry_url", "page_url", "pdf_url")
HASH_FIELDS: tuple[str, ...] = (
    "metadata_sha256",
    "pdf_sha256",
    "normalized_sha256",
)
TIMESTAMP_FIELDS: tuple[str, ...] = (
    "first_seen_at",
    "last_seen_at",
    "last_changed_at",
)
NORMALIZED_RECORD_FIELDS: tuple[str, ...] = (
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
)

# These constants are intentionally plain tuples/dicts.  A future validator
# can use them without importing or instantiating a model, and can therefore
# report unknown fields instead of silently dropping them.
MODEL_SCHEMA_FIELDS: dict[str, tuple[str, ...]] = {
    "categories.official": CATEGORIES_OFFICIAL_FIELDS,
    "source": SOURCE_FIELDS,
    "hashes": HASH_FIELDS,
    "timestamps": TIMESTAMP_FIELDS,
    "normalized_record": NORMALIZED_RECORD_FIELDS,
}

NORMALIZED_RECORD_REQUIRED_FIELDS: frozenset[str] = frozenset(
    NORMALIZED_RECORD_FIELDS
)

# JSON-schema-like metadata is kept deliberately small and dependency-free.
# It documents shape/default semantics; runtime checks live in dataclass
# __post_init__ methods below.
NORMALIZED_RECORD_SCHEMA: dict[str, Any] = {
    "schema_version": {"type": "string", "required": True},
    "enbek_id": {"type": "integer", "minimum": 1, "required": True},
    "code": {"type": ["string", "null"], "default": None},
    "title": {"type": ["string", "null"], "default": None},
    "government_body": {"type": ["string", "null"], "default": None},
    "description": {"type": ["string", "null"], "default": None},
    "version": {"type": ["number", "null"], "default": None},
    "year": {"type": ["number", "null"], "default": None},
    "revision_date": {"type": ["string", "null"], "format": "date"},
    "oked": {"type": ["object", "null"], "default": None},
    "professions": {"type": "array", "default": []},
    "categories": {
        "type": "object",
        "required": True,
        "properties": {"official": {"type": "object", "required": True}},
    },
    "source": {
        "type": "object",
        "required": True,
        "properties": {
            name: {"type": ["string", "null"]} for name in SOURCE_FIELDS
        },
    },
    "hashes": {
        "type": "object",
        "required": True,
        "properties": {
            name: {"type": ["string", "null"], "format": "sha256"}
            for name in HASH_FIELDS
        },
    },
    "timestamps": {
        "type": "object",
        "required": True,
        "properties": {
            name: {"type": ["string", "null"], "format": "date-time"}
            for name in TIMESTAMP_FIELDS
        },
    },
}


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class _StringEnum(str, Enum):
    """Enum whose values remain directly comparable to JSON strings."""

    def __str__(self) -> str:
        return self.value


class RunMode(_StringEnum):
    BOOTSTRAP = "bootstrap"
    SYNC = "sync"
    DRY_RUN = "dry_run"


class RunStatus(_StringEnum):
    SUCCESS = "success"
    FAILED = "failed"


class EventType(_StringEnum):
    NEW = "NEW"
    UPDATED = "UPDATED"
    MISSING = "MISSING"
    REMOVED = "REMOVED"
    RESTORED = "RESTORED"


class LifecycleState(_StringEnum):
    BASELINE = "BASELINE"
    ACTIVE = "ACTIVE"
    MISSING = "MISSING"
    REMOVED = "REMOVED"


class ArtifactKind(_StringEnum):
    REGISTRY = "registry"
    DETAIL = "detail"
    PDF = "pdf"
    HH_SEARCH = "hh_search"
    HH_DETAIL = "hh_detail"


class SourceKind(_StringEnum):
    CAPTURE = "capture"
    FIXTURE = "fixture"


class ParserMode(_StringEnum):
    JSON_API = "json_api"
    HTML = "html"
    BROWSER = "browser"


# Semantic aliases keep the public vocabulary close to the design document.
SyncMode = RunMode
LifecycleEventType = EventType
LifecycleEventKind = EventType
ContentHash = SHA256Digest

RUN_MODE_VALUES: tuple[str, ...] = tuple(item.value for item in RunMode)
RUN_STATUS_VALUES: tuple[str, ...] = tuple(item.value for item in RunStatus)
EVENT_TYPE_VALUES: tuple[str, ...] = tuple(item.value for item in EventType)
LIFECYCLE_STATE_VALUES: tuple[str, ...] = tuple(item.value for item in LifecycleState)
ARTIFACT_KIND_VALUES: tuple[str, ...] = tuple(item.value for item in ArtifactKind)
SOURCE_KIND_VALUES: tuple[str, ...] = tuple(item.value for item in SourceKind)
PARSER_MODE_VALUES: tuple[str, ...] = tuple(item.value for item in ParserMode)

ENUM_VALUES: dict[str, tuple[str, ...]] = {
    "mode": RUN_MODE_VALUES,
    "status": RUN_STATUS_VALUES,
    "event_type": EVENT_TYPE_VALUES,
    "lifecycle_state": LIFECYCLE_STATE_VALUES,
    "artifact_kind": ARTIFACT_KIND_VALUES,
    "source_kind": SOURCE_KIND_VALUES,
    "parser_mode": PARSER_MODE_VALUES,
}


# ---------------------------------------------------------------------------
# Small validation/serialization helpers
# ---------------------------------------------------------------------------


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _optional_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string or null")
    return value


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{field_name} must be a non-empty string")
    return value


def _positive_id(value: Any, field_name: str = "enbek_id") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TypeError(f"{field_name} must be an integer greater than zero")
    return value


def _nonnegative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeError(f"{field_name} must be a non-negative integer")
    return value


def _optional_number(value: Any, field_name: str) -> Number | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a number or null")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite")
    return value


def _optional_date(value: Any, field_name: str) -> ISODate | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise TypeError(f"{field_name} must use YYYY-MM-DD or be null")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} is not a calendar date") from exc
    return value


def _optional_datetime(value: Any, field_name: str) -> ISODateTime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field_name} must be timezone-aware UTC")
        utc_value = value.astimezone(timezone.utc)
        if utc_value.microsecond:
            raise ValueError(f"{field_name} must not contain fractional seconds")
        return utc_value.strftime("%Y-%m-%dT%H:%M:%SZ")
    if not isinstance(value, str) or not _DATETIME_RE.fullmatch(value):
        raise TypeError(f"{field_name} must use YYYY-MM-DDTHH:mm:ssZ or be null")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} is not a valid date-time") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{field_name} must be UTC and end with Z")
    return value


def _optional_sha(value: Any, field_name: str) -> SHA256Digest | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a 64-character hexadecimal SHA-256 digest")
    return value.lower()


EnumType = TypeVar("EnumType", bound=Enum)


def _enum_value(value: Any, enum_type: type[EnumType], field_name: str) -> EnumType:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        values = ", ".join(repr(item.value) for item in enum_type)
        raise ValueError(f"{field_name} must be one of {values}") from exc


def _json_copy(value: Any, path: str = "$") -> JSONValue:
    """Copy and validate a JSON-compatible value without external packages."""

    if value is None or isinstance(value, (str, bool, int)):
        return copy.deepcopy(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite JSON number")
        return value
    if isinstance(value, Mapping):
        result: dict[str, JSONValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} contains a non-string object key")
            result[key] = _json_copy(item, f"{path}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [_json_copy(item, f"{path}[{index}]") for index, item in enumerate(value)]
    raise TypeError(f"{path} contains unsupported JSON value {type(value).__name__}")


def _serialize(value: Any) -> JSONValue:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        if isinstance(value, datetime):
            if value.tzinfo is not None:
                return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            return value.isoformat()
        return value.isoformat()
    if is_dataclass(value):
        if isinstance(value, SerializableModel):
            return value.to_dict()
        return {
            item.name: _serialize(getattr(value, item.name))
            for item in fields(value)
            if item.init or hasattr(value, item.name)
        }
    if isinstance(value, Mapping):
        return {str(key): _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite values cannot be serialized as JSON")
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _serialize(value.to_dict())
    raise TypeError(f"cannot serialize value of type {type(value).__name__}")


def _coerce_model(value: Any, model_type: type[Any], field_name: str) -> Any:
    if isinstance(value, model_type):
        return value
    if isinstance(value, Mapping):
        unknown = unknown_fields(value, model_type)
        if unknown:
            joined = ", ".join(unknown)
            raise ValueError(f"{field_name} contains unknown fields: {joined}")
        return model_type(**dict(value))
    raise TypeError(f"{field_name} must be {model_type.__name__} or an object")


class SerializableModel:
    """Mixin providing deterministic, complete JSON-compatible serialization."""

    __schema_fields__: ClassVar[tuple[str, ...]] = ()

    @classmethod
    def field_names(cls) -> tuple[str, ...]:
        declared = getattr(cls, "__schema_fields__", ())
        if declared:
            return declared
        return tuple(item.name for item in fields(cls))

    @classmethod
    def unknown_fields(cls, document: Mapping[str, Any]) -> tuple[str, ...]:
        return unknown_fields(document, cls)

    def to_dict(self) -> dict[str, JSONValue]:
        result: dict[str, JSONValue] = {}
        for name in self.field_names():
            if not hasattr(self, name):
                continue
            result[name] = _serialize(getattr(self, name))
        return result

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        ) + "\n"

    def to_json_bytes(self) -> bytes:
        return self.to_json().encode("utf-8")

    @classmethod
    def from_dict(cls: type["ModelType"], document: Mapping[str, Any]) -> "ModelType":
        if not isinstance(document, Mapping):
            raise TypeError(f"{cls.__name__} requires an object")
        unknown = unknown_fields(document, cls)
        if unknown:
            raise ValueError(
                f"{cls.__name__} contains unknown fields: {', '.join(unknown)}"
            )
        return cls(**dict(document))


ModelType = TypeVar("ModelType", bound=SerializableModel)


def unknown_fields(
    document: Mapping[str, Any],
    model_or_fields: type[SerializableModel] | tuple[str, ...] | list[str],
) -> tuple[str, ...]:
    """Return keys not declared by a model/schema, preserving input order."""

    if not isinstance(document, Mapping):
        raise TypeError("document must be a mapping")
    if isinstance(model_or_fields, type):
        allowed = set(model_or_fields.field_names())
    else:
        allowed = set(model_or_fields)
    return tuple(str(key) for key in document if key not in allowed)


def find_unknown_fields(
    document: Mapping[str, Any],
    model_type: type[SerializableModel] = None,  # type: ignore[assignment]
    path: str = "$",
) -> tuple[str, ...]:
    """Find top-level unknown keys, with optional recursive canonical paths."""

    if model_type is None:
        model_type = NormalizedRecord
    result = [f"{path}.{key}" for key in unknown_fields(document, model_type)]
    if model_type is NormalizedRecord:
        nested_specs = (
            ("categories", Categories, document.get("categories")),
            ("source", SourceReference, document.get("source")),
            ("hashes", Hashes, document.get("hashes")),
            ("timestamps", Timestamps, document.get("timestamps")),
        )
        for name, nested_type, nested_value in nested_specs:
            if isinstance(nested_value, Mapping):
                result.extend(
                    f"{path}.{name}.{key}"
                    for key in unknown_fields(nested_value, nested_type)
                )
                if name == "categories":
                    official = nested_value.get("official")
                    if isinstance(official, Mapping):
                        result.extend(
                            f"{path}.categories.official.{key}"
                            for key in unknown_fields(official, OfficialCategory)
                        )
    return tuple(result)


# ---------------------------------------------------------------------------
# Source and canonical-domain models
# ---------------------------------------------------------------------------


@dataclass
class Selector(SerializableModel):
    """A recorded source selector (JSON path, CSS, XPath, or text selector)."""

    type: str
    value: str

    __schema_fields__: ClassVar[tuple[str, ...]] = ("type", "value")

    def __post_init__(self) -> None:
        self.type = _required_text(self.type, "selector.type")
        self.value = _required_text(self.value, "selector.value")

    def __getitem__(self, key: str) -> str:
        if key == "type":
            return self.type
        if key == "value":
            return self.value
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default


@dataclass
class SourceProfile(SerializableModel):
    """A validated, replayable source/API capture contract."""

    schema_version: str = SOURCE_PROFILE_SCHEMA_VERSION
    source_kind: SourceKind | str = SourceKind.CAPTURE
    confirmed: bool = True
    parser_mode: ParserMode | str = ParserMode.JSON_API
    registry_url: str = ""
    method: str = "GET"
    query: Mapping[str, JSONValue] = field(default_factory=dict)
    body: JSONValue = None
    headers: Mapping[str, str] = field(default_factory=dict)
    pagination: Mapping[str, JSONValue] = field(default_factory=dict)
    detail_link_selector: Selector = field(
        default_factory=lambda: Selector("json_path", "$.detail_url")
    )
    pdf_link_selector: Selector = field(
        default_factory=lambda: Selector("json_path", "$.pdf_url")
    )
    capture_id: str | None = None
    captured_at: str | None = None

    __schema_fields__: ClassVar[tuple[str, ...]] = (
        "schema_version",
        "kind",
        "confirmed",
        "parser_mode",
        "registry",
        "pagination",
        "detail_link_selector",
        "pdf_link_selector",
        "capture_id",
        "captured_at",
    )

    def __post_init__(self) -> None:
        self.schema_version = _required_text(self.schema_version, "schema_version")
        self.source_kind = _enum_value(self.source_kind, SourceKind, "kind")
        if not isinstance(self.confirmed, bool):
            raise TypeError("confirmed must be boolean")
        self.parser_mode = _enum_value(self.parser_mode, ParserMode, "parser_mode")
        self.registry_url = _required_text(self.registry_url, "registry_url")
        self.method = _required_text(self.method, "method").upper()
        if not isinstance(self.query, Mapping):
            raise TypeError("query must be an object")
        self.query = _json_copy(self.query, "$.registry.query")  # type: ignore[assignment]
        self.body = _json_copy(self.body, "$.registry.body")
        if not isinstance(self.headers, Mapping):
            raise TypeError("headers must be an object")
        normalized_headers: dict[str, str] = {}
        for key, value in self.headers.items():
            normalized_headers[_required_text(key, "header name")] = _required_text(
                value, f"header {key!r}"
            )
        self.headers = normalized_headers
        if not isinstance(self.pagination, Mapping):
            raise TypeError("pagination must be an object")
        self.pagination = _json_copy(self.pagination, "$.pagination")  # type: ignore[assignment]
        self.detail_link_selector = _coerce_model(
            self.detail_link_selector, Selector, "detail_link_selector"
        )
        self.pdf_link_selector = _coerce_model(
            self.pdf_link_selector, Selector, "pdf_link_selector"
        )
        self.capture_id = _optional_text(self.capture_id, "capture_id")
        self.captured_at = _optional_text(self.captured_at, "captured_at")

    @property
    def kind(self) -> SourceKind:
        return self.source_kind  # type: ignore[return-value]

    @property
    def adapter(self) -> ParserMode:
        return self.parser_mode  # type: ignore[return-value]

    @property
    def is_fixture(self) -> bool:
        return self.source_kind == SourceKind.FIXTURE

    @property
    def registry_request(self) -> dict[str, JSONValue]:
        return {
            "url": self.registry_url,
            "method": self.method,
            "query": copy.deepcopy(dict(self.query)),
            "body": copy.deepcopy(self.body),
            "headers": dict(self.headers),
        }

    @property
    def selectors(self) -> dict[str, dict[str, str]]:
        return {
            "detail": self.detail_link_selector.to_dict(),
            "pdf": self.pdf_link_selector.to_dict(),
        }

    def to_dict(self) -> dict[str, JSONValue]:
        result: dict[str, JSONValue] = {
            "schema_version": self.schema_version,
            "kind": self.source_kind.value,
            "confirmed": self.confirmed,
            "parser_mode": self.parser_mode.value,
            "registry": {
                "url": self.registry_url,
                "method": self.method,
                "query": _serialize(dict(self.query)),
                "body": _serialize(self.body),
                "headers": _serialize(dict(self.headers)),
            },
            "pagination": _serialize(dict(self.pagination)),
            "detail_link_selector": self.detail_link_selector.to_dict(),
            "pdf_link_selector": self.pdf_link_selector.to_dict(),
        }
        if self.capture_id is not None:
            result["capture_id"] = self.capture_id
        if self.captured_at is not None:
            result["captured_at"] = self.captured_at
        return result

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "SourceProfile":
        if not isinstance(document, Mapping):
            raise TypeError("SourceProfile requires an object")
        allowed = {
            "schema_version",
            "kind",
            "source_kind",
            "confirmed",
            "parser_mode",
            "registry",
            "registry_url",
            "method",
            "query",
            "body",
            "headers",
            "pagination",
            "detail_link_selector",
            "pdf_link_selector",
            "capture_id",
            "captured_at",
        }
        unknown = tuple(str(key) for key in document if key not in allowed)
        if unknown:
            raise ValueError(f"SourceProfile contains unknown fields: {', '.join(unknown)}")
        registry = document.get("registry")
        registry_map = registry if isinstance(registry, Mapping) else {}
        detail_selector = document.get("detail_link_selector", {"type": "json_path", "value": "$.detail_url"})
        pdf_selector = document.get("pdf_link_selector", {"type": "json_path", "value": "$.pdf_url"})
        return cls(
            schema_version=document.get("schema_version", SOURCE_PROFILE_SCHEMA_VERSION),
            source_kind=document.get("source_kind", document.get("kind", SourceKind.CAPTURE)),
            confirmed=document.get("confirmed", True),
            parser_mode=document.get("parser_mode", ParserMode.JSON_API),
            registry_url=registry_map.get("url", document.get("registry_url", "")),
            method=registry_map.get("method", document.get("method", "GET")),
            query=registry_map.get("query", document.get("query", {})),
            body=registry_map.get("body", document.get("body")),
            headers=registry_map.get("headers", document.get("headers", {})),
            pagination=document.get("pagination", registry_map.get("pagination", {})),
            detail_link_selector=_coerce_selector(detail_selector),
            pdf_link_selector=_coerce_selector(pdf_selector),
            capture_id=document.get("capture_id"),
            captured_at=document.get("captured_at"),
        )


def _coerce_selector(value: Any) -> Selector:
    return value if isinstance(value, Selector) else _coerce_model(value, Selector, "selector")


@dataclass
class RegistryEntry(SerializableModel):
    """One registry row and its stable detail-page identity."""

    enbek_id: int
    detail_page_url: str
    business_code: str | None = None

    __schema_fields__: ClassVar[tuple[str, ...]] = (
        "enbek_id",
        "detail_page_url",
        "business_code",
    )

    def __post_init__(self) -> None:
        self.enbek_id = _positive_id(self.enbek_id)
        self.detail_page_url = _required_text(self.detail_page_url, "detail_page_url")
        self.business_code = _optional_text(self.business_code, "business_code")

    @property
    def code(self) -> str | None:
        """Normalized-name alias for the registry's Business_Code."""

        return self.business_code


@dataclass
class RegistryAttributes(SerializableModel):
    """Attributes carried from a registry row into the normalizer."""

    enbek_id: int
    code: str | None = None
    detail_page_url: str | None = None
    registry_url: str | None = None

    __schema_fields__: ClassVar[tuple[str, ...]] = (
        "enbek_id",
        "code",
        "detail_page_url",
        "registry_url",
    )

    def __post_init__(self) -> None:
        self.enbek_id = _positive_id(self.enbek_id)
        self.code = _optional_text(self.code, "code")
        self.detail_page_url = _optional_text(self.detail_page_url, "detail_page_url")
        self.registry_url = _optional_text(self.registry_url, "registry_url")


@dataclass
class ProfessionRecord(SerializableModel):
    """Published profession name and optional ORK level."""

    name: str | None = None
    ork_level: int | None = None

    __schema_fields__: ClassVar[tuple[str, ...]] = ("name", "ork_level")

    def __post_init__(self) -> None:
        self.name = _optional_text(self.name, "profession.name")
        if self.ork_level is not None:
            if isinstance(self.ork_level, bool) or not isinstance(self.ork_level, int):
                raise TypeError("profession.ork_level must be an integer or null")
            if self.ork_level < 0:
                raise ValueError("profession.ork_level must be non-negative")


@dataclass
class OkedAttributes(SerializableModel):
    """Official OKED section/division/group/class/subclass attributes."""

    section_code: str | None = None
    section_name: str | None = None
    division_code: str | None = None
    division_name: str | None = None
    group_code: str | None = None
    group_name: str | None = None
    class_code: str | None = None
    class_name: str | None = None
    subclass_code: str | None = None
    subclass_name: str | None = None

    __schema_fields__: ClassVar[tuple[str, ...]] = (
        "section_code",
        "section_name",
        "division_code",
        "division_name",
        "group_code",
        "group_name",
        "class_code",
        "class_name",
        "subclass_code",
        "subclass_name",
    )

    def __post_init__(self) -> None:
        for name in self.__schema_fields__:
            setattr(self, name, _optional_text(getattr(self, name), f"oked.{name}"))


# Uppercase alias mirrors the official acronym used by the requirements.
OKEDAttributes = OkedAttributes
OKED = OkedAttributes


@dataclass
class OfficialCategory(SerializableModel):
    """The exact source-only ``categories.official`` object shape."""

    oked: list[str] = field(default_factory=list)
    government_body: str | None = None
    ork_levels: list[int] = field(default_factory=list)
    year: Number | None = None
    version: Number | None = None
    lifecycle_state: LifecycleState | str = LifecycleState.ACTIVE

    __schema_fields__: ClassVar[tuple[str, ...]] = CATEGORIES_OFFICIAL_FIELDS

    def __post_init__(self) -> None:
        if self.oked is None:
            self.oked = []
        if not isinstance(self.oked, (list, tuple)):
            raise TypeError("categories.official.oked must be an array")
        self.oked = [
            _required_text(item, "categories.official.oked item")
            for item in self.oked
        ]
        self.government_body = _optional_text(
            self.government_body, "categories.official.government_body"
        )
        if self.ork_levels is None:
            self.ork_levels = []
        if not isinstance(self.ork_levels, (list, tuple)):
            raise TypeError("categories.official.ork_levels must be an array")
        normalized_levels: list[int] = []
        for item in self.ork_levels:
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                raise TypeError("categories.official.ork_levels must contain integers")
            normalized_levels.append(item)
        self.ork_levels = normalized_levels
        self.year = _optional_number(self.year, "categories.official.year")
        self.version = _optional_number(self.version, "categories.official.version")
        self.lifecycle_state = _enum_value(
            self.lifecycle_state, LifecycleState, "categories.official.lifecycle_state"
        )


@dataclass
class Categories(SerializableModel):
    """Container reserved for source-backed category projections."""

    official: OfficialCategory = field(default_factory=OfficialCategory)

    __schema_fields__: ClassVar[tuple[str, ...]] = ("official",)

    def __post_init__(self) -> None:
        if self.official is None:
            self.official = OfficialCategory()
        self.official = _coerce_model(self.official, OfficialCategory, "categories.official")


CategorySet = Categories


@dataclass
class SourceReference(SerializableModel):
    """Exact URLs retained from the official source."""

    registry_url: str | None = None
    page_url: str | None = None
    pdf_url: str | None = None

    __schema_fields__: ClassVar[tuple[str, ...]] = SOURCE_FIELDS

    def __post_init__(self) -> None:
        self.registry_url = _optional_text(self.registry_url, "source.registry_url")
        self.page_url = _optional_text(self.page_url, "source.page_url")
        self.pdf_url = _optional_text(self.pdf_url, "source.pdf_url")


Source = SourceReference
SourceUrls = SourceReference


@dataclass
class Hashes(SerializableModel):
    """The three content digests stored in every normalized record."""

    metadata_sha256: SHA256Digest | None = None
    pdf_sha256: SHA256Digest | None = None
    normalized_sha256: SHA256Digest | None = None

    __schema_fields__: ClassVar[tuple[str, ...]] = HASH_FIELDS

    def __post_init__(self) -> None:
        self.metadata_sha256 = _optional_sha(
            self.metadata_sha256, "hashes.metadata_sha256"
        )
        self.pdf_sha256 = _optional_sha(self.pdf_sha256, "hashes.pdf_sha256")
        self.normalized_sha256 = _optional_sha(
            self.normalized_sha256, "hashes.normalized_sha256"
        )


HashValues = Hashes
ContentHashes = Hashes


@dataclass
class Timestamps(SerializableModel):
    """Technical observation timestamps excluded from content equality/hash."""

    first_seen_at: ISODateTime | None = None
    last_seen_at: ISODateTime | None = None
    last_changed_at: ISODateTime | None = None

    __schema_fields__: ClassVar[tuple[str, ...]] = TIMESTAMP_FIELDS

    def __post_init__(self) -> None:
        for name in self.__schema_fields__:
            setattr(self, name, _optional_datetime(getattr(self, name), f"timestamps.{name}"))


TechnicalTimestamps = Timestamps
TimestampSet = Timestamps


@dataclass
class SourceAttributes(SerializableModel):
    """Structured attributes parsed from a professional-standard detail page."""

    enbek_id: int
    code: str | None = None
    title: str | None = None
    government_body: str | None = None
    description: str | None = None
    version: Number | None = None
    year: Number | None = None
    revision_date: ISODate | None = None
    oked: OkedAttributes | None = None
    professions: list[ProfessionRecord] = field(default_factory=list)
    registry_url: str | None = None
    page_url: str | None = None
    pdf_url: str | None = None

    __schema_fields__: ClassVar[tuple[str, ...]] = (
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
        "registry_url",
        "page_url",
        "pdf_url",
    )

    def __post_init__(self) -> None:
        self.enbek_id = _positive_id(self.enbek_id)
        for name in ("code", "title", "government_body", "description"):
            setattr(self, name, _optional_text(getattr(self, name), f"{name}"))
        self.version = _optional_number(self.version, "version")
        self.year = _optional_number(self.year, "year")
        self.revision_date = _optional_date(self.revision_date, "revision_date")
        if self.oked is not None:
            self.oked = _coerce_model(self.oked, OkedAttributes, "oked")
        if self.professions is None:
            self.professions = []
        if not isinstance(self.professions, (list, tuple)):
            raise TypeError("professions must be an array")
        self.professions = [
            _coerce_model(item, ProfessionRecord, "professions item")
            for item in self.professions
        ]
        self.registry_url = _optional_text(self.registry_url, "registry_url")
        self.page_url = _optional_text(self.page_url, "page_url")
        self.pdf_url = _optional_text(self.pdf_url, "pdf_url")


ProfessionalStandardAttributes = SourceAttributes
DetailAttributes = SourceAttributes


@dataclass
class NormalizedRecord(SerializableModel):
    """Canonical JSON record for one professional standard.

    All schema fields are emitted by :meth:`to_dict`.  Scalar source omissions
    are represented by ``None`` and collection omissions by ``[]``; validation
    of whether a candidate is acceptable remains a separate concern.
    """

    schema_version: str = NORMALIZED_RECORD_SCHEMA_VERSION
    enbek_id: int = 0
    code: str | None = None
    title: str | None = None
    government_body: str | None = None
    description: str | None = None
    version: Number | None = None
    year: Number | None = None
    revision_date: ISODate | None = None
    oked: OkedAttributes | None = None
    professions: list[ProfessionRecord] = field(default_factory=list)
    categories: Categories = field(default_factory=Categories)
    source: SourceReference = field(default_factory=SourceReference)
    hashes: Hashes = field(default_factory=Hashes)
    timestamps: Timestamps = field(default_factory=Timestamps)

    __schema_fields__: ClassVar[tuple[str, ...]] = NORMALIZED_RECORD_FIELDS

    def __post_init__(self) -> None:
        self.schema_version = _required_text(self.schema_version, "schema_version")
        self.enbek_id = _positive_id(self.enbek_id)
        for name in ("code", "title", "government_body", "description"):
            setattr(self, name, _optional_text(getattr(self, name), name))
        self.version = _optional_number(self.version, "version")
        self.year = _optional_number(self.year, "year")
        self.revision_date = _optional_date(self.revision_date, "revision_date")
        if self.oked is not None:
            self.oked = _coerce_model(self.oked, OkedAttributes, "oked")
        if self.professions is None:
            self.professions = []
        if not isinstance(self.professions, (list, tuple)):
            raise TypeError("professions must be an array")
        self.professions = [
            _coerce_model(item, ProfessionRecord, "professions item")
            for item in self.professions
        ]
        self.categories = _coerce_model(self.categories, Categories, "categories")
        self.source = _coerce_model(self.source, SourceReference, "source")
        self.hashes = _coerce_model(self.hashes, Hashes, "hashes")
        self.timestamps = _coerce_model(self.timestamps, Timestamps, "timestamps")

    @property
    def OKED(self) -> OkedAttributes | None:
        """Acronym-preserving alias for callers using the glossary spelling."""

        return self.oked

    @property
    def business_code(self) -> str | None:
        return self.code


# ---------------------------------------------------------------------------
# Snapshots, lifecycle, history and reports
# ---------------------------------------------------------------------------


@dataclass
class ErrorInfo(SerializableModel):
    """Serializable diagnostic common denominator for candidate/run errors."""

    stage: str = ""
    reason: str = ""
    url: str | None = None
    path: str | None = None
    attempts: int | None = None
    status: int | None = None
    fields: list[str] = field(default_factory=list)

    __schema_fields__: ClassVar[tuple[str, ...]] = (
        "stage",
        "reason",
        "url",
        "path",
        "attempts",
        "status",
        "fields",
    )

    def __post_init__(self) -> None:
        self.stage = _required_text(self.stage, "error.stage")
        self.reason = _required_text(self.reason, "error.reason")
        self.url = _optional_text(self.url, "error.url")
        self.path = _optional_text(self.path, "error.path")
        if self.attempts is not None:
            self.attempts = _nonnegative_int(self.attempts, "error.attempts")
        if self.status is not None:
            self.status = _nonnegative_int(self.status, "error.status")
        if self.fields is None:
            self.fields = []
        if not isinstance(self.fields, (list, tuple)):
            raise TypeError("error.fields must be an array")
        self.fields = [_required_text(item, "error.fields item") for item in self.fields]


@dataclass
class RawArtifactRef(SerializableModel):
    """Reference to a raw artifact staged by a candidate."""

    logical_key: str = ""
    physical_path: str = ""
    kind: ArtifactKind | str = ArtifactKind.REGISTRY
    source_url: str | None = None
    enbek_id: int | None = None

    __schema_fields__: ClassVar[tuple[str, ...]] = (
        "logical_key",
        "physical_path",
        "kind",
        "source_url",
        "enbek_id",
    )

    def __post_init__(self) -> None:
        self.logical_key = _required_text(self.logical_key, "raw.logical_key")
        self.physical_path = _required_text(self.physical_path, "raw.physical_path")
        self.kind = _enum_value(self.kind, ArtifactKind, "raw.kind")
        self.source_url = _optional_text(self.source_url, "raw.source_url")
        if self.enbek_id is not None:
            self.enbek_id = _positive_id(self.enbek_id)


@dataclass
class LifecycleStatus(SerializableModel):
    """Sidecar lifecycle state stored separately from record content."""

    state: LifecycleState | str = LifecycleState.ACTIVE
    missing_count: int = 0

    __schema_fields__: ClassVar[tuple[str, ...]] = ("state", "missing_count")

    def __post_init__(self) -> None:
        self.state = _enum_value(self.state, LifecycleState, "lifecycle.state")
        self.missing_count = _nonnegative_int(self.missing_count, "missing_count")


LifecycleEntry = LifecycleStatus


def _coerce_record_map(value: Any, field_name: str) -> dict[int, NormalizedRecord]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be an object keyed by Enbek_ID")
    result: dict[int, NormalizedRecord] = {}
    for raw_id, raw_record in value.items():
        if isinstance(raw_id, bool):
            raise TypeError(f"{field_name} contains a boolean ID")
        try:
            record_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise TypeError(f"{field_name} contains a non-integer ID") from exc
        record = _coerce_model(raw_record, NormalizedRecord, f"{field_name}[{raw_id}]")
        if record.enbek_id != record_id:
            raise ValueError(f"{field_name}[{raw_id}] does not match record.enbek_id")
        result[record_id] = record
    return result


def _coerce_lifecycle_map(value: Any) -> dict[int, LifecycleStatus]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("lifecycle must be an object keyed by Enbek_ID")
    result: dict[int, LifecycleStatus] = {}
    for raw_id, raw_status in value.items():
        try:
            record_id = _positive_id(int(raw_id), "lifecycle Enbek_ID")
        except (TypeError, ValueError) as exc:
            raise TypeError("lifecycle contains a non-positive integer ID") from exc
        result[record_id] = _coerce_model(raw_status, LifecycleStatus, f"lifecycle[{raw_id}]")
    return result


@dataclass
class CandidateSnapshot(SerializableModel):
    """Collected snapshot before validation and atomic publication."""

    snapshot_id: str = ""
    run_id: str = ""
    registry_url: str = ""
    collected_at: ISODateTime | None = None
    complete: bool = False
    records: dict[int, NormalizedRecord] = field(default_factory=dict)
    raw_artifact_refs: list[RawArtifactRef] = field(default_factory=list)
    errors: list[ErrorInfo] = field(default_factory=list)

    __schema_fields__: ClassVar[tuple[str, ...]] = (
        "snapshot_id",
        "run_id",
        "registry_url",
        "collected_at",
        "complete",
        "records",
        "raw_artifact_refs",
        "errors",
    )

    def __post_init__(self) -> None:
        self.snapshot_id = _required_text(self.snapshot_id, "snapshot_id")
        self.run_id = _required_text(self.run_id, "run_id")
        self.registry_url = _required_text(self.registry_url, "registry_url")
        self.collected_at = _optional_datetime(self.collected_at, "collected_at")
        if not isinstance(self.complete, bool):
            raise TypeError("complete must be boolean")
        self.records = _coerce_record_map(self.records, "records")
        if self.raw_artifact_refs is None:
            self.raw_artifact_refs = []
        if not isinstance(self.raw_artifact_refs, (list, tuple)):
            raise TypeError("raw_artifact_refs must be an array")
        self.raw_artifact_refs = [
            _coerce_model(item, RawArtifactRef, "raw_artifact_refs item")
            for item in self.raw_artifact_refs
        ]
        if self.errors is None:
            self.errors = []
        if not isinstance(self.errors, (list, tuple)):
            raise TypeError("errors must be an array")
        self.errors = [
            item if isinstance(item, ErrorInfo) else _coerce_model(item, ErrorInfo, "errors item")
            for item in self.errors
        ]


@dataclass
class SnapshotState(SerializableModel):
    """Last successfully committed snapshot plus lifecycle sidecar."""

    generation_id: str = ""
    records: dict[int, NormalizedRecord] = field(default_factory=dict)
    lifecycle: dict[int, LifecycleStatus] = field(default_factory=dict)
    source_registry_url: str = ""
    committed_run_id: str = ""
    content_manifest_hash: SHA256Digest | None = None

    __schema_fields__: ClassVar[tuple[str, ...]] = (
        "generation_id",
        "records",
        "lifecycle",
        "source_registry_url",
        "committed_run_id",
        "content_manifest_hash",
    )

    def __post_init__(self) -> None:
        self.generation_id = _required_text(self.generation_id, "generation_id")
        self.records = _coerce_record_map(self.records, "records")
        self.lifecycle = _coerce_lifecycle_map(self.lifecycle)
        self.source_registry_url = _required_text(
            self.source_registry_url, "source_registry_url"
        )
        self.committed_run_id = _required_text(self.committed_run_id, "committed_run_id")
        self.content_manifest_hash = _optional_sha(
            self.content_manifest_hash, "content_manifest_hash"
        )


CurrentSnapshot = SnapshotState
Baseline = SnapshotState


@dataclass
class FieldLevelDiff(SerializableModel):
    """One deterministic old/new leaf change."""

    path: str = "$"
    old: JSONValue = None
    new: JSONValue = None

    __schema_fields__: ClassVar[tuple[str, ...]] = ("path", "old", "new")

    def __post_init__(self) -> None:
        self.path = _required_text(self.path, "change.path")
        self.old = _json_copy(self.old, f"{self.path}.old")
        self.new = _json_copy(self.new, f"{self.path}.new")


FieldDiff = FieldLevelDiff


@dataclass
class LifecycleEvent(SerializableModel):
    """A single accepted lifecycle transition for one Enbek_ID."""

    event_id: str = ""
    run_id: str = ""
    enbek_id: int = 0
    code: str | None = None
    event_type: EventType | str = EventType.NEW
    detected_at: ISODateTime | None = None
    old_hashes: Hashes | None = None
    new_hashes: Hashes | None = None
    previous_state: LifecycleState | str | None = None
    new_state: LifecycleState | str = LifecycleState.ACTIVE
    missing_count: int = 0
    changes: list[FieldLevelDiff] = field(default_factory=list)
    source: SourceReference = field(default_factory=SourceReference)

    __schema_fields__: ClassVar[tuple[str, ...]] = (
        "event_id",
        "run_id",
        "enbek_id",
        "code",
        "event_type",
        "detected_at",
        "old_hashes",
        "new_hashes",
        "previous_state",
        "new_state",
        "missing_count",
        "changes",
        "source",
    )

    def __post_init__(self) -> None:
        self.event_id = _required_text(self.event_id, "event_id")
        self.run_id = _required_text(self.run_id, "run_id")
        self.enbek_id = _positive_id(self.enbek_id)
        self.code = _optional_text(self.code, "code")
        self.event_type = _enum_value(self.event_type, EventType, "event_type")
        self.detected_at = _optional_datetime(self.detected_at, "detected_at")
        if self.old_hashes is not None:
            self.old_hashes = _coerce_model(self.old_hashes, Hashes, "old_hashes")
        if self.new_hashes is not None:
            self.new_hashes = _coerce_model(self.new_hashes, Hashes, "new_hashes")
        if self.previous_state is not None:
            self.previous_state = _enum_value(
                self.previous_state, LifecycleState, "previous_state"
            )
        self.new_state = _enum_value(self.new_state, LifecycleState, "new_state")
        self.missing_count = _nonnegative_int(self.missing_count, "missing_count")
        if self.changes is None:
            self.changes = []
        if not isinstance(self.changes, (list, tuple)):
            raise TypeError("changes must be an array")
        self.changes = [
            _coerce_model(item, FieldLevelDiff, "changes item") for item in self.changes
        ]
        self.source = _coerce_model(self.source, SourceReference, "event.source")


Event = LifecycleEvent


@dataclass
class RunReport(SerializableModel):
    """Machine-readable result of one successful or failed run."""

    run_id: str = ""
    started_at: ISODateTime | None = None
    finished_at: ISODateTime | None = None
    duration: float = 0.0
    registry_url: str = ""
    mode: RunMode | str = RunMode.SYNC
    status: RunStatus | str = RunStatus.SUCCESS
    total: int = 0
    baseline: int = 0
    new: int = 0
    updated: int = 0
    missing: int = 0
    removed: int = 0
    restored: int = 0
    unchanged: int = 0
    errors: list[ErrorInfo] = field(default_factory=list)
    planned_events: list[LifecycleEvent] = field(default_factory=list)

    __schema_fields__: ClassVar[tuple[str, ...]] = (
        "run_id",
        "started_at",
        "finished_at",
        "duration",
        "registry_url",
        "mode",
        "status",
        "total",
        "baseline",
        "new",
        "updated",
        "missing",
        "removed",
        "restored",
        "unchanged",
        "errors",
        "planned_events",
    )

    COUNT_FIELDS: ClassVar[tuple[str, ...]] = (
        "total",
        "baseline",
        "new",
        "updated",
        "missing",
        "removed",
        "restored",
        "unchanged",
    )

    def __post_init__(self) -> None:
        self.run_id = _required_text(self.run_id, "run_id")
        self.started_at = _optional_datetime(self.started_at, "started_at")
        self.finished_at = _optional_datetime(self.finished_at, "finished_at")
        if isinstance(self.duration, bool) or not isinstance(self.duration, (int, float)):
            raise TypeError("duration must be a number")
        if not math.isfinite(float(self.duration)) or self.duration < 0:
            raise ValueError("duration must be a finite non-negative number")
        self.duration = float(self.duration)
        self.registry_url = _required_text(self.registry_url, "registry_url")
        self.mode = _enum_value(self.mode, RunMode, "mode")
        self.status = _enum_value(self.status, RunStatus, "status")
        for name in self.COUNT_FIELDS:
            setattr(self, name, _nonnegative_int(getattr(self, name), name))
        if self.errors is None:
            self.errors = []
        if not isinstance(self.errors, (list, tuple)):
            raise TypeError("errors must be an array")
        self.errors = [
            item if isinstance(item, ErrorInfo) else _coerce_model(item, ErrorInfo, "errors item")
            for item in self.errors
        ]
        if self.planned_events is None:
            self.planned_events = []
        if not isinstance(self.planned_events, (list, tuple)):
            raise TypeError("planned_events must be an array")
        self.planned_events = [
            _coerce_model(item, LifecycleEvent, "planned_events item")
            for item in self.planned_events
        ]


@dataclass
class RawArtifact(SerializableModel):
    """Immutable source response/PDF metadata stored in the raw manifest."""

    logical_key: str = ""
    kind: ArtifactKind | str = ArtifactKind.REGISTRY
    physical_path: str = ""
    source_url: str = ""
    fetched_at: ISODateTime | None = None
    enbek_id: int | None = None
    http_status: int | None = None
    content_type: str | None = None
    byte_length: int = 0
    sha256: SHA256Digest | None = None
    immutable: bool = True

    __schema_fields__: ClassVar[tuple[str, ...]] = (
        "logical_key",
        "kind",
        "physical_path",
        "source_url",
        "fetched_at",
        "enbek_id",
        "http_status",
        "content_type",
        "byte_length",
        "sha256",
        "immutable",
    )

    def __post_init__(self) -> None:
        self.logical_key = _required_text(self.logical_key, "logical_key")
        self.kind = _enum_value(self.kind, ArtifactKind, "kind")
        self.physical_path = _required_text(self.physical_path, "physical_path")
        self.source_url = _required_text(self.source_url, "source_url")
        self.fetched_at = _optional_datetime(self.fetched_at, "fetched_at")
        if self.enbek_id is not None:
            self.enbek_id = _positive_id(self.enbek_id)
        if self.http_status is not None:
            self.http_status = _nonnegative_int(self.http_status, "http_status")
        self.content_type = _optional_text(self.content_type, "content_type")
        self.byte_length = _nonnegative_int(self.byte_length, "byte_length")
        self.sha256 = _optional_sha(self.sha256, "sha256")
        if self.immutable is not True:
            raise ValueError("raw artifacts must be immutable")


# ---------------------------------------------------------------------------
# Public aliases and exports
# ---------------------------------------------------------------------------


RawArtifactReference = RawArtifactRef
SnapshotLifecycle = LifecycleStatus
Normalized = NormalizedRecord

__all__ = [
    "ArtifactKind",
    "ARTIFACT_KIND_VALUES",
    "Baseline",
    "CATEGORIES_OFFICIAL_FIELDS",
    "CategorySet",
    "CandidateSnapshot",
    "ContentHash",
    "ContentHashes",
    "CurrentSnapshot",
    "DetailAttributes",
    "ENUM_VALUES",
    "ErrorInfo",
    "Event",
    "EventType",
    "EVENT_TYPE_VALUES",
    "FieldDiff",
    "FieldLevelDiff",
    "HASH_FIELDS",
    "Hashes",
    "HashValues",
    "ISODate",
    "ISODateTime",
    "LifecycleEntry",
    "LifecycleEvent",
    "LifecycleEventKind",
    "LifecycleEventType",
    "LifecycleState",
    "LIFECYCLE_STATE_VALUES",
    "LifecycleStatus",
    "MISSING_COLLECTION",
    "MISSING_SCALAR",
    "MODEL_SCHEMA_FIELDS",
    "NORMALIZED_RECORD_FIELDS",
    "NORMALIZED_RECORD_REQUIRED_FIELDS",
    "NORMALIZED_RECORD_SCHEMA",
    "NORMALIZED_RECORD_SCHEMA_VERSION",
    "Normalized",
    "NormalizedRecord",
    "Number",
    "OKED",
    "OKEDAttributes",
    "OkedAttributes",
    "PARSER_MODE_VALUES",
    "ParserMode",
    "ProfessionalStandardAttributes",
    "RawArtifact",
    "RawArtifactRef",
    "RawArtifactReference",
    "RegistryAttributes",
    "RegistryEntry",
    "RunMode",
    "RUN_MODE_VALUES",
    "RunReport",
    "RunStatus",
    "RUN_STATUS_VALUES",
    "SCHEMA_VERSION",
    "Selector",
    "SnapshotLifecycle",
    "SnapshotState",
    "SOURCE_FIELDS",
    "SOURCE_KIND_VALUES",
    "SOURCE_PROFILE_SCHEMA_VERSION",
    "Source",
    "SourceAttributes",
    "SourceKind",
    "SourceProfile",
    "SourceReference",
    "SourceUrls",
    "SyncMode",
    "TechnicalTimestamps",
    "TimestampSet",
    "TIMESTAMP_FIELDS",
    "Timestamps",
    "JSONMapping",
    "JSONScalar",
    "JSONValue",
    "SHA256Digest",
    "unknown_fields",
    "find_unknown_fields",
]
