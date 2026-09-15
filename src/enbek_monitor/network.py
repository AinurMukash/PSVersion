"""Transport-neutral network client for the local Enbek monitor.

The client owns request policy, not source parsing.  It accepts a small
transport contract (an object with ``request`` or ``get``), so the same code
works with a real HTTP adapter and the fixture transport used by local tests.
Retries are per URL, request activity is bounded by one shared semaphore, and
retry sleeps happen outside the active-request interval.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeAlias
from urllib.parse import urlsplit

from .config import NetworkConfig
from .errors import SourceError


Body: TypeAlias = bytes | bytearray | memoryview | str | Mapping[str, Any] | Sequence[Any] | None


class RetryableError(OSError):
    """A transport-level failure that is safe to retry."""


# Names matching the design vocabulary are intentionally exported as aliases.
Retryable_Error = RetryableError


@dataclass(frozen=True)
class SourceResponse:
    """Minimal response contract exposed to collectors and parsers.

    ``raw`` retains the injected transport response for adapters that need
    transport-specific details.  It is deliberately not serialized by the
    network client and never contains client configuration such as a proxy.
    """

    url: str
    status_code: int
    headers: Mapping[str, str]
    content: bytes
    raw: Any = None
    request_kind: str | None = None
    attempts: int = 1

    @property
    def status(self) -> int:
        return self.status_code

    @property
    def body(self) -> bytes:
        return self.content

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def is_success(self) -> bool:
        return self.ok

    def json(self) -> Any:
        import json

        return json.loads(self.content.decode("utf-8"))


@dataclass(frozen=True)
class _AttemptResult:
    response: SourceResponse | None = None
    error: SourceError | None = None
    retryable: bool = False


TransportCallable: TypeAlias = Callable[..., Any]


class NetworkClient:
    """Execute bounded, retrying requests through an injected transport.

    The synchronous :meth:`request` method is the primary contract.  The
    :meth:`request_async` counterpart accepts both synchronous transports and
    transports returning awaitables.  ``proxy`` is forwarded only when the
    transport explicitly accepts it; it is never included in a response or
    error payload.  Callers may pass ``os.environ.get("PROXY")`` explicitly,
    but this module does not read environment variables itself.
    """

    def __init__(
        self,
        transport: Any,
        *,
        clock: Any | None = None,
        sleeper: Any | None = None,
        network_config: NetworkConfig | Any | None = None,
        max_attempts: int | None = None,
        retry_delays: Sequence[float] | None = None,
        max_concurrency: int | None = None,
        proxy: str | None = None,
        registry_url: str | None = None,
        allowed_hosts: Sequence[str] | None = None,
        fixture_mode: bool | None = None,
    ) -> None:
        if transport is None:
            raise ValueError("transport is required")
        self.transport = transport
        self.clock = clock
        self._config = network_config

        if network_config is not None:
            max_attempts = (
                getattr(network_config, "max_attempts", None)
                if max_attempts is None
                else max_attempts
            )
            retry_delays = (
                getattr(network_config, "retry_delays", None)
                if retry_delays is None
                else retry_delays
            )
            max_concurrency = (
                getattr(network_config, "max_concurrency", None)
                if max_concurrency is None
                else max_concurrency
            )

        self.max_attempts = self._validate_attempts(
            3 if max_attempts is None else max_attempts
        )
        self.retry_delays = self._validate_delays(
            (2.0, 5.0) if retry_delays is None else retry_delays
        )
        self.max_concurrency = self._validate_concurrency(
            5 if max_concurrency is None else max_concurrency
        )
        if proxy is not None and (not isinstance(proxy, str) or not proxy.strip()):
            raise ValueError("proxy must be a non-empty string when supplied")
        self.proxy = proxy
        self._sleeper = sleeper if sleeper is not None else time.sleep
        self._semaphore = __import__("threading").BoundedSemaphore(self.max_concurrency)

        self._registry_host = self._host_from_url(registry_url)
        configured_hosts = allowed_hosts
        if configured_hosts is None and self._registry_host is not None:
            configured_hosts = (self._registry_host,)
        self.allowed_hosts = frozenset(
            self._normalise_host(host) for host in (configured_hosts or ())
        )
        if any(not host for host in self.allowed_hosts):
            raise ValueError("allowed_hosts must contain non-empty host names")
        self.fixture_mode = (
            self._looks_like_fixture_transport(transport)
            if fixture_mode is None
            else bool(fixture_mode)
        )

    @classmethod
    def from_config(
        cls,
        config: Any,
        transport: Any,
        **kwargs: Any,
    ) -> "NetworkClient":
        """Build a client from ``MonitorConfig`` or ``NetworkConfig``.

        The adapter copies only network policy and the exact registry host;
        unrelated configuration, including any future secret-like fields, is
        not inspected or persisted.
        """

        network = getattr(config, "network", config)
        registry_url = kwargs.pop("registry_url", getattr(config, "registry_url", None))
        return cls(
            transport,
            network_config=network,
            registry_url=registry_url,
            **kwargs,
        )

    @staticmethod
    def _validate_attempts(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 3:
            raise ValueError("max_attempts must be an integer from 1 through 3")
        return value

    @staticmethod
    def _validate_concurrency(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
            raise ValueError("max_concurrency must be an integer from 1 through 5")
        return value

    @staticmethod
    def _validate_delays(value: Sequence[float]) -> tuple[float, float]:
        if isinstance(value, (str, bytes)) or len(value) != 2:
            raise ValueError("retry_delays must contain exactly two numbers")
        result: list[float] = []
        for delay in value:
            if isinstance(delay, bool) or not isinstance(delay, (int, float)) or delay < 0:
                raise ValueError("retry_delays must contain non-negative numbers")
            result.append(float(delay))
        return result[0], result[1]

    @staticmethod
    def _normalise_host(value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            return ""
        text = value.strip().lower()
        if "://" in text:
            text = urlsplit(text).hostname or ""
        return text.rstrip(".")

    @classmethod
    def _host_from_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        host = parsed.hostname
        return cls._normalise_host(host) if host else None

    @staticmethod
    def _looks_like_fixture_transport(transport: Any) -> bool:
        marker = getattr(transport, "is_fixture", None)
        if marker is not None:
            return bool(marker)
        name = transport.__class__.__name__.casefold()
        module = transport.__class__.__module__.casefold()
        return "fake" in name or module.startswith("tests.")

    def _host_error(self, url: str) -> SourceError | None:
        parsed = urlsplit(url)
        if parsed.scheme in {"fixture", "file"}:
            return None
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return SourceError(
                "URL must be an absolute HTTP(S) URL",
                url=url,
                attempts=1,
                stage="network",
            )
        # A fixture/injected transport is allowed to use local test hosts. A
        # live adapter must have an explicit configured host allow-list; no
        # external host is inferred from proxy configuration.
        if self.fixture_mode:
            return None
        if not self.allowed_hosts or self._normalise_host(parsed.hostname) not in self.allowed_hosts:
            return SourceError(
                "request host is not the configured Enbek host",
                url=url,
                attempts=1,
                stage="network",
            )
        return None

    @staticmethod
    def _callable_for(transport: Any, method: str) -> tuple[TransportCallable, bool]:
        request = getattr(transport, "request", None)
        if callable(request):
            return request, False
        get = getattr(transport, "get", None)
        if method.upper() == "GET" and callable(get):
            return get, True
        if callable(transport):
            return transport, False
        raise TypeError("transport must provide request(), get(), or be callable")

    @staticmethod
    def _transport_signature(call: TransportCallable) -> inspect.Signature | None:
        try:
            return inspect.signature(call)
        except (TypeError, ValueError):
            return None

    def _invoke_transport(
        self,
        url: str,
        *,
        method: str,
        params: Any,
        body: Any,
        headers: Mapping[str, str] | None,
        expected_kind: str | None,
        attempt: int | None = None,
    ) -> Any:
        call, is_get = self._callable_for(self.transport, method)
        signature = self._transport_signature(call)
        accepted = set(signature.parameters) if signature is not None else set()
        has_var_kwargs = bool(
            signature
            and any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in signature.parameters.values()
            )
        )
        values: dict[str, Any] = {
            "method": method,
            "params": params,
            "headers": headers,
            "expected_kind": expected_kind,
        }
        if attempt is not None and ("attempt" in accepted or has_var_kwargs or signature is None):
            values["attempt"] = attempt
        if body is not None:
            if "body" in accepted:
                values["body"] = body
            elif "content" in accepted:
                values["content"] = body
            elif "json" in accepted:
                values["json"] = body
            elif has_var_kwargs or signature is None:
                values["body"] = body
        # Proxy is intentionally passed only to an explicitly proxy-aware
        # adapter, never merely because it has a catch-all **kwargs.
        if self.proxy is not None and "proxy" in accepted:
            values["proxy"] = self.proxy
        if signature is not None:
            if is_get:
                values.pop("method", None)
            values = {
                key: value
                for key, value in values.items()
                if key in accepted or has_var_kwargs
            }
        elif is_get:
            values.pop("method", None)
        return call(url, **values)

    @staticmethod
    def _response_parts(value: Any) -> tuple[int, Mapping[str, str], bytes, str | None, str | None]:
        if isinstance(value, SourceResponse):
            return value.status_code, value.headers, value.content, value.url, value.request_kind
        status = getattr(value, "status_code", getattr(value, "status", None))
        if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
            raise ValueError("transport response has no valid HTTP status")
        headers = getattr(value, "headers", {})
        if headers is None or not isinstance(headers, Mapping):
            raise ValueError("transport response has malformed headers")
        body = getattr(value, "content", None)
        if body is None and hasattr(value, "body"):
            body = getattr(value, "body")
        if not isinstance(body, (bytes, bytearray, memoryview)):
            raise ValueError("transport response body must be bytes")
        response_url = getattr(value, "url", None)
        if response_url is not None and not isinstance(response_url, str):
            response_url = None
        kind = getattr(value, "request_kind", None)
        if kind is not None and not isinstance(kind, str):
            kind = None
        return status, dict(headers), bytes(body), response_url, kind

    def _make_response(self, value: Any, url: str, attempt: int, expected_kind: str | None) -> SourceResponse:
        status, headers, content, response_url, response_kind = self._response_parts(value)
        return SourceResponse(
            url=response_url or url,
            status_code=status,
            headers=headers,
            content=content,
            raw=value,
            request_kind=response_kind or expected_kind,
            attempts=attempt,
        )

    def _reason(self, error: BaseException) -> str:
        reason = str(error).strip() or error.__class__.__name__
        # A proxy can contain credentials. Never echo the configured value in
        # a SourceError reason, even if an adapter includes it in an exception.
        if self.proxy:
            reason = reason.replace(self.proxy, "[proxy]")
        return reason

    @staticmethod
    def _exception_status(error: BaseException) -> int | None:
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", getattr(response, "status", None))
        return status if isinstance(status, int) and 100 <= status <= 599 else None

    @classmethod
    def _is_retryable_exception(cls, error: BaseException) -> bool:
        status = cls._exception_status(error)
        if status is not None:
            return 500 <= status <= 599
        if isinstance(error, RetryableError):
            return True
        if isinstance(error, (TimeoutError, ConnectionError, OSError)):
            return True
        return type(error).__name__ in {
            "ConnectError",
            "ConnectTimeout",
            "PoolTimeout",
            "ReadError",
            "ReadTimeout",
            "WriteError",
            "WriteTimeout",
            "NetworkError",
            "TimeoutException",
        }

    def _attempt_sync(self, **kwargs: Any) -> _AttemptResult:
        url = kwargs["url"]
        attempt = kwargs["attempt"]
        try:
            with self._semaphore:
                value = self._invoke_transport(
                    url=url,
                    method=kwargs["method"],
                    params=kwargs["params"],
                    body=kwargs["body"],
                    headers=kwargs["headers"],
                    expected_kind=kwargs.get("expected_kind"),
                    attempt=attempt,
                )
                if inspect.isawaitable(value):
                    value = self._run_awaitable_sync(value)
        except BaseException as error:
            retryable = self._is_retryable_exception(error)
            return _AttemptResult(
                error=SourceError(
                    self._reason(error),
                    url=url,
                    attempts=attempt,
                    status=self._exception_status(error),
                    stage="network",
                ),
                retryable=retryable,
            )
        try:
            response = self._make_response(value, url, attempt, kwargs.get("expected_kind"))
        except BaseException as error:
            return _AttemptResult(
                error=SourceError(
                    f"malformed transport response: {self._reason(error)}",
                    url=url,
                    attempts=attempt,
                    stage="network",
                )
            )
        if 200 <= response.status_code < 300:
            return _AttemptResult(response=response)
        retryable = 500 <= response.status_code <= 599
        return _AttemptResult(
            error=SourceError(
                f"HTTP status {response.status_code}",
                url=url,
                attempts=attempt,
                status=response.status_code,
                stage="network",
            ),
            retryable=retryable,
        )

    @staticmethod
    def _run_awaitable_sync(value: Awaitable[Any]) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(value)
        raise RuntimeError("async transport requires request_async while an event loop is running")

    def _sleep_sync(self, delay: float) -> None:
        sleeper = self._sleeper
        call = getattr(sleeper, "sleep", sleeper)
        result = call(delay)
        if inspect.isawaitable(result):
            self._run_awaitable_sync(result)

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        params: Any = None,
        body: Any = None,
        headers: Mapping[str, str] | None = None,
        expected_kind: str | None = None,
    ) -> SourceResponse | SourceError:
        """Perform one logical request and return a response or structured error."""

        if not isinstance(url, str) or not url.strip():
            return SourceError("URL must be a non-empty string", url=str(url), attempts=1)
        host_error = self._host_error(url)
        if host_error is not None:
            return host_error
        last: _AttemptResult | None = None
        for attempt in range(1, self.max_attempts + 1):
            last = self._attempt_sync(
                url=url,
                method=method.upper(),
                params=params,
                body=body,
                headers=headers,
                expected_kind=expected_kind,
                attempt=attempt,
            )
            if last.response is not None:
                return last.response
            if not last.retryable or attempt >= self.max_attempts:
                return last.error  # type: ignore[return-value]
            self._sleep_sync(self.retry_delays[attempt - 1])
        raise AssertionError("request loop must return a response or error")

    def get(self, url: str, **kwargs: Any) -> SourceResponse | SourceError:
        kwargs.setdefault("method", "GET")
        return self.request(url, **kwargs)

    async def _attempt_async(self, **kwargs: Any) -> _AttemptResult:
        url = kwargs["url"]
        attempt = kwargs["attempt"]
        await asyncio.to_thread(self._semaphore.acquire)
        try:
            try:
                value = self._invoke_transport(
                    url=url,
                    method=kwargs["method"],
                    params=kwargs["params"],
                    body=kwargs["body"],
                    headers=kwargs["headers"],
                    expected_kind=kwargs.get("expected_kind"),
                    attempt=attempt,
                )
                if inspect.isawaitable(value):
                    value = await value
            except BaseException as error:
                retryable = self._is_retryable_exception(error)
                return _AttemptResult(
                    error=SourceError(self._reason(error), url=url, attempts=attempt, stage="network"),
                    retryable=retryable,
                )
        finally:
            self._semaphore.release()
        try:
            response = self._make_response(value, url, attempt, kwargs.get("expected_kind"))
        except BaseException as error:
            return _AttemptResult(
                error=SourceError(
                    f"malformed transport response: {self._reason(error)}",
                    url=url,
                    attempts=attempt,
                    stage="network",
                )
            )
        if 200 <= response.status_code < 300:
            return _AttemptResult(response=response)
        retryable = 500 <= response.status_code <= 599
        return _AttemptResult(
            error=SourceError(
                f"HTTP status {response.status_code}",
                url=url,
                attempts=attempt,
                status=response.status_code,
                stage="network",
            ),
            retryable=retryable,
        )

    async def _sleep_async(self, delay: float) -> None:
        sleeper = self._sleeper
        call = getattr(sleeper, "async_sleep", None)
        if call is None:
            call = getattr(sleeper, "asleep", None)
        if call is None:
            call = getattr(sleeper, "sleep", sleeper)
        result = call(delay)
        if inspect.isawaitable(result):
            await result
        else:
            await asyncio.to_thread(lambda: None)

    async def request_async(self, url: str, **kwargs: Any) -> SourceResponse | SourceError:
        """Async-friendly counterpart accepting sync or awaitable transports."""

        if not isinstance(url, str) or not url.strip():
            return SourceError("URL must be a non-empty string", url=str(url), attempts=1)
        host_error = self._host_error(url)
        if host_error is not None:
            return host_error
        kwargs = {
            "url": url,
            "method": str(kwargs.pop("method", "GET")).upper(),
            "params": kwargs.pop("params", None),
            "body": kwargs.pop("body", None),
            "headers": kwargs.pop("headers", None),
            "expected_kind": kwargs.pop("expected_kind", None),
        }
        if kwargs:
            raise TypeError(f"unexpected request arguments: {', '.join(sorted(kwargs))}")
        for attempt in range(1, self.max_attempts + 1):
            result = await self._attempt_async(**kwargs, attempt=attempt)
            if result.response is not None:
                return result.response
            if not result.retryable or attempt >= self.max_attempts:
                return result.error  # type: ignore[return-value]
            await self._sleep_async(self.retry_delays[attempt - 1])
        raise AssertionError("request loop must return a response or error")

    async def aget(self, url: str, **kwargs: Any) -> SourceResponse | SourceError:
        kwargs.setdefault("method", "GET")
        return await self.request_async(url, **kwargs)


# Vocabulary aliases used by the requirements and by collector code.
Network_Client = NetworkClient


__all__ = [
    "NetworkClient",
    "Network_Client",
    "RetryableError",
    "Retryable_Error",
    "SourceResponse",
]
