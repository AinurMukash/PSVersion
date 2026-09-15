"""Discovery and validation of recorded Enbek source profiles.

The discovery layer deliberately does not perform HTTP requests.  An operator
records the Network/XHR request (or prepares a fixture profile), stores it as a
JSON document, and this module turns that document into a deterministic
:class:`SourceProfile`.  This keeps source discovery reproducible when Enbek is
not reachable and prevents the collector from guessing endpoint parameters.

The canonical capture shape is documented by ``source_profile.schema.json``.
For convenience, the loader also accepts a small set of explicit aliases used
by browser-exported captures (for example ``registry_url`` and
``request.url``).  Aliases are normalized immediately; the resulting profile
always exposes the same contract to downstream collectors.
"""

from __future__ import annotations

import copy
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeAlias
from urllib.parse import urlsplit

SCHEMA_VERSION = "1"

ParserMode: TypeAlias = Literal["json_api", "html", "browser"]
SourceKind: TypeAlias = Literal["capture", "fixture"]

ADAPTER_PRIORITY: tuple[ParserMode, ...] = ("json_api", "html", "browser")
SUPPORTED_PARSER_MODES = frozenset(ADAPTER_PRIORITY)
SUPPORTED_SOURCE_KINDS = frozenset(("capture", "fixture"))
SUPPORTED_SELECTOR_TYPES = frozenset(("json_path", "json_pointer", "css", "xpath", "text"))

# This is intentionally an allow-list rather than a deny-list.  A captured
# request can therefore not accidentally carry Authorization/Cookie or a
# vendor-specific credential header into a local source profile.
PUBLIC_HEADER_ALLOWLIST = frozenset(
    {
        "accept",
        "accept-encoding",
        "accept-language",
        "cache-control",
        "content-type",
        "dnt",
        "origin",
        "pragma",
        "range",
        "referer",
        "user-agent",
        "x-requested-with",
        "sec-fetch-dest",
        "sec-fetch-mode",
        "sec-fetch-site",
        "sec-fetch-user",
    }
)

_SENSITIVE_NAME_RE = re.compile(
    r"(?:^|[_-])(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|token|cookie|secret|password|passwd|credential|session)(?:$|[_-])",
    re.IGNORECASE,
)
_SENSITIVE_INLINE_RE = re.compile(
    r"(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|cookie|secret|password|passwd|credential|session)\s*[:=]",
    re.IGNORECASE,
)
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")

JSONScalar: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]
# ``SourceProfile`` is declared below; the runtime loader accepts it explicitly
# while this alias keeps annotations valid on Python 3.11 without evaluating a
# forward-reference string inside a ``|`` expression.
SourceInput: TypeAlias = str | Path | Mapping[str, Any] | Sequence[Mapping[str, Any]]


class SourceDiscoveryError(ValueError):
    """Base error for invalid or unavailable recorded source information."""


class CaptureValidationError(SourceDiscoveryError):
    """A capture/profile does not satisfy the source-profile contract."""

    def __init__(self, message: str, *, violations: Sequence[str] = ()) -> None:
        self.violations = tuple(violations)
        super().__init__(message)


class SecretMaterialError(CaptureValidationError):
    """A capture contains credentials, cookies, tokens, or secret-like data."""


class UnsupportedParserModeError(CaptureValidationError):
    """The capture requested an adapter not supported by this contract."""


class SourceUnavailableError(SourceDiscoveryError):
    """No confirmed capture or deterministic fixture profile is available."""


@dataclass(frozen=True)
class Selector:
    """A parser selector recorded from the source, without interpreting it."""

    type: str
    value: str

    def __post_init__(self) -> None:
        if self.type not in SUPPORTED_SELECTOR_TYPES:
            raise CaptureValidationError(
                f"unsupported selector type: {self.type!r}",
                violations=(f"selector.type: unsupported value {self.type!r}",),
            )
        if not isinstance(self.value, str) or not self.value or not self.value.strip():
            raise CaptureValidationError(
                "selector value must be a non-empty string",
                violations=("selector.value: must be a non-empty string",),
            )

    def to_dict(self) -> dict[str, str]:
        """Return the stable JSON representation used by ``SourceProfile``."""

        return {"type": self.type, "value": self.value}

    # Small Mapping-like conveniences make selectors easy to inspect in tests
    # and in collectors while keeping the typed object useful to callers.
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


@dataclass(frozen=True)
class SourceProfile:
    """Validated, deterministic contract for registry collection.

    ``registry_url`` is retained byte-for-byte as supplied in the capture; it
    is never joined with, normalized against, or inferred from another URL.
    The pagination mapping is copied from the capture and includes canonical
    ``records_selector``, ``next_selector`` and ``terminal_condition`` keys.
    """

    schema_version: str
    source_kind: SourceKind
    confirmed: bool
    parser_mode: ParserMode
    registry_url: str
    method: str
    query: Mapping[str, JSONValue]
    body: JSONValue
    headers: Mapping[str, str]
    pagination: Mapping[str, JSONValue]
    detail_link_selector: Selector
    pdf_link_selector: Selector
    capture_id: str | None = None
    captured_at: str | None = None

    @property
    def kind(self) -> SourceKind:
        """Alias matching the JSON field name used by the schema."""

        return self.source_kind

    @property
    def adapter(self) -> ParserMode:
        """Human-readable alias for the selected parser adapter."""

        return self.parser_mode

    @property
    def is_fixture(self) -> bool:
        return self.source_kind == "fixture"

    @property
    def registry_request(self) -> dict[str, JSONValue]:
        """Return the exact request contract in collector-friendly form."""

        return {
            "url": self.registry_url,
            "method": self.method,
            "query": copy.deepcopy(dict(self.query)),
            "body": copy.deepcopy(self.body),
            "headers": dict(self.headers),
        }

    @property
    def selectors(self) -> dict[str, dict[str, str]]:
        """Return detail/PDF selectors as plain JSON-compatible mappings."""

        return {
            "detail": self.detail_link_selector.to_dict(),
            "pdf": self.pdf_link_selector.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        """Serialize the profile without adding inferred source values."""

        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "kind": self.source_kind,
            "confirmed": self.confirmed,
            "parser_mode": self.parser_mode,
            "registry": {
                "url": self.registry_url,
                "method": self.method,
                "query": copy.deepcopy(dict(self.query)),
                "body": copy.deepcopy(self.body),
                "headers": dict(self.headers),
            },
            "pagination": copy.deepcopy(dict(self.pagination)),
            "detail_link_selector": self.detail_link_selector.to_dict(),
            "pdf_link_selector": self.pdf_link_selector.to_dict(),
        }
        if self.capture_id is not None:
            result["capture_id"] = self.capture_id
        if self.captured_at is not None:
            result["captured_at"] = self.captured_at
        return result

    def to_json(self) -> str:
        """Return deterministic UTF-8-compatible JSON text with one final LF."""

        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ) + "\n"

    def to_json_bytes(self) -> bytes:
        """Return the deterministic profile representation as UTF-8 bytes."""

        return self.to_json().encode("utf-8")

    def write(self, path: str | Path) -> None:
        """Write a profile locally; this method performs no network access."""

        Path(path).write_bytes(self.to_json_bytes())

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "SourceProfile":
        """Build one profile from a canonical or supported capture mapping."""

        return _profile_from_mapping(document)


@dataclass(frozen=True)
class SourceDiscoveryAdapter:
    """Stateless adapter that resolves recorded captures and fixture profiles."""

    def discover(
        self,
        capture: SourceInput | None = None,
        *,
        fixture_profile: SourceInput | None = None,
    ) -> SourceProfile:
        return discover_source(capture, fixture_profile=fixture_profile)


def _path_text(path: Sequence[object]) -> str:
    result = "$"
    for part in path:
        if isinstance(part, int):
            result += f"[{part}]"
        else:
            result += f".{part}"
    return result


def _raise_violations(violations: list[str]) -> None:
    if violations:
        raise CaptureValidationError(
            "invalid source profile: " + "; ".join(violations),
            violations=violations,
        )


def _is_mapping(value: Any) -> bool:
    return isinstance(value, Mapping)


def _copy_json(value: Any, path: Sequence[object] = ()) -> JSONValue:
    """Validate JSON-compatible values and return an independent copy."""

    if value is None or isinstance(value, (str, bool, int)):
        return copy.deepcopy(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CaptureValidationError(
                "non-finite JSON number is not allowed",
                violations=(f"{_path_text(path)}: non-finite number",),
            )
        return value
    if isinstance(value, Mapping):
        result: dict[str, JSONValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CaptureValidationError(
                    "JSON object keys must be strings",
                    violations=(f"{_path_text(path)}: key must be a string",),
                )
            result[key] = _copy_json(item, (*path, key))
        return result
    if isinstance(value, list):
        return [_copy_json(item, (*path, index)) for index, item in enumerate(value)]
    if isinstance(value, tuple):
        return [_copy_json(item, (*path, index)) for index, item in enumerate(value)]
    raise CaptureValidationError(
        f"unsupported value type {type(value).__name__}",
        violations=(f"{_path_text(path)}: not a JSON value",),
    )


def _sensitive_name(name: str) -> bool:
    normalized = name.strip().replace(" ", "_")
    return bool(_SENSITIVE_NAME_RE.search(normalized))


def _scan_for_secret_material(value: Any, path: Sequence[object] = ()) -> None:
    """Reject secret-like keys and form snippets before profile creation."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            current_path = (*path, key_text)
            if _sensitive_name(key_text):
                raise SecretMaterialError(
                    f"secret-like field is not allowed at {_path_text(current_path)}",
                    violations=(f"{_path_text(current_path)}: secret/cookie/token field",),
                )
            _scan_for_secret_material(item, current_path)
        return
    if isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _scan_for_secret_material(item, (*path, index))
        return
    if isinstance(value, str) and _SENSITIVE_INLINE_RE.search(value):
        raise SecretMaterialError(
            f"secret-like inline value is not allowed at {_path_text(path)}",
            violations=(f"{_path_text(path)}: secret/cookie/token material",),
        )


def _validate_text(value: Any, path: Sequence[object], *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise CaptureValidationError(
            f"{_path_text(path)} must be a non-empty string",
            violations=(f"{_path_text(path)}: must be a non-empty string",),
        )
    if _CONTROL_CHAR_RE.search(value):
        raise CaptureValidationError(
            f"{_path_text(path)} contains control characters",
            violations=(f"{_path_text(path)}: control characters are not allowed",),
        )
    return value


def _validate_url(value: Any, path: Sequence[object], *, allow_fixture: bool = False) -> str:
    text = _validate_text(value, path)
    if text != text.strip():
        raise CaptureValidationError(
            f"{_path_text(path)} must retain a URL without surrounding whitespace",
            violations=(f"{_path_text(path)}: surrounding whitespace is not allowed",),
        )
    parsed = urlsplit(text)
    allowed_schemes = {"http", "https"}
    if allow_fixture:
        allowed_schemes.add("fixture")
    if parsed.scheme not in allowed_schemes or not parsed.netloc:
        raise CaptureValidationError(
            f"{_path_text(path)} must be an absolute HTTP(S) URL",
            violations=(f"{_path_text(path)}: invalid absolute URL",),
        )
    return text


def _parse_selector(
    value: Any,
    path: Sequence[object],
    *,
    default_type: str,
) -> Selector:
    if isinstance(value, str):
        value = {"type": default_type, "value": value}
    if not isinstance(value, Mapping):
        raise CaptureValidationError(
            f"{_path_text(path)} must be a selector object",
            violations=(f"{_path_text(path)}: selector object required",),
        )
    selector_type = value.get("type", value.get("kind"))
    selector_value = value.get("value", value.get("path"))
    if not isinstance(selector_type, str):
        raise CaptureValidationError(
            f"{_path_text((*path, 'type'))} must identify selector syntax",
            violations=(f"{_path_text((*path, 'type'))}: missing selector type",),
        )
    if selector_type not in SUPPORTED_SELECTOR_TYPES:
        raise CaptureValidationError(
            f"{_path_text((*path, 'type'))} has unsupported value {selector_type!r}",
            violations=(f"{_path_text((*path, 'type'))}: unsupported selector type",),
        )
    if not isinstance(selector_value, str) or not selector_value.strip():
        raise CaptureValidationError(
            f"{_path_text((*path, 'value'))} must be non-empty",
            violations=(f"{_path_text((*path, 'value'))}: missing selector value",),
        )
    return Selector(selector_type, selector_value)


def _default_selector_type(parser_mode: ParserMode) -> str:
    return "json_path" if parser_mode == "json_api" else "css"


def _validate_headers(value: Any, path: Sequence[object]) -> dict[str, str]:
    if value is None:
        raise CaptureValidationError(
            f"{_path_text(path)} must be an object",
            violations=(f"{_path_text(path)}: headers object required",),
        )
    if not isinstance(value, Mapping):
        raise CaptureValidationError(
            f"{_path_text(path)} must be an object",
            violations=(f"{_path_text(path)}: headers object required",),
        )
    result: dict[str, str] = {}
    seen: set[str] = set()
    violations: list[str] = []
    for raw_name, raw_value in value.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            violations.append(f"{_path_text((*path, str(raw_name)))}: invalid header name")
            continue
        name = raw_name.strip()
        lower_name = name.lower()
        header_path = _path_text((*path, name))
        if lower_name in seen:
            violations.append(f"{header_path}: duplicate header name")
            continue
        seen.add(lower_name)
        if lower_name not in PUBLIC_HEADER_ALLOWLIST:
            if _sensitive_name(name) or lower_name in {
                "authorization",
                "cookie",
                "set-cookie",
                "proxy-authorization",
            }:
                raise SecretMaterialError(
                    f"secret or cookie header is not allowed: {name}",
                    violations=(f"{header_path}: secret/cookie header",),
                )
            violations.append(f"{header_path}: header is not in the public allow-list")
            continue
        if not isinstance(raw_value, str):
            violations.append(f"{header_path}: header value must be a string")
            continue
        if _CONTROL_CHAR_RE.search(raw_value):
            violations.append(f"{header_path}: control characters are not allowed")
            continue
        result[name] = raw_value
    _raise_violations(violations)
    return result


def _normalize_pagination(
    value: Any,
    parser_mode: ParserMode,
    path: Sequence[object],
    *,
    registry_records_selector: Any = None,
) -> dict[str, JSONValue]:
    if not isinstance(value, Mapping):
        raise CaptureValidationError(
            f"{_path_text(path)} must be an object",
            violations=(f"{_path_text(path)}: pagination object required",),
        )
    raw = dict(value)
    records_value = raw.get("records_selector", raw.get("records_field", registry_records_selector))
    next_missing = "next_selector" not in raw and "next_field" not in raw and "next_url_selector" not in raw and "next_link_selector" not in raw
    next_value = raw.get(
        "next_selector",
        raw.get("next_field", raw.get("next_url_selector", raw.get("next_link_selector"))),
    )
    terminal_value = raw.get("terminal_condition", raw.get("stop_condition", raw.get("terminal")))
    violations: list[str] = []
    if records_value is None:
        violations.append(f"{_path_text((*path, 'records_selector'))}: required")
    if next_missing:
        violations.append(f"{_path_text((*path, 'next_selector'))}: required; record the observed pagination field")
    if terminal_value is None or (isinstance(terminal_value, str) and not terminal_value.strip()):
        violations.append(f"{_path_text((*path, 'terminal_condition'))}: required")
    if violations:
        _raise_violations(violations)

    default_type = _default_selector_type(parser_mode)
    canonical: dict[str, JSONValue] = _copy_json(raw, path)  # type: ignore[assignment]
    canonical["records_selector"] = _parse_selector(
        records_value,
        (*path, "records_selector"),
        default_type=default_type,
    ).to_dict()  # type: ignore[assignment]
    if next_value is None:
        canonical["next_selector"] = None
    else:
        canonical["next_selector"] = _parse_selector(
            next_value,
            (*path, "next_selector"),
            default_type=default_type,
        ).to_dict()  # type: ignore[assignment]
    if isinstance(terminal_value, Mapping):
        terminal_condition = terminal_value.get("when", terminal_value.get("condition"))
        if not isinstance(terminal_condition, str) or not terminal_condition.strip():
            raise CaptureValidationError(
                f"{_path_text((*path, 'terminal_condition'))} must contain when/condition",
                violations=(f"{_path_text((*path, 'terminal_condition'))}: invalid terminal condition",),
            )
        canonical["terminal_condition"] = terminal_condition
    else:
        canonical["terminal_condition"] = _validate_text(
            terminal_value,
            (*path, "terminal_condition"),
        )
    return canonical


def _load_document(source: SourceInput) -> Any:
    if isinstance(source, SourceProfile):
        return source.to_dict()
    if isinstance(source, Mapping):
        return copy.deepcopy(dict(source))
    if isinstance(source, Sequence) and not isinstance(source, (str, bytes, bytearray)):
        return copy.deepcopy(list(source))
    if isinstance(source, (str, Path)):
        path_or_text = str(source)
        path = Path(path_or_text)
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise CaptureValidationError(
                    f"invalid JSON in capture {path}: {exc.msg}",
                    violations=(f"$: invalid JSON at character {exc.pos}",),
                ) from exc
        if isinstance(source, str) and source.lstrip().startswith(("{", "[")):
            try:
                return json.loads(source)
            except json.JSONDecodeError as exc:
                raise CaptureValidationError(
                    f"invalid inline capture JSON: {exc.msg}",
                    violations=(f"$: invalid JSON at character {exc.pos}",),
                ) from exc
        raise FileNotFoundError(path)
    raise CaptureValidationError(
        f"unsupported capture input type: {type(source).__name__}",
        violations=("$: expected path, mapping, or list of mappings",),
    )


def _deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(dict(base))
    for key, value in overlay.items():
        if isinstance(merged.get(key), Mapping) and isinstance(value, Mapping):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _expand_candidates(document: Any) -> list[Mapping[str, Any]]:
    if isinstance(document, Mapping):
        for collection_key in ("candidates", "profiles", "adapters"):
            candidates = document.get(collection_key)
            if candidates is not None:
                if not isinstance(candidates, list) or not candidates:
                    raise CaptureValidationError(
                        f"$.{collection_key} must be a non-empty array",
                        violations=(f"$.{collection_key}: non-empty array required",),
                    )
                base = {key: value for key, value in document.items() if key != collection_key}
                result: list[Mapping[str, Any]] = []
                for index, candidate in enumerate(candidates):
                    if not isinstance(candidate, Mapping):
                        raise CaptureValidationError(
                            f"$.{collection_key}[{index}] must be an object",
                            violations=(f"$.{collection_key}[{index}]: object required",),
                        )
                    result.append(_deep_merge(base, candidate))
                return result
        return [document]
    if isinstance(document, list):
        if not document:
            raise CaptureValidationError(
                "capture candidate list must not be empty",
                violations=("$: non-empty candidate list required",),
            )
        result = []
        for index, candidate in enumerate(document):
            if not isinstance(candidate, Mapping):
                raise CaptureValidationError(
                    f"$[{index}] must be an object",
                    violations=(f"$[{index}]: object required",),
                )
            result.append(candidate)
        return result
    raise CaptureValidationError(
        "capture document must be an object or candidate array",
        violations=("$: object or array required",),
    )


def _profile_from_mapping(
    document: Mapping[str, Any],
    *,
    source_kind_override: SourceKind | None = None,
) -> SourceProfile:
    # Check the whole recorded contract before interpreting any field.  This
    # catches secrets hidden in a query/body or in adapter metadata as well as
    # obvious credential headers.
    _scan_for_secret_material(document)

    schema_version = document.get("schema_version", SCHEMA_VERSION)
    if not isinstance(schema_version, str) or not schema_version.strip():
        raise CaptureValidationError(
            "schema_version must be a non-empty string",
            violations=("$.schema_version: required non-empty string",),
        )

    raw_kind = source_kind_override or document.get("kind", document.get("profile_kind", "capture"))
    if raw_kind not in SUPPORTED_SOURCE_KINDS:
        raise CaptureValidationError(
            f"unsupported source profile kind: {raw_kind!r}",
            violations=("$.kind: expected capture or fixture",),
        )
    source_kind: SourceKind = raw_kind  # type: ignore[assignment]
    confirmed = document.get("confirmed", True if source_kind == "capture" else False)
    if not isinstance(confirmed, bool):
        raise CaptureValidationError(
            "confirmed must be boolean",
            violations=("$.confirmed: boolean required",),
        )

    registry_raw = document.get("registry")
    if not isinstance(registry_raw, Mapping):
        request_raw = document.get("request")
        registry_raw = request_raw if isinstance(request_raw, Mapping) else {}
    registry = dict(registry_raw)
    parser_mode_raw = document.get("parser_mode", registry.get("parser_mode"))
    if parser_mode_raw not in SUPPORTED_PARSER_MODES:
        if parser_mode_raw is None:
            raise UnsupportedParserModeError(
                "parser_mode is required and must come from a confirmed capture",
                violations=("$.parser_mode: required",),
            )
        raise UnsupportedParserModeError(
            f"unsupported parser_mode: {parser_mode_raw!r}",
            violations=("$.parser_mode: expected json_api, html, or browser",),
        )
    parser_mode: ParserMode = parser_mode_raw  # type: ignore[assignment]

    registry_url = registry.get("url", registry.get("registry_url", document.get("registry_url")))
    if registry_url is None and isinstance(document.get("request"), Mapping):
        registry_url = document["request"].get("url")
    registry_url = _validate_url(
        registry_url,
        ("registry", "url"),
        allow_fixture=source_kind == "fixture",
    )
    method_raw = registry.get("method", document.get("method"))
    if method_raw is None:
        raise CaptureValidationError(
            "registry.method is required; discovery will not infer the HTTP method",
            violations=("$.registry.method: required",),
        )
    method = _validate_text(method_raw, ("registry", "method"))
    method = method.upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
        raise CaptureValidationError(
            f"unsupported HTTP method: {method!r}",
            violations=("$.registry.method: unsupported HTTP method",),
        )

    query_raw = registry["query"] if "query" in registry else document.get("query")
    if query_raw is None:
        raise CaptureValidationError(
            "registry.query is required; discovery will not infer endpoint parameters",
            violations=("$.registry.query: required object",),
        )
    if not isinstance(query_raw, Mapping):
        raise CaptureValidationError(
            "registry.query must be an object",
            violations=("$.registry.query: object required",),
        )
    query = _copy_json(query_raw, ("registry", "query"))
    assert isinstance(query, dict)

    body_raw = registry["body"] if "body" in registry else document.get("body")
    if "body" not in registry and "body" not in document:
        raise CaptureValidationError(
            "registry.body is required; record null when the request has no body",
            violations=("$.registry.body: required",),
        )
    body = _copy_json(body_raw, ("registry", "body"))

    headers_raw = registry["headers"] if "headers" in registry else document.get("headers")
    if headers_raw is None and "headers" not in registry and "headers" not in document:
        raise CaptureValidationError(
            "registry.headers is required; record an empty object when no public headers were sent",
            violations=("$.registry.headers: required",),
        )
    headers = _validate_headers(headers_raw, ("registry", "headers"))

    response_raw = registry.get("response")
    response = response_raw if isinstance(response_raw, Mapping) else {}
    pagination_raw = document.get("pagination", registry.get("pagination", response.get("pagination")))
    if pagination_raw is None:
        raise CaptureValidationError(
            "pagination contract is required; discovery will not infer page parameters",
            violations=("$.pagination: required",),
        )
    registry_records_selector = registry.get("records_selector", response.get("records_selector"))
    pagination = _normalize_pagination(
        pagination_raw,
        parser_mode,
        ("pagination",),
        registry_records_selector=registry_records_selector,
    )

    detail_raw = document.get("detail")
    detail = detail_raw if isinstance(detail_raw, Mapping) else {}
    detail_selector_raw = registry.get(
        "detail_link_selector",
        registry.get("detail_url_selector", document.get("detail_link_selector")),
    )
    if detail_selector_raw is None:
        selectors = registry.get("selectors")
        if isinstance(selectors, Mapping):
            detail_selector_raw = selectors.get("detail")
    if detail_selector_raw is None:
        detail_selector_raw = detail.get("link_selector", detail.get("url_selector"))
    if detail_selector_raw is None:
        raise CaptureValidationError(
            "detail link selector is required",
            violations=("$.detail_link_selector: required",),
        )
    selector_default = _default_selector_type(parser_mode)
    detail_selector = _parse_selector(
        detail_selector_raw,
        ("detail_link_selector",),
        default_type=selector_default,
    )

    pdf_selector_raw = detail.get(
        "pdf_link_selector",
        detail.get("pdf_url_selector", document.get("pdf_link_selector")),
    )
    if pdf_selector_raw is None:
        selectors = detail.get("selectors")
        if isinstance(selectors, Mapping):
            pdf_selector_raw = selectors.get("pdf")
    if pdf_selector_raw is None:
        raise CaptureValidationError(
            "PDF link selector is required",
            violations=("$.pdf_link_selector: required",),
        )
    pdf_selector = _parse_selector(
        pdf_selector_raw,
        ("pdf_link_selector",),
        default_type=selector_default,
    )

    capture_id = document.get("capture_id", document.get("id"))
    if capture_id is not None:
        capture_id = _validate_text(capture_id, ("capture_id",))
    captured_at = document.get("captured_at")
    if captured_at is not None:
        captured_at = _validate_text(captured_at, ("captured_at",))

    return SourceProfile(
        schema_version=schema_version,
        source_kind=source_kind,
        confirmed=confirmed,
        parser_mode=parser_mode,
        registry_url=registry_url,
        method=method,
        query=query,
        body=body,
        headers=headers,
        pagination=pagination,
        detail_link_selector=detail_selector,
        pdf_link_selector=pdf_selector,
        capture_id=capture_id,
        captured_at=captured_at,
    )


def _profiles_from_input(
    source: SourceInput,
    *,
    source_kind_override: SourceKind | None = None,
) -> list[SourceProfile]:
    document = _load_document(source)
    profiles: list[SourceProfile] = []
    for candidate in _expand_candidates(document):
        profiles.append(_profile_from_mapping(candidate, source_kind_override=source_kind_override))
    return profiles


def load_source_profile(source: SourceInput) -> SourceProfile:
    """Load exactly one canonical source/fixture profile from local input.

    This function only reads a local path or an already supplied mapping.  It
    never probes ``registry_url``.  Use :func:`discover_source` when a capture
    contains multiple adapter candidates or when a fixture fallback is needed.
    """

    profiles = _profiles_from_input(source)
    if len(profiles) != 1:
        raise CaptureValidationError(
            "load_source_profile expects exactly one profile; use discover_source for candidates",
            violations=("$: exactly one profile required",),
        )
    return profiles[0]


def load_capture(source: SourceInput) -> SourceProfile:
    """Load one operator capture and require it to be a capture profile."""

    profile = load_source_profile(source)
    if profile.source_kind != "capture":
        raise CaptureValidationError(
            "load_capture received a fixture profile",
            violations=("$.kind: expected capture",),
        )
    return profile


def select_profile(
    profiles: Sequence[SourceProfile],
    *,
    require_confirmed: bool = True,
) -> SourceProfile:
    """Select the first supported adapter in json_api → html → browser order."""

    if not profiles:
        raise SourceUnavailableError("no source profiles were supplied")
    for mode in ADAPTER_PRIORITY:
        for profile in profiles:
            if profile.parser_mode == mode and (not require_confirmed or profile.confirmed):
                return profile
    state = "confirmed " if require_confirmed else ""
    raise SourceUnavailableError(f"no {state}source profile has a supported parser mode")


def select_parser_mode(
    profiles: Sequence[SourceProfile],
    *,
    require_confirmed: bool = True,
) -> ParserMode:
    """Return the selected parser mode without making a network request."""

    return select_profile(profiles, require_confirmed=require_confirmed).parser_mode


def _resolve_optional_input(
    primary: SourceInput | None,
    alias: SourceInput | None,
    name: str,
) -> SourceInput | None:
    if primary is not None and alias is not None:
        raise SourceDiscoveryError(f"provide only one of {name} and its path alias")
    return primary if primary is not None else alias


def discover_source(
    capture: SourceInput | None = None,
    *,
    fixture_profile: SourceInput | None = None,
    capture_path: SourceInput | None = None,
    fixture_profile_path: SourceInput | None = None,
) -> SourceProfile:
    """Resolve a confirmed capture, falling back to a local fixture profile.

    No live URL is accepted as an instruction to make a request.  A missing
    capture path is treated as source unavailability and allows a supplied
    fixture profile to be selected.  A malformed existing capture still
    raises a validation error instead of being silently hidden by a fallback.
    An unconfirmed capture is never selected when no deterministic fixture is
    available.
    """

    capture = _resolve_optional_input(capture, capture_path, "capture")
    fixture_profile = _resolve_optional_input(
        fixture_profile,
        fixture_profile_path,
        "fixture_profile",
    )

    capture_profiles: list[SourceProfile] = []
    capture_missing = False
    if capture is not None:
        try:
            capture_profiles = _profiles_from_input(capture)
        except FileNotFoundError:
            capture_missing = True

    confirmed_captures = [
        profile
        for profile in capture_profiles
        if profile.source_kind == "capture" and profile.confirmed
    ]
    if confirmed_captures:
        return select_profile(confirmed_captures, require_confirmed=True)

    # A capture document may explicitly carry a fixture candidate.  Treat it
    # as fixture data, but never treat an unconfirmed capture as confirmed.
    embedded_fixtures = [profile for profile in capture_profiles if profile.source_kind == "fixture"]
    if fixture_profile is not None:
        try:
            fixture_profiles = _profiles_from_input(
                fixture_profile,
                source_kind_override="fixture",
            )
        except FileNotFoundError:
            fixture_profiles = []
    else:
        fixture_profiles = []
    fixture_candidates = [*embedded_fixtures, *fixture_profiles]
    if fixture_candidates:
        return select_profile(fixture_candidates, require_confirmed=False)

    if capture_profiles and not confirmed_captures:
        raise SourceUnavailableError(
            "capture was loaded but no confirmed adapter was found; provide a deterministic fixture profile"
        )
    if capture_missing:
        raise SourceUnavailableError(
            "recorded capture is unavailable and no fixture profile was supplied"
        )
    raise SourceUnavailableError(
        "no recorded capture or fixture profile supplied; live connectivity is not used by discovery"
    )


# Explicit alias for callers that prefer the spelling used in the requirements.
Source_Profile = SourceProfile

__all__ = [
    "ADAPTER_PRIORITY",
    "CaptureValidationError",
    "ParserMode",
    "PUBLIC_HEADER_ALLOWLIST",
    "SecretMaterialError",
    "Selector",
    "SourceDiscoveryAdapter",
    "SourceDiscoveryError",
    "SourceProfile",
    "SourceUnavailableError",
    "Source_Profile",
    "UnsupportedParserModeError",
    "discover_source",
    "load_capture",
    "load_source_profile",
    "select_parser_mode",
    "select_profile",
]
