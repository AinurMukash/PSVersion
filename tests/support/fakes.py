"""Deterministic transport, clock, and sleeper fakes for local tests.

The helpers in this module deliberately have no dependency on ``httpx`` (or on
any other network library).  They model only the small boundary needed by the
monitor's network client:

* :class:`FakeTransport` returns scripted responses or raises scripted network
  exceptions for exact URLs and optional ``registry``/``detail``/``pdf`` kinds;
* :class:`FakeClock` advances only when a test or fake sleeper tells it to;
* :class:`FakeSleeper` records every delay and advances the injected clock
  without waiting in real time.

A route can contain one outcome or a sequence of outcomes.  A sequence is
consumed in insertion order, which makes retry tests reproducible.  Once a
sequence is exhausted its last outcome is repeated by default; pass
``repeat_last=False`` when exhaustion itself should be observable.
"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import threading
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar, TypeAlias


# A private sentinel is used instead of ``None`` because ``None`` is a useful
# JSON response body and an explicit ``json_data=None`` is a valid fixture.
_UNSET = object()


class FakeTransportError(OSError):
    """Base exception raised by a fake transport scenario."""


class FakeNetworkError(FakeTransportError):
    """A deterministic stand-in for a connection/timeout failure."""


class ScenarioExhaustedError(FakeTransportError):
    """Raised when a non-repeating scripted scenario has no outcome left."""


class FakeHTTPStatusError(FakeTransportError):
    """Raised by :meth:`FakeResponse.raise_for_status` for an HTTP error."""

    def __init__(self, response: "FakeResponse") -> None:
        self.response = response
        super().__init__(
            f"fake HTTP status {response.status_code} for "
            f"{response.url or '<unknown URL>'}"
        )


# ---------------------------------------------------------------------------
# HTTP-like response objects
# ---------------------------------------------------------------------------


class FakeHeaders(Mapping[str, str]):
    """Small case-insensitive, insertion-preserving header mapping."""

    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        entries: list[tuple[str, str]] = []
        index: dict[str, int] = {}
        for raw_key, raw_value in (values or {}).items():
            if not isinstance(raw_key, str) or not raw_key:
                raise TypeError("fake header names must be non-empty strings")
            if not isinstance(raw_value, str):
                raise TypeError("fake header values must be strings")
            folded = raw_key.casefold()
            item = (raw_key, raw_value)
            if folded in index:
                entries[index[folded]] = item
            else:
                index[folded] = len(entries)
                entries.append(item)
        self._entries = tuple(entries)
        self._index = {key.casefold(): value for key, value in self._entries}

    def __getitem__(self, key: str) -> str:
        try:
            return self._index[key.casefold()]
        except (AttributeError, KeyError) as exc:
            raise KeyError(key) from exc

    def __iter__(self):
        return (key for key, _ in self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def items(self):
        return self._entries

    def get(self, key: str, default: Any = None) -> str | Any:
        try:
            return self[key]
        except KeyError:
            return default

    def to_dict(self) -> dict[str, str]:
        return dict(self._entries)



def _encode_body(value: Any) -> bytes:
    """Convert a fixture body to bytes without changing supplied bytes."""

    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, (Mapping, list, tuple, int, float, bool)):
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    raise TypeError(f"unsupported fake response body: {type(value).__name__}")



def _validate_nonnegative_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a non-negative number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{field_name} must be a finite non-negative number")
    return number


class FakeResponse:
    """HTTP-like response with exact byte content and JSON convenience access."""

    def __init__(
        self,
        status_code: int = 200,
        content: Any = _UNSET,
        *,
        body: Any = _UNSET,
        headers: Mapping[str, str] | None = None,
        url: str | None = None,
        request_kind: str | None = None,
        json_data: Any = _UNSET,
        latency: float = 0.0,
    ) -> None:
        if isinstance(status_code, bool) or not isinstance(status_code, int):
            raise TypeError("status_code must be an integer")
        if not 100 <= status_code <= 599:
            raise ValueError("status_code must be between 100 and 599")
        if content is not _UNSET and body is not _UNSET:
            raise TypeError("provide either content or body, not both")
        raw_body = body if body is not _UNSET else content
        if raw_body is _UNSET:
            raw_body = _encode_body(json_data) if json_data is not _UNSET else b""
        self.status_code = status_code
        self.content = _encode_body(raw_body)
        self.headers = FakeHeaders(headers)
        self.url = url
        self.request_kind = request_kind
        self.latency = _validate_nonnegative_number(latency, "latency")
        self._json_data = (
            copy.deepcopy(json_data) if json_data is not _UNSET else _UNSET
        )
        self.request: FakeRequest | None = None

    @property
    def body(self) -> bytes:
        """Alias matching the source-response terminology."""

        return self.content

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    @property
    def status(self) -> int:
        """Short status alias used by some transport adapters."""

        return self.status_code

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 400

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> Any:
        """Return fixture JSON, parsing the exact body when necessary."""

        if self._json_data is not _UNSET:
            return copy.deepcopy(self._json_data)
        return json.loads(self.content.decode("utf-8"))

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise FakeHTTPStatusError(self)

    def for_request(
        self,
        request: "FakeRequest",
        *,
        url: str,
        request_kind: str | None,
    ) -> "FakeResponse":
        """Copy a scripted response and attach request metadata."""

        response = copy.copy(self)
        response.url = self.url if self.url is not None else url
        response.request_kind = (
            self.request_kind if self.request_kind is not None else request_kind
        )
        response.request = request
        return response

    def __repr__(self) -> str:
        return (
            f"FakeResponse(status_code={self.status_code!r}, "
            f"content={self.content!r}, url={self.url!r})"
        )


# ---------------------------------------------------------------------------
# Clock and sleeper
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClockAdvance:
    """One explicit fake-clock advancement."""

    started_at: float
    seconds: float
    finished_at: float

    @property
    def delay(self) -> float:
        return self.seconds


class FakeClock:
    """A manually controlled numeric clock; it never reads wall-clock time."""

    def __init__(self, start: float | int | datetime = 0.0) -> None:
        self._lock = threading.RLock()
        self._now = self._coerce_start(start)
        self.advances: list[ClockAdvance] = []

    @staticmethod
    def _coerce_start(start: float | int | datetime) -> float:
        if isinstance(start, datetime):
            if start.tzinfo is None or start.utcoffset() is None:
                # A naive fixture timestamp is explicitly interpreted as UTC;
                # no system timezone is consulted.
                start = start.replace(tzinfo=timezone.utc)
            return start.astimezone(timezone.utc).timestamp()
        return float(_validate_nonnegative_number(start, "start"))

    def now(self) -> float:
        """Return the current deterministic timestamp in fake seconds."""

        with self._lock:
            return self._now

    __call__ = now

    @property
    def current(self) -> float:
        return self.now()

    @property
    def current_time(self) -> float:
        return self.now()

    def monotonic(self) -> float:
        """Alias suitable for retry/concurrency code."""

        return self.now()

    def utcnow(self) -> datetime:
        return datetime.fromtimestamp(self.now(), timezone.utc)

    def iso_now(self) -> str:
        return self.utcnow().replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")

    def advance(self, seconds: float | int) -> float:
        """Advance by ``seconds`` and return the new timestamp."""

        amount = _validate_nonnegative_number(seconds, "seconds")
        with self._lock:
            started_at = self._now
            self._now += amount
            self.advances.append(
                ClockAdvance(
                    started_at=started_at,
                    seconds=amount,
                    finished_at=self._now,
                )
            )
            return self._now

    def set(self, value: float | int | datetime) -> float:
        """Set an explicit timestamp, useful for deterministic test setup."""

        new_value = self._coerce_start(value)
        with self._lock:
            self._now = new_value
            return self._now


@dataclass(frozen=True)
class SleepCall:
    """One fake sleep invocation, including its exact clock interval."""

    call_index: int
    delay: float
    started_at: float
    finished_at: float

    @property
    def seconds(self) -> float:
        return self.delay


class FakeSleeper:
    """Record-and-advance sleeper with no real-time waiting."""

    def __init__(self, clock: FakeClock | None = None) -> None:
        self.clock = clock if clock is not None else FakeClock()
        self.calls: list[SleepCall] = []

    def sleep(self, seconds: float | int) -> None:
        delay = _validate_nonnegative_number(seconds, "sleep seconds")
        started_at = self.clock.now()
        finished_at = self.clock.advance(delay)
        self.calls.append(
            SleepCall(
                call_index=len(self.calls) + 1,
                delay=delay,
                started_at=started_at,
                finished_at=finished_at,
            )
        )

    __call__ = sleep

    async def async_sleep(self, seconds: float | int) -> None:
        """Async counterpart that yields once but never waits wall-clock time."""

        self.sleep(seconds)
        await asyncio.sleep(0)

    asleep = async_sleep

    @property
    def delays(self) -> list[float]:
        return [call.delay for call in self.calls]

    @property
    def sleep_calls(self) -> list[SleepCall]:
        return self.calls

    @property
    def total_slept(self) -> float:
        return sum(self.delays)


# ---------------------------------------------------------------------------
# Request/event records and scripted scenarios
# ---------------------------------------------------------------------------


@dataclass
class FakeRequest:
    """Recorded request metadata and completion outcome."""

    request_id: int
    url: str
    method: str
    params: Any
    body: Any
    headers: FakeHeaders
    expected_kind: str | None
    attempt: int
    started_at: float
    scenario_index: int | None = None
    finished_at: float | None = None
    status_code: int | None = None
    outcome_type: str | None = None
    error_type: str | None = None
    error_message: str | None = None

    @property
    def completed(self) -> bool:
        return self.finished_at is not None

    @property
    def duration(self) -> float | None:
        if self.finished_at is None:
            return None
        return self.finished_at - self.started_at

    @property
    def kind(self) -> str | None:
        return self.expected_kind

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "url": self.url,
            "method": self.method,
            "params": copy.deepcopy(self.params),
            "body": copy.deepcopy(self.body),
            "headers": self.headers.to_dict(),
            "expected_kind": self.expected_kind,
            "attempt": self.attempt,
            "started_at": self.started_at,
            "scenario_index": self.scenario_index,
            "finished_at": self.finished_at,
            "status_code": self.status_code,
            "outcome_type": self.outcome_type,
            "error_type": self.error_type,
            "error_message": self.error_message,
        }


@dataclass(frozen=True)
class RequestEvent:
    """Start/finish event with active-request count at the event boundary."""

    type: str
    request_id: int
    timestamp: float
    url: str
    method: str
    expected_kind: str | None
    attempt: int
    active_requests: int
    status_code: int | None = None
    error_type: str | None = None

    @property
    def event_type(self) -> str:
        return self.type

    @property
    def kind(self) -> str:
        return self.type

    @property
    def active_count(self) -> int:
        return self.active_requests

    @property
    def time(self) -> float:
        return self.timestamp

    def __getitem__(self, key: str) -> Any:
        aliases = {
            "event": self.type,
            "event_type": self.type,
            "type": self.type,
            "time": self.timestamp,
            "timestamp": self.timestamp,
            "active": self.active_requests,
            "active_count": self.active_requests,
            "active_requests": self.active_requests,
        }
        if key in aliases:
            return aliases[key]
        try:
            return getattr(self, key)
        except AttributeError as exc:
            raise KeyError(key) from exc

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "request_id": self.request_id,
            "timestamp": self.timestamp,
            "url": self.url,
            "method": self.method,
            "expected_kind": self.expected_kind,
            "attempt": self.attempt,
            "active_requests": self.active_requests,
            "status_code": self.status_code,
            "error_type": self.error_type,
        }


OutcomeFactory: TypeAlias = Callable[[FakeRequest], Any]
FakeOutcome: TypeAlias = FakeResponse | BaseException | OutcomeFactory


@dataclass(frozen=True)
class ScenarioSelection:
    outcome: FakeOutcome
    index: int
    latency: float | None


class FakeScenario:
    """An ordered response/error script for one transport route."""

    def __init__(
        self,
        outcomes: FakeOutcome | Iterable[FakeOutcome],
        *,
        latency: float | None = None,
        repeat_last: bool = True,
        name: str | None = None,
    ) -> None:
        if isinstance(outcomes, FakeScenario):
            if latency is None:
                latency = outcomes.latency
            repeat_last = outcomes.repeat_last
            name = name or outcomes.name
            outcomes = outcomes.outcomes
        elif isinstance(outcomes, (list, tuple, deque)):
            outcomes = tuple(outcomes)
        elif isinstance(outcomes, (str, bytes, bytearray, Mapping)):
            outcomes = (outcomes,)  # type: ignore[assignment]
        else:
            try:
                outcomes = tuple(outcomes)  # type: ignore[arg-type]
            except TypeError:
                outcomes = (outcomes,)  # type: ignore[assignment]
        if not outcomes:
            raise ValueError("a fake scenario requires at least one outcome")
        self.outcomes = tuple(outcomes)
        self.latency = (
            None
            if latency is None
            else _validate_nonnegative_number(latency, "scenario latency")
        )
        self.repeat_last = bool(repeat_last)
        self.name = name
        self.calls = 0

    def select(self, request: FakeRequest) -> ScenarioSelection:
        if self.calls >= len(self.outcomes) and not self.repeat_last:
            raise ScenarioExhaustedError(
                f"fake scenario {self.name or '<unnamed>'!r} is exhausted"
            )
        index = min(self.calls, len(self.outcomes) - 1)
        self.calls += 1
        return ScenarioSelection(
            outcome=self.outcomes[index],
            index=index,
            latency=self.latency,
        )

    def reset(self) -> None:
        self.calls = 0

    @property
    def next_index(self) -> int:
        return min(self.calls, len(self.outcomes) - 1)


ResponseScenario = FakeScenario


class ErrorScenario(FakeScenario):
    """Convenience scenario for one repeated or scripted exception."""

    def __init__(
        self,
        error: BaseException | type[BaseException] | Iterable[BaseException],
        *,
        latency: float | None = None,
        repeat_last: bool = True,
        name: str | None = None,
    ) -> None:
        super().__init__(
            error,  # type: ignore[arg-type]
            latency=latency,
            repeat_last=repeat_last,
            name=name,
        )


@dataclass(frozen=True)
class OutcomeRecord:
    """Deterministic audit entry for a completed scripted outcome."""

    request_id: int
    url: str
    expected_kind: str | None
    scenario_index: int | None
    outcome_type: str
    status_code: int | None


class FakeRequestHandle:
    """A manually finishable request used to model overlapping requests."""

    def __init__(
        self,
        transport: "FakeTransport",
        request: FakeRequest,
        selection: ScenarioSelection,
        latency_override: float | None,
    ) -> None:
        self.transport = transport
        self.request = request
        self.selection = selection
        self.latency_override = latency_override
        self._finished = False

    @property
    def finished(self) -> bool:
        return self._finished

    def finish(self, outcome: Any = _UNSET) -> FakeResponse:
        if self._finished:
            raise RuntimeError("fake request has already been finished")
        self._finished = True
        return self.transport._finish_handle(self, outcome)

    def fail(self, error: BaseException | None = None) -> None:
        if self._finished:
            raise RuntimeError("fake request has already been finished")
        self._finished = True
        self.transport._finish_handle(
            self,
            error
            if error is not None
            else FakeNetworkError("fake request failed by explicit test command"),
        )

    def __enter__(self) -> "FakeRequestHandle":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        if not self._finished:
            self._finished = True
            try:
                self.transport._finish_handle(self, _UNSET)
            except BaseException:
                if exc_type is None:
                    raise
        return False

    async def __aenter__(self) -> "FakeRequestHandle":
        return self

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        return self.__exit__(exc_type, exc_value, traceback)


# ---------------------------------------------------------------------------
# Fake transport
# ---------------------------------------------------------------------------


_KIND_ALIASES: dict[str, str] = {
    "registry": "registry",
    "registry_page": "registry",
    "registry-page": "registry",
    "detail": "detail",
    "detail_page": "detail",
    "detail-page": "detail",
    "pdf": "pdf",
    "pdf_document": "pdf",
    "pdf-document": "pdf",
}


class FakeTransport:
    """Scripted, local-only transport with request activity instrumentation.

    Parameters may be supplied as one general mapping or as separate
    ``registry``, ``detail`` and ``pdf`` mappings.  Mapping keys are exact URLs;
    a ``(kind, url)`` tuple is also accepted in the general mapping.  Each
    value may be a :class:`FakeScenario`, one :class:`FakeResponse`/exception,
    or an iterable of outcomes.

    ``request`` completes immediately apart from deterministic fake latency.
    ``start_request`` and ``FakeRequestHandle.finish`` let tests deliberately
    overlap requests and inspect the active-request peak.  ``request_async``
    is an async convenience that yields once to let several scripted requests
    start without introducing real-time sleeps.
    """

    _CATEGORY_NAMES: ClassVar[frozenset[str]] = frozenset({"registry", "detail", "pdf"})

    def __init__(
        self,
        scenarios: Mapping[Any, Any] | None = None,
        *,
        responses: Mapping[Any, Any] | None = None,
        registry: Mapping[str, Any] | None = None,
        detail: Mapping[str, Any] | None = None,
        pdf: Mapping[str, Any] | None = None,
        clock: FakeClock | None = None,
        default_latency: float = 0.0,
        repeat_last: bool = True,
    ) -> None:
        self.clock = clock if clock is not None else FakeClock()
        self.default_latency = _validate_nonnegative_number(
            default_latency, "default_latency"
        )
        self.repeat_last = bool(repeat_last)
        self._routes: dict[tuple[str | None, str], FakeScenario] = {}
        self._lock = threading.RLock()
        self._active = 0
        self._peak_active = 0
        self.request_log: list[FakeRequest] = []
        self.events: list[RequestEvent] = []
        self.outcome_log: list[OutcomeRecord] = []
        self.response_order: list[FakeResponse | BaseException] = []

        if scenarios is not None:
            self._load_mapping(scenarios)
        if responses is not None:
            self._load_mapping(responses)
        if registry is not None:
            self._load_mapping(registry, kind="registry")
        if detail is not None:
            self._load_mapping(detail, kind="detail")
        if pdf is not None:
            self._load_mapping(pdf, kind="pdf")

    # ---- route registration -------------------------------------------------

    @staticmethod
    def _normalise_kind(kind: Any) -> str | None:
        if kind is None:
            return None
        value = getattr(kind, "value", kind)
        if not isinstance(value, str):
            raise TypeError("fake request kind must be a string or enum value")
        return _KIND_ALIASES.get(value.casefold(), value)

    def _load_mapping(
        self,
        mapping: Mapping[Any, Any],
        *,
        kind: str | None = None,
    ) -> None:
        if not isinstance(mapping, Mapping):
            raise TypeError("fake transport routes must be mappings")
        normalised_kind = self._normalise_kind(kind)
        for raw_key, value in mapping.items():
            if (
                normalised_kind is None
                and isinstance(raw_key, str)
                and raw_key.casefold() in self._CATEGORY_NAMES
                and isinstance(value, Mapping)
            ):
                self._load_mapping(value, kind=raw_key)
                continue
            route_kind = normalised_kind
            url = raw_key
            if route_kind is None and isinstance(raw_key, tuple) and len(raw_key) == 2:
                route_kind = self._normalise_kind(raw_key[0])
                url = raw_key[1]
            if not isinstance(url, str) or not url:
                raise TypeError("fake transport route URLs must be non-empty strings")
            self.register(url, value, kind=route_kind)

    def register(
        self,
        url: str,
        outcomes: Any,
        *,
        kind: str | None = None,
        latency: float | None = None,
        repeat_last: bool | None = None,
        name: str | None = None,
    ) -> FakeScenario:
        """Install or replace one exact URL route and return its scenario."""

        if not isinstance(url, str) or not url:
            raise ValueError("fake route URL must be a non-empty string")
        route_kind = self._normalise_kind(kind)
        if isinstance(outcomes, FakeScenario) and latency is None and repeat_last is None:
            scenario = outcomes
        else:
            scenario = FakeScenario(
                outcomes,
                latency=latency,
                repeat_last=self.repeat_last if repeat_last is None else repeat_last,
                name=name,
            )
        with self._lock:
            self._routes[(route_kind, url)] = scenario
        return scenario

    def register_response(
        self,
        url: str,
        response: FakeResponse,
        *,
        kind: str | None = None,
        latency: float | None = None,
    ) -> FakeScenario:
        return self.register(url, response, kind=kind, latency=latency)

    add_response = register_response

    def register_error(
        self,
        url: str,
        error: BaseException | type[BaseException],
        *,
        kind: str | None = None,
        latency: float | None = None,
    ) -> FakeScenario:
        return self.register(
            url,
            error,
            kind=kind,
            latency=latency,
        )

    add_error = register_error

    def register_registry(self, url: str, outcomes: Any, **kwargs: Any) -> FakeScenario:
        return self.register(url, outcomes, kind="registry", **kwargs)

    add_registry_response = register_registry

    def register_detail(self, url: str, outcomes: Any, **kwargs: Any) -> FakeScenario:
        return self.register(url, outcomes, kind="detail", **kwargs)

    add_detail_response = register_detail

    def register_pdf(self, url: str, outcomes: Any, **kwargs: Any) -> FakeScenario:
        return self.register(url, outcomes, kind="pdf", **kwargs)

    add_pdf_response = register_pdf

    # ---- request lifecycle --------------------------------------------------

    def _route_for(self, url: str, kind: str | None) -> FakeScenario | None:
        route_kind = self._normalise_kind(kind)
        with self._lock:
            exact = self._routes.get((route_kind, url))
            if exact is not None:
                return exact
            generic = self._routes.get((None, url))
            if generic is not None:
                return generic
            if route_kind is None:
                # A single kind-specific route is unambiguous even when the
                # caller does not pass expected_kind (useful for small tests).
                matches = [
                    scenario
                    for (registered_kind, registered_url), scenario in self._routes.items()
                    if registered_url == url and registered_kind is not None
                ]
                if len(matches) == 1:
                    return matches[0]
            return None

    @staticmethod
    def _copy_request_value(value: Any) -> Any:
        try:
            return copy.deepcopy(value)
        except Exception:
            return value

    def start_request(
        self,
        url: str,
        method: str = "GET",
        params: Any = None,
        body: Any = None,
        expected_kind: str | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        attempt: int | None = None,
        kind: str | None = None,
        request_kind: str | None = None,
        latency: float | None = None,
        **kwargs: Any,
    ) -> FakeRequestHandle:
        """Start and log a request without finishing it.

        ``kind`` and ``request_kind`` are aliases for ``expected_kind``.  A
        caller may pass ``json=...`` in ``kwargs`` as an alias for ``body``;
        this keeps the fake convenient for both sync and async clients.
        """

        if not isinstance(url, str) or not url:
            raise ValueError("fake request URL must be a non-empty string")
        if body is None and "json" in kwargs:
            body = kwargs.pop("json")
        if kwargs:
            unknown = ", ".join(sorted(kwargs))
            raise TypeError(f"unexpected fake request arguments: {unknown}")
        selected_kind = request_kind if request_kind is not None else kind
        if expected_kind is not None and selected_kind is not None:
            if self._normalise_kind(expected_kind) != self._normalise_kind(selected_kind):
                raise ValueError("expected_kind and kind disagree")
        selected_kind = expected_kind if expected_kind is not None else selected_kind
        normalised_kind = self._normalise_kind(selected_kind)
        if attempt is None:
            attempt_number = 1
        elif isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ValueError("attempt must be a positive integer")
        else:
            attempt_number = attempt
        if latency is not None:
            latency = _validate_nonnegative_number(latency, "request latency")

        request = FakeRequest(
            request_id=len(self.request_log) + 1,
            url=url,
            method=str(method).upper(),
            params=self._copy_request_value(params),
            body=self._copy_request_value(body),
            headers=FakeHeaders(headers),
            expected_kind=normalised_kind,
            attempt=attempt_number,
            started_at=self.clock.now(),
        )
        scenario = self._route_for(url, normalised_kind)
        if scenario is None:
            selection = ScenarioSelection(
                outcome=FakeNetworkError(f"no fake response configured for {url}"),
                index=-1,
                latency=None,
            )
        else:
            try:
                selection = scenario.select(request)
            except BaseException as error:
                selection = ScenarioSelection(outcome=error, index=-1, latency=None)
            request.scenario_index = selection.index if selection.index >= 0 else None

        with self._lock:
            self.request_log.append(request)
            self._active += 1
            self._peak_active = max(self._peak_active, self._active)
            self.events.append(
                RequestEvent(
                    type="started",
                    request_id=request.request_id,
                    timestamp=request.started_at,
                    url=request.url,
                    method=request.method,
                    expected_kind=request.expected_kind,
                    attempt=request.attempt,
                    active_requests=self._active,
                )
            )
        return FakeRequestHandle(self, request, selection, latency)

    def _materialise_outcome(self, outcome: Any, request: FakeRequest) -> Any:
        if isinstance(outcome, type) and issubclass(outcome, BaseException):
            return outcome()
        if callable(outcome) and not isinstance(outcome, FakeResponse):
            return self._materialise_outcome(outcome(request), request)
        return outcome

    def _effective_latency(
        self,
        handle: FakeRequestHandle,
        outcome: Any,
    ) -> float:
        if handle.latency_override is not None:
            return handle.latency_override
        if handle.selection.latency is not None:
            return handle.selection.latency
        if isinstance(outcome, FakeResponse):
            return outcome.latency
        return self.default_latency

    def _finish_handle(self, handle: FakeRequestHandle, override: Any) -> FakeResponse:
        request = handle.request
        outcome_for_log: FakeResponse | BaseException | None = None
        try:
            scripted = handle.selection.outcome if override is _UNSET else override
            materialised = self._materialise_outcome(scripted, request)
            delay = self._effective_latency(handle, materialised)
            if delay:
                self.clock.advance(delay)
            if isinstance(materialised, BaseException):
                raise materialised
            if not isinstance(materialised, FakeResponse):
                raise FakeTransportError(
                    "fake scenario returned "
                    f"{type(materialised).__name__}, expected FakeResponse or exception"
                )
            response = materialised.for_request(
                request,
                url=request.url,
                request_kind=request.expected_kind,
            )
            request.status_code = response.status_code
            request.outcome_type = "response"
            outcome_for_log = response
            return response
        except BaseException as error:
            request.outcome_type = "error"
            request.error_type = type(error).__name__
            request.error_message = str(error) or type(error).__name__
            outcome_for_log = error
            raise
        finally:
            request.finished_at = self.clock.now()
            with self._lock:
                self._active -= 1
                if self._active < 0:  # pragma: no cover - defensive invariant
                    self._active = 0
                self.events.append(
                    RequestEvent(
                        type="finished",
                        request_id=request.request_id,
                        timestamp=request.finished_at,
                        url=request.url,
                        method=request.method,
                        expected_kind=request.expected_kind,
                        attempt=request.attempt,
                        active_requests=self._active,
                        status_code=request.status_code,
                        error_type=request.error_type,
                    )
                )
                if outcome_for_log is not None:
                    self.response_order.append(outcome_for_log)
                    self.outcome_log.append(
                        OutcomeRecord(
                            request_id=request.request_id,
                            url=request.url,
                            expected_kind=request.expected_kind,
                            scenario_index=request.scenario_index,
                            outcome_type=request.outcome_type or "unknown",
                            status_code=request.status_code,
                        )
                    )

    def finish_request(
        self,
        handle: FakeRequestHandle,
        outcome: Any = _UNSET,
    ) -> FakeResponse:
        """Finish a handle explicitly; useful for overlap/concurrency tests."""

        if not isinstance(handle, FakeRequestHandle) or handle.transport is not self:
            raise TypeError("handle must belong to this FakeTransport")
        if handle.finished:
            raise RuntimeError("fake request has already been finished")
        handle._finished = True
        return self._finish_handle(handle, outcome)

    def request(
        self,
        url: str,
        method: str = "GET",
        params: Any = None,
        body: Any = None,
        expected_kind: str | None = None,
        **kwargs: Any,
    ) -> FakeResponse:
        """Complete one scripted request immediately and return its response."""

        handle = self.start_request(
            url,
            method=method,
            params=params,
            body=body,
            expected_kind=expected_kind,
            **kwargs,
        )
        return handle.finish()

    send = request

    def get(
        self,
        url: str,
        *,
        params: Any = None,
        headers: Mapping[str, str] | None = None,
        expected_kind: str | None = None,
        **kwargs: Any,
    ) -> FakeResponse:
        return self.request(
            url,
            method="GET",
            params=params,
            expected_kind=expected_kind,
            headers=headers,
            **kwargs,
        )

    async def request_async(
        self,
        url: str,
        method: str = "GET",
        params: Any = None,
        body: Any = None,
        expected_kind: str | None = None,
        **kwargs: Any,
    ) -> FakeResponse:
        """Async request with an event-loop yield but no wall-clock delay."""

        handle = self.start_request(
            url,
            method=method,
            params=params,
            body=body,
            expected_kind=expected_kind,
            **kwargs,
        )
        await asyncio.sleep(0)
        return handle.finish()

    arequest = request_async

    async def aget(self, url: str, **kwargs: Any) -> FakeResponse:
        return await self.request_async(url, method="GET", **kwargs)

    # ---- inspection/reset helpers ------------------------------------------

    @property
    def active_requests(self) -> int:
        with self._lock:
            return self._active

    @property
    def active_request_count(self) -> int:
        return self.active_requests

    @property
    def peak_active_requests(self) -> int:
        with self._lock:
            return self._peak_active

    @property
    def peak_concurrency(self) -> int:
        return self.peak_active_requests

    @property
    def max_active_requests(self) -> int:
        return self.peak_active_requests

    @property
    def requests(self) -> list[FakeRequest]:
        return self.request_log

    @property
    def request_events(self) -> list[RequestEvent]:
        return self.events

    def calls_for(self, url: str, *, kind: str | None = None) -> list[FakeRequest]:
        normalised_kind = self._normalise_kind(kind)
        return [
            request
            for request in self.request_log
            if request.url == url
            and (normalised_kind is None or request.expected_kind == normalised_kind)
        ]

    def clear_log(self, *, reset_routes: bool = False) -> None:
        """Clear observations without touching the fake clock by default."""

        with self._lock:
            if self._active:
                raise RuntimeError("cannot clear a log while fake requests are active")
            self.request_log.clear()
            self.events.clear()
            self.outcome_log.clear()
            self.response_order.clear()
            self._peak_active = 0
            if reset_routes:
                for scenario in self._routes.values():
                    scenario.reset()

    def __enter__(self) -> "FakeTransport":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        if self.active_requests:
            raise RuntimeError("fake transport closed with active requests")
        return False

    async def __aenter__(self) -> "FakeTransport":
        return self

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        return self.__exit__(exc_type, exc_value, traceback)


# Friendly aliases used by tests that prefer the terminology from the design.
SourceResponse = FakeResponse
NetworkException = FakeNetworkError
FakeRequestEvent = RequestEvent
FakeSleep = FakeSleeper


def response_scenario(
    response: FakeResponse | Iterable[FakeResponse],
    *,
    latency: float | None = None,
    repeat_last: bool = True,
    name: str | None = None,
) -> FakeScenario:
    """Build a response-only scenario with explicit readable intent."""

    return FakeScenario(
        response,
        latency=latency,
        repeat_last=repeat_last,
        name=name,
    )


def error_scenario(
    error: BaseException | type[BaseException] | Iterable[BaseException],
    *,
    latency: float | None = None,
    repeat_last: bool = True,
    name: str | None = None,
) -> ErrorScenario:
    """Build an exception-only scenario with explicit readable intent."""

    return ErrorScenario(
        error,
        latency=latency,
        repeat_last=repeat_last,
        name=name,
    )


__all__ = [
    "ClockAdvance",
    "ErrorScenario",
    "FakeClock",
    "FakeHTTPStatusError",
    "FakeHeaders",
    "FakeNetworkError",
    "FakeOutcome",
    "FakeRequest",
    "FakeRequestEvent",
    "FakeRequestHandle",
    "FakeResponse",
    "FakeScenario",
    "FakeSleep",
    "FakeSleeper",
    "FakeTransport",
    "FakeTransportError",
    "NetworkException",
    "OutcomeRecord",
    "RequestEvent",
    "ResponseScenario",
    "ScenarioExhaustedError",
    "ScenarioSelection",
    "SleepCall",
    "SourceResponse",
    "error_scenario",
    "response_scenario",
]
