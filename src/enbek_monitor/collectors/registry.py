"""Complete, fixture-friendly collection of paginated registry pages.

The collector owns only the registry traversal boundary.  It delegates HTTP
retry/host policy to :class:`NetworkClient`, source decoding to the selected
pure registry parser, and immutable persistence to :class:`RawArtifactStore`.
A result with any mandatory-page error is deliberately marked incomplete and
can never be used as a lifecycle comparison candidate.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

from ..errors import MonitorError, ParseError, SourceError
from ..models import RawArtifact, RegistryEntry
from ..network import NetworkClient, SourceResponse
from ..source_discovery import SourceProfile
from ..storage.raw import RawArtifactStore
from ..parsers.registry_html import RegistryHtmlParser
from ..parsers.registry_json import RegistryJsonParser
from ..parsers.source_schema import RegistryPage, ResponseFormat, is_absolute_source_url


class _RegistryEntryCounter(HTMLParser):
    """Count the same ``registry-entry`` nodes understood by the HTML parser."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.count = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name.casefold(): value or "" for name, value in attrs}
        if "registry-entry" in set(values.get("class", "").split()):
            self.count += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)


@dataclass(frozen=True, slots=True)
class RegistryCollectionResult:
    """Typed outcome of one complete-or-incomplete registry traversal.

    ``entries`` may contain pages accepted before a later failure, which is
    useful for diagnostics.  Callers must use ``complete`` (or the explicit
    ``eligible_for_lifecycle`` property) before passing the result downstream;
    an incomplete result is never a source of NEW/MISSING/REMOVED events.
    """

    entries: tuple[RegistryEntry, ...] = ()
    pages: tuple[RegistryPage, ...] = ()
    raw_artifacts: tuple[RawArtifact, ...] = ()
    errors: tuple[MonitorError, ...] = ()
    complete: bool = False
    requested_urls: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.entries, tuple):
            object.__setattr__(self, "entries", tuple(self.entries))
        if not isinstance(self.pages, tuple):
            object.__setattr__(self, "pages", tuple(self.pages))
        if not isinstance(self.raw_artifacts, tuple):
            object.__setattr__(self, "raw_artifacts", tuple(self.raw_artifacts))
        if not isinstance(self.errors, tuple):
            object.__setattr__(self, "errors", tuple(self.errors))
        if not isinstance(self.requested_urls, tuple):
            object.__setattr__(self, "requested_urls", tuple(self.requested_urls))
        if any(not isinstance(item, RegistryEntry) for item in self.entries):
            raise TypeError("registry collection entries must be RegistryEntry values")
        if any(not isinstance(item, RegistryPage) for item in self.pages):
            raise TypeError("registry collection pages must be RegistryPage values")
        if any(not isinstance(item, RawArtifact) for item in self.raw_artifacts):
            raise TypeError("registry collection artifacts must be RawArtifact values")
        if any(not isinstance(item, MonitorError) for item in self.errors):
            raise TypeError("registry collection errors must be structured monitor errors")
        if not isinstance(self.complete, bool):
            raise TypeError("complete must be boolean")
        if self.complete and self.errors:
            raise ValueError("a complete registry result cannot contain errors")

    @property
    def success(self) -> bool:
        """Whether every mandatory registry page was collected and parsed."""

        return self.complete and not self.errors

    @property
    def failed(self) -> bool:
        return not self.success

    @property
    def incomplete(self) -> bool:
        return not self.complete

    @property
    def eligible_for_lifecycle(self) -> bool:
        """Explicit guard consumed by snapshot/lifecycle orchestration."""

        return self.complete and not self.errors

    @property
    def registry_entries(self) -> tuple[RegistryEntry, ...]:
        """Vocabulary alias for callers using the domain glossary name."""

        return self.entries

    @property
    def candidate_entries(self) -> tuple[RegistryEntry, ...]:
        """Alias used by candidate-building code."""

        return self.entries

    @property
    def error(self) -> MonitorError | None:
        """Return the first error for simple callers, without hiding others."""

        return self.errors[0] if self.errors else None


Registry_Collection_Result = RegistryCollectionResult


class RegistryCollector:
    """Collect the first registry page and every explicit pagination link."""

    def __init__(
        self,
        network_client: NetworkClient,
        raw_store: RawArtifactStore | SourceProfile,
        legacy_raw_store: RawArtifactStore | None = None,
        *,
        profile: SourceProfile | None = None,
        source_profile: SourceProfile | None = None,
        artifact_store: RawArtifactStore | None = None,
        parser: Any | None = None,
        json_parser: Any | None = None,
        html_parser: Any | None = None,
        run_id: str | None = None,
        staging_root: str | None = None,
    ) -> None:
        if not isinstance(network_client, NetworkClient):
            raise TypeError("network_client must be a NetworkClient")
        # The canonical project convention is (client, raw_store, profile=...).
        # Accept (client, profile, raw_store) as a compatibility convenience
        # for callers that constructed the collector from the task wording.
        if isinstance(raw_store, RawArtifactStore):
            selected_store = raw_store
            positional_profile = None
            if legacy_raw_store is not None:
                raise TypeError("third positional argument is only valid after a profile")
        else:
            positional_profile = raw_store
            selected_store = legacy_raw_store
        if artifact_store is not None:
            if selected_store is not None and selected_store is not artifact_store:
                raise ValueError("raw_store and artifact_store must refer to the same store")
            selected_store = artifact_store
        if not isinstance(selected_store, RawArtifactStore):
            raise TypeError("raw_store or artifact_store must be a RawArtifactStore")
        profile_values = [value for value in (positional_profile, profile, source_profile) if value is not None]
        if len(profile_values) > 1 and any(value is not profile_values[0] for value in profile_values[1:]):
            raise ValueError("profile and source_profile must refer to the same profile")
        selected_profile = profile_values[0] if profile_values else None
        if selected_profile is not None and not hasattr(selected_profile, "registry_url"):
            raise TypeError("profile must expose registry_url")
        if parser is not None and (json_parser is not None or html_parser is not None):
            raise ValueError("parser cannot be combined with json_parser or html_parser")
        self.network_client = network_client
        self.source_profile = selected_profile
        self.raw_store = selected_store
        self._parser_override = parser
        self.json_parser = json_parser if json_parser is not None else RegistryJsonParser()
        self.html_parser = html_parser if html_parser is not None else RegistryHtmlParser()
        self.run_id = run_id
        self.staging_root = staging_root

    def collect(
        self,
        source_profile: SourceProfile | None = None,
        *,
        profile: SourceProfile | None = None,
        run_id: str | None = None,
        staging_root: str | None = None,
        fetched_at: Any | None = None,
    ) -> RegistryCollectionResult:
        """Traverse a profile's registry pagination chain.

        The first request uses the profile's exact URL, method, query, body and
        headers.  Subsequent requests use each parser-provided absolute
        ``next`` URL; the recorded query/body pagination contract is applied
        without mutating the profile.  ``fixture://`` URLs are passed through
        unchanged, which makes the fallback fixtures behave like a captured
        source rather than a guessed endpoint.
        """

        if source_profile is not None and profile is not None and source_profile is not profile:
            raise ValueError("source_profile and profile must refer to the same profile")
        selected_profile = source_profile or profile or self.source_profile
        if selected_profile is None:
            raise ValueError("source_profile is required")
        try:
            registry_url = self._required_registry_url(selected_profile)
        except ValueError as exc:
            return self._failure_result(str(exc), url=str(getattr(selected_profile, "registry_url", "")))

        parser_result = self._select_parser(selected_profile, registry_url)
        if isinstance(parser_result, ParseError):
            return self._failure_result_from_error(parser_result)
        parser = parser_result
        selected_run_id = self.run_id if run_id is None else run_id
        selected_staging_root = self.staging_root if staging_root is None else staging_root

        entries: list[RegistryEntry] = []
        pages: list[RegistryPage] = []
        artifacts: list[RawArtifact] = []
        errors: list[MonitorError] = []
        requested_urls: list[str] = []
        seen_urls: set[str] = set()
        current_url = registry_url
        previous_page: RegistryPage | None = None

        while True:
            if current_url in seen_urls:
                errors.append(
                    self._pagination_error(
                        current_url,
                        "pagination cycle or repeated registry page URL detected",
                    )
                )
                break
            seen_urls.add(current_url)
            requested_urls.append(current_url)

            params, body = self._request_pagination_values(
                selected_profile,
                previous_page,
            )
            response_or_error = self.network_client.request(
                current_url,
                method=str(getattr(selected_profile, "method", "GET")).upper(),
                params=params,
                body=body,
                headers=dict(getattr(selected_profile, "headers", {}) or {}),
                expected_kind="registry",
            )
            if isinstance(response_or_error, SourceError):
                errors.append(response_or_error)
                break
            if not isinstance(response_or_error, SourceResponse):
                errors.append(
                    SourceError(
                        "registry request returned no usable HTTP response",
                        url=current_url,
                        stage="registry collection",
                    )
                )
                break

            # Fake/fixture transports may retain the profile URL on their
            # response object.  The requested page URL is the authoritative
            # source URL for parser diagnostics and the raw manifest.
            response = self._with_requested_url(response_or_error, current_url)
            try:
                artifact = self.raw_store.store_registry(
                    response,
                    run_id=selected_run_id,
                    staging_root=selected_staging_root,
                    fetched_at=fetched_at,
                )
                artifacts.append(artifact)
            except MonitorError as error:
                errors.append(error)
                break
            except (OSError, ValueError, TypeError) as error:
                errors.append(
                    SourceError(
                        f"could not persist registry raw response: {error}",
                        url=current_url,
                        status=response.status_code,
                        stage="registry collection",
                    )
                )
                break

            try:
                parsed = parser.parse(current_url, response)
            except ParseError as error:
                errors.append(error)
                break
            except (TypeError, ValueError) as error:
                errors.append(
                    ParseError(
                        f"registry parser failed: {error}",
                        url=current_url,
                        path="$",
                        invalid_fields=("$",),
                        stage="registry collection",
                    )
                )
                break
            if isinstance(parsed, ParseError):
                errors.append(parsed)
                break
            if not isinstance(parsed, RegistryPage):
                errors.append(
                    ParseError(
                        "registry parser returned an invalid page result",
                        url=current_url,
                        path="$",
                        invalid_fields=("$",),
                        stage="registry collection",
                    )
                )
                break

            cardinality_error = self._check_cardinality(parsed, response, current_url)
            if cardinality_error is not None:
                errors.append(cardinality_error)
                break
            entry_error = self._check_entries(parsed.entries, entries, current_url)
            if entry_error is not None:
                errors.append(entry_error)
                break

            pages.append(parsed)
            entries.extend(parsed.entries)
            previous_page = parsed
            if parsed.pagination.terminal or not parsed.pagination.has_next:
                return RegistryCollectionResult(
                    entries=tuple(entries),
                    pages=tuple(pages),
                    raw_artifacts=tuple(artifacts),
                    errors=(),
                    complete=True,
                    requested_urls=tuple(requested_urls),
                )

            next_url = parsed.pagination.next_url
            if next_url is None:
                # Defensive fallback for a malformed custom parser result.
                errors.append(
                    self._pagination_error(
                        current_url,
                        "pagination page was non-terminal but did not provide next URL",
                    )
                )
                break
            if not is_absolute_source_url(next_url, allow_fixture=True):
                errors.append(
                    self._pagination_error(
                        current_url,
                        "pagination next URL must be an absolute source or fixture URL",
                    )
                )
                break
            current_url = next_url

        return RegistryCollectionResult(
            entries=tuple(entries),
            pages=tuple(pages),
            raw_artifacts=tuple(artifacts),
            errors=tuple(errors),
            complete=False,
            requested_urls=tuple(requested_urls),
        )

    collect_registry = collect
    fetch = collect

    @staticmethod
    def _required_registry_url(profile: Any) -> str:
        value = getattr(profile, "registry_url", None)
        if not isinstance(value, str) or not value.strip():
            raise ValueError("SourceProfile.registry_url must be a non-empty string")
        if not is_absolute_source_url(value, allow_fixture=True):
            raise ValueError("SourceProfile.registry_url must be an absolute source URL")
        return value

    def _select_parser(self, profile: Any, url: str) -> Any | ParseError:
        if self._parser_override is not None:
            parser = self._parser_override
            if not callable(getattr(parser, "parse", None)):
                return ParseError(
                    "registry parser must expose parse(url, response)",
                    url=url,
                    path="$",
                    invalid_fields=("$",),
                    stage="registry collection",
                )
            return parser
        mode = getattr(getattr(profile, "parser_mode", None), "value", getattr(profile, "parser_mode", "json_api"))
        mode = str(mode).casefold()
        if mode == "html":
            return self.html_parser
        if mode in {"json_api", "json", "browser"}:
            # Browser discovery still hands the collector a captured JSON/HTML
            # response.  JSON is the deterministic default unless the caller
            # explicitly supplies an HTML parser/profile.
            return self.json_parser
        return ParseError(
            f"unsupported registry parser mode: {mode}",
            url=url,
            path="$.parser_mode",
            invalid_fields=("$.parser_mode",),
            stage="registry collection",
        )

    @staticmethod
    def _with_requested_url(response: SourceResponse, requested_url: str) -> SourceResponse:
        if response.url == requested_url:
            return response
        return SourceResponse(
            url=requested_url,
            status_code=response.status_code,
            headers=response.headers,
            content=response.content,
            raw=response.raw,
            request_kind=response.request_kind,
            attempts=response.attempts,
        )

    @staticmethod
    def _request_pagination_values(
        profile: Any,
        previous_page: RegistryPage | None,
    ) -> tuple[Mapping[str, Any] | None, Any]:
        query = copy.deepcopy(dict(getattr(profile, "query", {}) or {}))
        body = copy.deepcopy(getattr(profile, "body", None))
        if previous_page is None:
            return query, body

        pagination = getattr(profile, "pagination", {}) or {}
        request_contract = pagination.get("request", {}) if isinstance(pagination, Mapping) else {}
        if not isinstance(request_contract, Mapping):
            request_contract = {}
        location = str(request_contract.get("location", "")).casefold()
        page_parameter = request_contract.get("page_parameter", "page")
        page_size_parameter = request_contract.get("page_size_parameter", "page_size")
        if not isinstance(page_parameter, str) or not page_parameter.strip():
            return query, body
        next_page = previous_page.pagination.page_number
        if next_page is not None:
            next_page += 1
        if next_page is None:
            return query, body

        # An explicit fixture location means the next URL is authoritative;
        # still retain the profile query/body for transports that inspect it.
        target: dict[str, Any] | None = None
        if location in {"body", "json", "request_body"}:
            if isinstance(body, Mapping):
                target = dict(body)
                target[page_parameter] = next_page
            return query, target if target is not None else body
        if location in {"query", "params", "url"} or not location:
            query[page_parameter] = next_page
            page_size = previous_page.pagination.page_size
            if isinstance(page_size_parameter, str) and page_size_parameter and page_size is not None:
                query[page_size_parameter] = page_size
            return query, body
        if location == "fixture":
            return query, body
        return query, body

    @staticmethod
    def _check_cardinality(
        page: RegistryPage,
        response: SourceResponse,
        url: str,
    ) -> ParseError | None:
        expected: int | None = None
        if page.response_format is ResponseFormat.JSON:
            try:
                document = json.loads(response.content.decode("utf-8"))
                items = document.get("data", {}).get("items") if isinstance(document, Mapping) else None
                if isinstance(items, list):
                    expected = len(items)
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                expected = None
        else:
            try:
                counter = _RegistryEntryCounter()
                counter.feed(response.content.decode("utf-8"))
                counter.close()
                expected = counter.count
            except (UnicodeDecodeError, ValueError):
                expected = None
        if expected is not None and len(page.entries) != expected:
            return ParseError(
                f"registry page cardinality mismatch: parsed {len(page.entries)}, source contains {expected}",
                url=url,
                path="$.data.items" if page.response_format is ResponseFormat.JSON else "$.registry-entry",
                invalid_fields=("$.data.items" if page.response_format is ResponseFormat.JSON else "$.registry-entry",),
                stage="registry collection",
            )
        return None

    @staticmethod
    def _check_entries(
        page_entries: tuple[RegistryEntry, ...],
        previous_entries: list[RegistryEntry],
        url: str,
    ) -> ParseError | None:
        seen = {entry.enbek_id for entry in previous_entries}
        page_seen: set[int] = set()
        for entry in page_entries:
            if isinstance(entry.enbek_id, bool) or not isinstance(entry.enbek_id, int) or entry.enbek_id <= 0:
                return ParseError(
                    "registry entry Enbek_ID must be a positive integer",
                    url=url,
                    path="$.data.items",
                    invalid_fields=("$.data.items",),
                    stage="registry collection",
                )
            if not isinstance(entry.detail_page_url, str) or not is_absolute_source_url(
                entry.detail_page_url,
                allow_fixture=True,
            ):
                return ParseError(
                    "registry entry Detail_Page URL must be absolute",
                    url=url,
                    path="$.data.items",
                    invalid_fields=("$.data.items",),
                    stage="registry collection",
                )
            if entry.enbek_id in page_seen or entry.enbek_id in seen:
                return ParseError(
                    f"duplicate Enbek_ID {entry.enbek_id} in registry pagination chain",
                    url=url,
                    path="$.data.items",
                    invalid_fields=("$.data.items",),
                    stage="registry collection",
                )
            page_seen.add(entry.enbek_id)
        return None

    @staticmethod
    def _pagination_error(url: str, reason: str) -> ParseError:
        return ParseError(
            reason,
            url=url,
            path="$.data.next",
            invalid_fields=("$.data.next",),
            stage="registry collection",
        )

    @staticmethod
    def _failure_result(reason: str, *, url: str) -> RegistryCollectionResult:
        return RegistryCollectionResult(
            errors=(SourceError(reason, url=url or None, stage="registry collection"),),
            complete=False,
        )

    @staticmethod
    def _failure_result_from_error(error: MonitorError) -> RegistryCollectionResult:
        return RegistryCollectionResult(errors=(error,), complete=False)


Registry_Collector = RegistryCollector


__all__ = [
    "RegistryCollectionResult",
    "Registry_Collection_Result",
    "RegistryCollector",
    "Registry_Collector",
]
