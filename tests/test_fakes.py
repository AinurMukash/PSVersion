"""Focused deterministic checks for the local network test doubles."""

from __future__ import annotations

import asyncio

import pytest

from tests.support.fakes import (
    FakeClock,
    FakeNetworkError,
    FakeResponse,
    FakeSleeper,
    FakeTransport,
)


def test_fake_sleeper_records_retry_delays_without_wall_clock_waiting() -> None:
    clock = FakeClock()
    sleeper = FakeSleeper(clock)

    sleeper.sleep(2)
    sleeper.sleep(5)

    assert sleeper.delays == [2.0, 5.0]
    assert [call.started_at for call in sleeper.calls] == [0.0, 2.0]
    assert [call.finished_at for call in sleeper.calls] == [2.0, 7.0]
    assert clock.now() == 7.0
    assert [advance.seconds for advance in clock.advances] == [2.0, 5.0]


def test_fake_transport_preserves_scripted_registry_response_and_error_order() -> None:
    url = "https://fixture.invalid/registry"
    transport = FakeTransport(
        registry={
            url: [
                FakeResponse(500, body=b"server-error"),
                FakeNetworkError("connection reset"),
                FakeResponse(200, body=b"registry-page"),
            ]
        }
    )

    first = transport.request(url, expected_kind="registry", attempt=1)
    assert first.status_code == 500

    with pytest.raises(FakeNetworkError, match="connection reset"):
        transport.request(url, expected_kind="registry", attempt=2)

    third = transport.request(url, expected_kind="registry", attempt=3)
    assert third.status_code == 200
    assert third.content == b"registry-page"
    assert [request.scenario_index for request in transport.requests] == [0, 1, 2]
    assert [
        outcome.status_code if isinstance(outcome, FakeResponse) else type(outcome).__name__
        for outcome in transport.response_order
    ] == [500, "FakeNetworkError", 200]
    assert transport.active_requests == 0
    assert transport.peak_active_requests == 1


def test_fake_transport_routes_detail_and_pdf_and_logs_active_peak() -> None:
    detail_url = "https://fixture.invalid/detail/41"
    pdf_url = "https://fixture.invalid/pdf/41"
    transport = FakeTransport(
        detail={detail_url: FakeResponse(200, json_data={"enbek_id": 41})},
        pdf={pdf_url: FakeResponse(200, body=b"%PDF-1.7")},
    )

    detail_handle = transport.start_request(detail_url, expected_kind="detail")
    pdf_handle = transport.start_request(pdf_url, expected_kind="pdf")
    assert transport.active_requests == 2
    assert transport.peak_active_requests == 2

    assert transport.finish_request(pdf_handle).status_code == 200
    assert transport.finish_request(detail_handle).json() == {"enbek_id": 41}
    assert transport.active_requests == 0
    assert [event.type for event in transport.events] == [
        "started",
        "started",
        "finished",
        "finished",
    ]
    assert [event.active_requests for event in transport.events] == [1, 2, 1, 0]
    assert [event.expected_kind for event in transport.events] == [
        "detail",
        "pdf",
        "pdf",
        "detail",
    ]


def test_fake_transport_async_requests_yield_without_real_sleep() -> None:
    url = "https://fixture.invalid/page"
    transport = FakeTransport({url: FakeResponse(200, body=b"ok")})

    async def run() -> None:
        first, second = await asyncio.gather(
            transport.request_async(url, attempt=1),
            transport.request_async(url, attempt=2),
        )
        assert first.content == b"ok"
        assert second.content == b"ok"

    asyncio.run(run())
    assert transport.peak_active_requests == 2
    assert transport.active_requests == 0
