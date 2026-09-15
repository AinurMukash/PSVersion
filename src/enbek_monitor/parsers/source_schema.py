"""Shared source-response and parser contracts.

This module defines the boundary between source transports and pure parsers.
It deliberately does not perform network or filesystem work: callers pass the
explicit request URL and :class:`~enbek_monitor.network.SourceResponse` into a
parser, while JSON/HTML adapters only decode an already received response.

The source schema distinguishes fields that are absent, explicitly ``null``,
empty, or of the wrong type.  A parser may collect all such violations and use
:func:`build_parse_error` to return one structured :class:`ParseError` instead
of emitting a partial domain object.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from json import JSONDecodeError
from typing import Any, ClassVar, Protocol, TypeAlias, runtime_checkable
from urllib.parse import urlsplit

from ..errors import ParseError
from ..models import RegistryEntry, SourceAttributes
from ..network import SourceResponse


JSONScalar: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]


class ResponseFormat(str, Enum):
    """Source representation understood by the parser adapters."""

    JSON = "json"
    HTML = "html"


class SourceFieldState(str, Enum):
    """Presence state used before a parser applies field-specific rules.

    ``EMPTY`` is intentionally separate from ``NULL`` because empty strings or
    collections may be valid for some source fields and invalid for others.
    ``TYPE_INVALID`` means the value cannot satisfy the expected source type.
    """

    ABSENT = "absent"
    NULL = "null"
    EMPTY = "empty"
    VALUE = "value"
    TYPE_INVALID = "type_invalid"


# Readable aliases for callers that use the shorter terminology from the
# requirements or from a schema implementation.
FieldState = SourceFieldState
FieldPresence = SourceFieldState


@dataclass(frozen=True, slots=True)
class SourceField:
    """A source value together with its exact diagnostic path and state."""

    path: str
    state: SourceFieldState
    value: Any = None

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path.strip():
            raise ValueError("source field path must be non-empty")
        if not isinstance(self.state, SourceFieldState):
            object.__setattr__(self, "state", SourceFieldState(self.state))

    @property
    def is_missing(self) -> bool:
        return self.state is SourceFieldState.ABSENT

    @property
    def is_null(self) -> bool:
        return self.state is SourceFieldState.NULL

    @property
    def is_empty(self) -> bool:
        return self.state is SourceFieldState.EMPTY

    @property
    def is_type_invalid(self) -> bool:
        return self.state is SourceFieldState.TYPE_INVALID


_ABSENT = object()


def inspect_field(
    value: Any = _ABSENT,
    *,
    path: str = "$",
    expected_types: tuple[type[Any], ...] = (),
) -> SourceField:
    """Classify a raw source value without coercing or contacting a source.

    ``value`` defaults to an internal absent sentinel.  Empty strings (after
    whitespace inspection), empty lists and empty mappings are classified as
    ``EMPTY``.  If ``expected_types`` is provided, a non-null/non-empty value
    of another type is ``TYPE_INVALID``.  ``bool`` is not silently accepted as
    an integer by this helper; parsers can enforce that distinction explicitly.
    """

    if value is _ABSENT:
        return SourceField(path, SourceFieldState.ABSENT)
    if value is None:
        return SourceField(path, SourceFieldState.NULL, None)
    if isinstance(value, str) and not value.strip():
        return SourceField(path, SourceFieldState.EMPTY, value)
    if isinstance(value, (list, tuple, Mapping)) and not value:
        return SourceField(path, SourceFieldState.EMPTY, value)
    if expected_types and not isinstance(value, expected_types):
        return SourceField(path, SourceFieldState.TYPE_INVALID, value)
    return SourceField(path, SourceFieldState.VALUE, value)


def inspect_mapping_field(
    mapping: Mapping[str, Any],
    name: str,
    *,
    path: str,
    expected_types: tuple[type[Any], ...] = (),
) -> SourceField:
    """Inspect a named mapping field while retaining an exact source path."""

    value = mapping[name] if name in mapping else _ABSENT
    return inspect_field(value, path=f"{path}.{name}", expected_types=expected_types)


@dataclass(frozen=True, slots=True)
class SourceSchemaViolation:
    """One collected source-schema violation.

    ``state`` records whether the violation came from an absent field, an
    empty required value, an explicit null where a value was required, or a
    type/format mismatch.  ``build_parse_error`` maps these states onto the
    missing/invalid field lists supported by :class:`ParseError`.
    """

    path: str
    reason: str
    state: SourceFieldState = SourceFieldState.TYPE_INVALID

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path.strip():
            raise ValueError("violation path must be non-empty")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("violation reason must be non-empty")
        if not isinstance(self.state, SourceFieldState):
            object.__setattr__(self, "state", SourceFieldState(self.state))

    @property
    def is_missing(self) -> bool:
        return self.state is SourceFieldState.ABSENT


class SourceUrlError(ValueError):
    """Raised only by local URL-contract helpers, never by network code."""


def is_absolute_source_url(value: Any, *, allow_fixture: bool = False) -> bool:
    """Return whether ``value`` is an absolute HTTP(S) source URL.

    Fixture transports may opt into the ``fixture://`` scheme.  No URL is
    normalized or reconstructed here; callers retain the exact supplied text.
    """

    if not isinstance(value, str) or not value or value != value.strip():
        return False
    parsed = urlsplit(value)
    schemes = {"http", "https"}
    if allow_fixture:
        schemes.add("fixture")
    return parsed.scheme in schemes and bool(parsed.netloc) and not parsed.username and not parsed.password


def require_absolute_source_url(
    value: Any,
    *,
    path: str,
    allow_fixture: bool = False,
) -> str:
    """Validate and return an exact absolute source URL without rewriting it."""

    if not is_absolute_source_url(value, allow_fixture=allow_fixture):
        raise SourceUrlError(f"{path}: required absolute source URL")
    return value


@dataclass(frozen=True, slots=True)
class RegistryFieldPaths:
    """Canonical paths used by JSON and HTML registry adapters.

    Braced ``{index}`` portions are templates and are replaced by parsers when
    reporting a concrete item violation.
    """

    json_items: str = "$.data.items"
    json_enbek_id: str = "$.data.items[{index}].enbek_id"
    json_detail_url: str = "$.data.items[{index}].detail_url"
    json_business_code: str = "$.data.items[{index}].code"
    json_next: str = "$.data.next"
    json_page: str = "$.data.page"
    json_page_size: str = "$.data.page_size"
    json_total: str = "$.data.total"
    html_items: str = "$.registry-entry"
    html_enbek_id: str = "$.registry-entry[{index}].data-enbek-id"
    html_detail_url: str = "$.registry-entry[{index}].data-detail-url"
    html_business_code: str = "$.registry-entry[{index}].data-business-code"
    html_next: str = "$.main#registry.data-next"
    html_page: str = "$.main#registry.data-page"
    html_page_size: str = "$.main#registry.data-page-size"
    html_total: str = "$.main#registry.data-total"


@dataclass(frozen=True, slots=True)
class DetailFieldPaths:
    """Canonical paths used by JSON and HTML detail adapters."""

    json_root: str = "$.data"
    json_enbek_id: str = "$.data.enbek_id"
    json_code: str = "$.data.code"
    json_title: str = "$.data.title"
    json_government_body: str = "$.data.government_body"
    json_description: str = "$.data.description"
    json_version: str = "$.data.version"
    json_year: str = "$.data.year"
    json_revision_date: str = "$.data.revision_date"
    json_oked: str = "$.data.oked"
    json_professions: str = "$.data.professions"
    json_pdf_url: str = "$.data.pdf_url"
    json_pdf_published: str = "$.data.pdf_published"
    html_root: str = "$.professional-standard[0]"
    html_enbek_id: str = "$.professional-standard[0].data-enbek-id"
    html_code: str = "$.professional-standard[0].data-code"
    html_title: str = "$.professional-standard[0].data-title"
    html_government_body: str = "$.professional-standard[0].data-government-body"
    html_description: str = "$.professional-standard[0][data-field='description']"
    html_version: str = "$.professional-standard[0][data-field='version']"
    html_year: str = "$.professional-standard[0][data-field='year']"
    html_revision_date: str = "$.professional-standard[0][data-field='revision_date']"
    html_oked: str = "$.professional-standard[0][data-field='oked']"
    html_professions: str = "$.professional-standard[0][data-field='professions']"
    html_pdf_url: str = "$.professional-standard[0].data-pdf-url"
    html_pdf_published: str = "$.professional-standard[0].data-pdf-published"


REGISTRY_FIELD_PATHS = RegistryFieldPaths()
DETAIL_FIELD_PATHS = DetailFieldPaths()
# Uppercase names are convenient for parser implementations and preserve the
# terminology used in the requirements.
REGISTRY_PATHS = REGISTRY_FIELD_PATHS
DETAIL_PATHS = DETAIL_FIELD_PATHS


@dataclass(frozen=True, slots=True)
class PaginationMetadata:
    """Page-local pagination result shared by JSON and HTML parsers.

    ``next_state`` preserves absent/null/empty distinction even though all
    three normally mean terminal pagination.  A parser must reject
    ``TYPE_INVALID`` before constructing this output.
    """

    next_url: str | None = None
    terminal: bool = True
    next_state: SourceFieldState = SourceFieldState.ABSENT
    next_path: str = "$.data.next"
    page_number: int | None = None
    page_size: int | None = None
    total: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.terminal, bool):
            raise TypeError("pagination.terminal must be boolean")
        if not isinstance(self.next_state, SourceFieldState):
            object.__setattr__(self, "next_state", SourceFieldState(self.next_state))
        if self.next_state is SourceFieldState.TYPE_INVALID:
            raise ValueError("invalid next field cannot form pagination metadata")
        if self.next_url is not None:
            require_absolute_source_url(
                self.next_url,
                path=self.next_path,
                allow_fixture=True,
            )
            if self.next_state is not SourceFieldState.VALUE:
                object.__setattr__(self, "next_state", SourceFieldState.VALUE)
        elif self.next_state is SourceFieldState.VALUE:
            raise ValueError("pagination VALUE state requires next_url")
        for name in ("page_number", "page_size", "total"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError(f"pagination.{name} must be a non-negative integer or null")

    @property
    def has_next(self) -> bool:
        return self.next_url is not None and not self.terminal


@runtime_checkable
class SourceResponseWrapper(Protocol):
    """Common decoded-response contract shared by JSON and HTML wrappers."""

    source_url: str
    response: SourceResponse
    body: bytes
    headers: Mapping[str, str]
    format: ClassVar[ResponseFormat]

    @property
    def text(self) -> str:
        """Return a text view of the response body."""
        ...


ResponseWrapper = SourceResponseWrapper


@dataclass(frozen=True, slots=True)
class JsonResponseWrapper:
    """Decoded JSON response retaining exact source response bytes."""

    source_url: str
    response: SourceResponse
    payload: Any
    body: bytes
    headers: Mapping[str, str]
    format: ClassVar[ResponseFormat] = ResponseFormat.JSON

    @classmethod
    def from_response(
        cls,
        response: SourceResponse,
        *,
        source_url: str | None = None,
    ) -> "JsonResponseWrapper":
        if not isinstance(response, SourceResponse):
            raise TypeError("JSON wrapper requires enbek_monitor.network.SourceResponse")
        url = source_url or response.url
        require_absolute_source_url(url, path="$", allow_fixture=True)
        try:
            payload = json.loads(response.content.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise ParseError(
                "source response is not valid UTF-8",
                url=url,
                path="$",
                invalid_fields=("$",),
            ) from exc
        except JSONDecodeError as exc:
            raise ParseError(
                "malformed JSON syntax",
                url=url,
                path="$",
                invalid_fields=("$",),
            ) from exc
        return cls(url, response, payload, response.content, dict(response.headers))

    @property
    def text(self) -> str:
        """Return the original UTF-8 response text without reserialization."""

        return self.body.decode("utf-8")

    @property
    def document(self) -> Any:
        """Alias used by parser implementations for the decoded JSON value."""

        return self.payload


@dataclass(frozen=True, slots=True)
class HtmlResponseWrapper:
    """UTF-8 HTML response retaining exact source response bytes."""

    source_url: str
    response: SourceResponse
    text: str
    body: bytes
    headers: Mapping[str, str]
    format: ClassVar[ResponseFormat] = ResponseFormat.HTML

    @classmethod
    def from_response(
        cls,
        response: SourceResponse,
        *,
        source_url: str | None = None,
    ) -> "HtmlResponseWrapper":
        if not isinstance(response, SourceResponse):
            raise TypeError("HTML wrapper requires enbek_monitor.network.SourceResponse")
        url = source_url or response.url
        require_absolute_source_url(url, path="$", allow_fixture=True)
        try:
            text = response.content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ParseError(
                "source response is not valid UTF-8 HTML",
                url=url,
                path="$",
                invalid_fields=("$",),
            ) from exc
        return cls(url, response, text, response.content, dict(response.headers))


# Explicit aliases make the adapter names easy to discover without coupling
# downstream parsers to one spelling of JSON/HTML.
JSONResponseWrapper = JsonResponseWrapper
HTMLResponseWrapper = HtmlResponseWrapper
JsonSourceResponse = JsonResponseWrapper
HtmlSourceResponse = HtmlResponseWrapper


@runtime_checkable
class SourceResponseAdapter(Protocol):
    """Common adapter contract for already received JSON/HTML responses."""

    format: ClassVar[ResponseFormat]

    def wrap(
        self,
        url: str,
        response: SourceResponse,
    ) -> SourceResponseWrapper:
        """Decode one response without performing a network request."""
        ...


class JsonResponseAdapter:
    """Adapter for JSON source responses."""

    format = ResponseFormat.JSON

    def wrap(self, url: str, response: SourceResponse) -> JsonResponseWrapper:
        return JsonResponseWrapper.from_response(response, source_url=url)

    adapt = wrap


class HtmlResponseAdapter:
    """Adapter for HTML source responses."""

    format = ResponseFormat.HTML

    def wrap(self, url: str, response: SourceResponse) -> HtmlResponseWrapper:
        return HtmlResponseWrapper.from_response(response, source_url=url)

    adapt = wrap


# Short aliases used by potential source adapter implementations.
JSONResponseAdapter = JsonResponseAdapter
HTMLResponseAdapter = HtmlResponseAdapter


@dataclass(frozen=True, slots=True)
class RegistryPage:
    """Typed output of either a JSON or HTML registry parser."""

    source_url: str
    entries: tuple[RegistryEntry, ...]
    pagination: PaginationMetadata
    response_format: ResponseFormat
    response: SourceResponse | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        require_absolute_source_url(self.source_url, path="$", allow_fixture=True)
        if not isinstance(self.entries, tuple):
            object.__setattr__(self, "entries", tuple(self.entries))
        if any(not isinstance(entry, RegistryEntry) for entry in self.entries):
            raise TypeError("registry page entries must be RegistryEntry values")
        if not isinstance(self.pagination, PaginationMetadata):
            raise TypeError("registry page pagination must be PaginationMetadata")
        if not isinstance(self.response_format, ResponseFormat):
            object.__setattr__(self, "response_format", ResponseFormat(self.response_format))


@dataclass(frozen=True, slots=True)
class DetailPage:
    """Typed output of either a JSON or HTML detail parser."""

    source_url: str
    attributes: SourceAttributes
    response_format: ResponseFormat
    pdf_published: bool | None = None
    response: SourceResponse | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        require_absolute_source_url(self.source_url, path="$", allow_fixture=True)
        if not isinstance(self.attributes, SourceAttributes):
            raise TypeError("detail page attributes must be SourceAttributes")
        if self.pdf_published is not None and not isinstance(self.pdf_published, bool):
            raise TypeError("detail page pdf_published must be boolean or null")
        if not isinstance(self.response_format, ResponseFormat):
            object.__setattr__(self, "response_format", ResponseFormat(self.response_format))


RegistryParseOutput: TypeAlias = RegistryPage
DetailParseOutput: TypeAlias = DetailPage


@runtime_checkable
class RegistryParserProtocol(Protocol):
    """Pure parser contract for one registry response."""

    def parse(
        self,
        url: str,
        response: SourceResponse,
    ) -> RegistryPage | ParseError:
        """Return a complete page or a structured ParseError; never fetch data."""
        ...


@runtime_checkable
class DetailParserProtocol(Protocol):
    """Pure parser contract for one detail response."""

    def parse(
        self,
        url: str,
        response: SourceResponse,
    ) -> DetailPage | ParseError:
        """Return complete detail attributes or a structured ParseError."""
        ...


# Protocol aliases keep both long, explicit and concise names available.
RegistryParser = RegistryParserProtocol
DetailParser = DetailParserProtocol


def build_parse_error(
    url: str,
    violations: Sequence[SourceSchemaViolation],
    *,
    stage: str = "parser",
) -> ParseError:
    """Aggregate all known schema violations into one :class:`ParseError`.

    The original order is preserved and duplicate paths are removed.  Missing
    fields are reported in ``missing_fields``; explicit null/empty/type or URL
    violations are reported in ``invalid_fields``.  The reason contains each
    path and reason so no schema detail is lost.
    """

    if not isinstance(violations, Sequence) or isinstance(violations, (str, bytes)):
        raise TypeError("violations must be a sequence of SourceSchemaViolation")
    normalized: list[SourceSchemaViolation] = []
    seen: set[tuple[str, SourceFieldState, str]] = set()
    for violation in violations:
        if not isinstance(violation, SourceSchemaViolation):
            raise TypeError("violations must contain SourceSchemaViolation values")
        key = (violation.path, violation.state, violation.reason)
        if key not in seen:
            normalized.append(violation)
            seen.add(key)
    if not normalized:
        raise ValueError("at least one schema violation is required")

    missing: list[str] = []
    invalid: list[str] = []
    reasons: list[str] = []
    for violation in normalized:
        if violation.is_missing:
            if violation.path not in missing:
                missing.append(violation.path)
        elif violation.path not in invalid:
            invalid.append(violation.path)
        reasons.append(f"{violation.path}: {violation.reason}")

    first = normalized[0]
    return ParseError(
        "; ".join(reasons),
        url=require_absolute_source_url(url, path="$", allow_fixture=True),
        stage=stage,
        path=first.path,
        missing_fields=missing,
        invalid_fields=invalid,
    )


def violation_from_field(
    field: SourceField,
    reason: str,
    *,
    invalid_null: bool = True,
) -> SourceSchemaViolation:
    """Create a violation from an inspected field using standard semantics."""

    state = field.state
    if state is SourceFieldState.NULL and not invalid_null:
        state = SourceFieldState.NULL
    return SourceSchemaViolation(field.path, reason, state)


__all__ = [
    "DETAIL_FIELD_PATHS",
    "DETAIL_PATHS",
    "DetailFieldPaths",
    "DetailPage",
    "DetailParseOutput",
    "DetailParser",
    "DetailParserProtocol",
    "FieldPresence",
    "FieldState",
    "HTMLResponseAdapter",
    "HTMLResponseWrapper",
    "HtmlResponseAdapter",
    "HtmlResponseWrapper",
    "HtmlSourceResponse",
    "JSONResponseAdapter",
    "JSONResponseWrapper",
    "JsonResponseAdapter",
    "JsonResponseWrapper",
    "JsonSourceResponse",
    "PaginationMetadata",
    "REGISTRY_FIELD_PATHS",
    "REGISTRY_PATHS",
    "RegistryFieldPaths",
    "RegistryPage",
    "RegistryParseOutput",
    "RegistryParser",
    "RegistryParserProtocol",
    "ResponseFormat",
    "SourceField",
    "SourceFieldState",
    "SourceResponseAdapter",
    "SourceResponseWrapper",
    "ResponseWrapper",
    "SourceSchemaViolation",
    "SourceUrlError",
    "build_parse_error",
    "inspect_field",
    "inspect_mapping_field",
    "is_absolute_source_url",
    "require_absolute_source_url",
    "violation_from_field",
]
