from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from typing import Any

import pytest

from enbek_monitor.errors import SourceError
from enbek_monitor.network import NetworkClient, SourceResponse
from tests.support.fakes import (
    FakeClock,
    FakeNetworkError,
    FakeResponse,
    FakeSleeper,
    FakeTransport,
)


REGISTRY_URL = "https://career.enbek.kz/ru/ps-registry"


def test_success_returns_transport_neutral_source_response() -> None:
    transport = FakeTransport({REGISTRY_URL: FakeResponse(200, body=b"registry")})
    client = NetworkClient(transport, fixture_mode=True)

    result = client.request(REGISTRY_URL, expected_kind="registry")

    assert isinstance(result, SourceResponse)
    assert result.url == REGISTRY_URL
    assert result.status_code == 200
    assert result.content == b"registry"
    assert result.attempts == 1
    assert transport.requests[0].attempt == 1
    assert transport.peak_active_requests == 1


def test_retryable_http_and_network_failures_use_exact_2_and_5_second_delays() -> None:
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    transport = FakeTransport(
        {REGISTRY_URL: [
            FakeResponse(500, body=b"temporary"),
            FakeNetworkError("connection reset"),
            FakeResponse(200, body=b"ok"),
        ]}
    )
    client = NetworkClient(transport, clock=clock, sleeper=sleeper, fixture_mode=True)

    result = client.request(REGISTRY_URL)

    assert isinstance(result, SourceResponse)
    assert result.attempts == 3
    assert sleeper.delays == [2.0, 5.0]
    assert [request.attempt for request in transport.requests] == [1, 2, 3]
    assert [request.started_at for request in transport.requests] == [0.0, 2.0, 7.0]
    assert clock.now() == 7.0


def test_non_retryable_4xx_returns_source_error_without_sleep_or_retry() -> None:
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    transport = FakeTransport({REGISTRY_URL: FakeResponse(404, body=b"not found")})
    client = NetworkClient(transport, clock=clock, sleeper=sleeper, fixture_mode=True)

    result = client.request(REGISTRY_URL)

    assert isinstance(result, SourceError)
    assert result.url == REGISTRY_URL
    assert result.status == 404
    assert result.attempts == 1
    assert result.reason == "HTTP status 404"
    assert sleeper.delays == []
    assert len(transport.requests) == 1


def test_exhausted_5xx_returns_last_error_and_never_exceeds_three_attempts() -> None:
    sleeper = FakeSleeper(FakeClock())
    transport = FakeTransport({REGISTRY_URL: FakeResponse(503, body=b"busy")})
    client = NetworkClient(transport, sleeper=sleeper, fixture_mode=True)

    result = client.request(REGISTRY_URL)

    assert isinstance(result, SourceError)
    assert result.url == REGISTRY_URL
    assert result.status == 503
    assert result.attempts == 3
    assert result.reason == "HTTP status 503"
    assert [request.attempt for request in transport.requests] == [1, 2, 3]
    assert sleeper.delays == [2.0, 5.0]


@dataclass
class MalformedResponse:
    content: str = "not bytes"


def test_malformed_transport_response_is_source_error_and_not_retried() -> None:
    class Transport:
        def request(self, url: str, **_: Any) -> MalformedResponse:
            return MalformedResponse()

    sleeper = FakeSleeper(FakeClock())
    client = NetworkClient(Transport(), sleeper=sleeper, fixture_mode=True)

    result = client.request(REGISTRY_URL)

    assert isinstance(result, SourceError)
    assert result.url == REGISTRY_URL
    assert result.attempts == 1
    assert "malformed transport response" in result.reason
    assert sleeper.delays == []


def test_active_requests_are_bounded_to_five_even_when_ten_threads_start() -> None:
    class BlockingTransport:
        def __init__(self) -> None:
            self.lock = threading.Lock()
            self.ready_for_release = threading.Event()
            self.active = 0
            self.peak = 0

        def request(self, url: str, **_: Any) -> FakeResponse:
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
                if self.active == 5:
                    self.ready_for_release.set()
            self.ready_for_release.wait(timeout=2)
            time.sleep(0.002)
            with self.lock:
                self.active -= 1
            return FakeResponse(200, body=b"ok", url=url)

    transport = BlockingTransport()
    client = NetworkClient(transport, fixture_mode=True)
    results: list[SourceResponse | SourceError] = []
    threads = [
        threading.Thread(
            target=lambda url=f"https://fixture.invalid/{index}": results.append(
                client.request(url)
            )
        )
        for index in range(10)
    ]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert all(not thread.is_alive() for thread in threads)
    assert len(results) == 10
    assert all(isinstance(result, SourceResponse) for result in results)
    assert transport.peak == 5


def test_proxy_is_optional_and_only_forwarded_to_transport_that_accepts_it() -> None:
    class ProxyAwareTransport:
        def __init__(self) -> None:
            self.seen: list[str | None] = []

        def request(self, url: str, *, proxy: str | None = None) -> FakeResponse:
            self.seen.append(proxy)
            return FakeResponse(200, body=b"ok")

    transport = ProxyAwareTransport()
    client = NetworkClient(
        transport,
        proxy="http://proxy.invalid:8080",
        fixture_mode=True,
    )

    result = client.request(REGISTRY_URL)

    assert isinstance(result, SourceResponse)
    assert transport.seen == ["http://proxy.invalid:8080"]
    assert not hasattr(result, "proxy")


def test_configured_live_host_is_enforced_without_contacting_other_hosts() -> None:
    class CountingTransport:
        calls = 0

        def request(self, url: str) -> FakeResponse:
            self.calls += 1
            return FakeResponse(200, body=b"unexpected")

    transport = CountingTransport()
    client = NetworkClient(
        transport,
        registry_url=REGISTRY_URL,
        fixture_mode=False,
    )

    result = client.request("https://example.invalid/registry")

    assert isinstance(result, SourceError)
    assert "configured Enbek host" in result.reason
    assert result.attempts == 1
    assert transport.calls == 0


@pytest.mark.parametrize("status", [400, 404, 429])
def test_all_non_5xx_http_failures_are_not_retried(status: int) -> None:
    sleeper = FakeSleeper(FakeClock())
    transport = FakeTransport({REGISTRY_URL: FakeResponse(status, body=b"error")})
    client = NetworkClient(transport, sleeper=sleeper, fixture_mode=True)

    result = client.request(REGISTRY_URL)

    assert isinstance(result, SourceError)
    assert result.attempts == 1
    assert sleeper.delays == []
    assert len(transport.requests) == 1


def test_async_request_accepts_async_transport_and_uses_injected_sleeper() -> None:
    class AsyncTransport:
        def __init__(self) -> None:
            self.attempts = 0

        async def request(self, url: str, **_: Any) -> FakeResponse:
            self.attempts += 1
            if self.attempts == 1:
                return FakeResponse(502, body=b"retry")
            return FakeResponse(200, body=b"ok")

    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    transport = AsyncTransport()
    client = NetworkClient(transport, sleeper=sleeper, fixture_mode=True)

    result = asyncio.run(client.request_async(REGISTRY_URL))

    assert isinstance(result, SourceResponse)
    assert result.attempts == 2
    assert transport.attempts == 2
    assert sleeper.delays == [2.0]
