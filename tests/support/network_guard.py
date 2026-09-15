"""Fail-fast guard against accidental outbound network access in tests.

The fixture harness uses :class:`tests.support.fakes.FakeTransport`, which is
entirely in-process and does not need an exception in this guard.  While the
guard is active every real socket, DNS, urllib and ``http.client`` entry point
raises :class:`OutboundNetworkError`.  This makes an accidental live request a
visible test failure instead of a slow or environment-dependent result.
"""

from __future__ import annotations

import http.client
import socket
import urllib.request
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from typing import Any, Iterator
from unittest.mock import patch


class OutboundNetworkError(AssertionError):
    """Raised when code under test attempts to use the real network."""


# Public aliases make the guard convenient in tests and keep error assertions
# independent from implementation details of the patched stdlib function.
NetworkAccessError = OutboundNetworkError
NoNetworkError = OutboundNetworkError


def _blocked(operation: str):
    def deny(*args: Any, **kwargs: Any) -> Any:
        target = operation
        if args and isinstance(args[0], str):
            target = f"{operation}({args[0]!r})"
        raise OutboundNetworkError(
            f"outbound network access is disabled in fixture tests: {target}"
        )

    deny.__name__ = f"blocked_{operation.replace('.', '_')}"
    return deny


def _blocked_socket(*args: Any, **kwargs: Any) -> Any:
    raise OutboundNetworkError(
        "outbound network access is disabled in fixture tests: socket.socket"
    )


@dataclass(frozen=True, slots=True)
class NetworkGuardState:
    """Small diagnostic object returned by :func:`no_network` callers."""

    active: bool = True
    allowed_transport_count: int = 0


class NoNetworkGuard:
    """Context manager that blocks real DNS, sockets and HTTP entry points.

    ``FakeTransport`` is deliberately not monkey-patched: it records and
    returns scripted outcomes without touching any of the blocked functions.
    ``allowed_transports`` is retained as an explicit audit declaration and is
    validated by :meth:`assert_transport`; it does not weaken the real-network
    block.
    """

    def __init__(self, *, allowed_transports: Any = ()) -> None:
        if allowed_transports is None:
            allowed_transports = ()
        if isinstance(allowed_transports, (str, bytes)):
            allowed_transports = (allowed_transports,)
        try:
            self.allowed_transports = tuple(allowed_transports)
        except TypeError as exc:
            raise TypeError("allowed_transports must be iterable") from exc
        self._stack: ExitStack | None = None
        self.state = NetworkGuardState(
            active=False,
            allowed_transport_count=len(self.allowed_transports),
        )

    def assert_transport(self, transport: Any) -> None:
        """Require a fixture transport declared for this guarded run.

        This catches accidentally constructing a second, untracked transport
        in a harness test while still allowing callers to use a single fake
        transport without importing its concrete class here.
        """

        if not self.allowed_transports:
            return
        if not any(transport is candidate for candidate in self.allowed_transports):
            raise AssertionError(
                "transport is not declared in NoNetworkGuard.allowed_transports"
            )

    def __enter__(self) -> "NoNetworkGuard":
        if self._stack is not None:
            raise RuntimeError("NoNetworkGuard cannot be entered twice")
        stack = ExitStack()
        # DNS and high-level stdlib entry points.  Keep these patches explicit
        # rather than relying only on ``socket.socket``: some resolvers can
        # perform work before creating a socket and a test should fail at the
        # attempted network API with a useful diagnostic.
        for name in (
            "getaddrinfo",
            "gethostbyname",
            "gethostbyname_ex",
            "gethostbyaddr",
            "getnameinfo",
            "getfqdn",
        ):
            if hasattr(socket, name):
                stack.enter_context(
                    patch.object(socket, name, _blocked(f"socket.{name}"))
                )
        stack.enter_context(
            patch.object(socket, "create_connection", _blocked("socket.create_connection"))
        )
        # New sockets and already-imported HTTP clients are both covered.
        # Patching request/getresponse as well as connect also catches callers
        # that provide a custom connection implementation in a test.
        stack.enter_context(patch.object(socket, "socket", _blocked_socket))
        for connection in (http.client.HTTPConnection, http.client.HTTPSConnection):
            for name in ("connect", "request", "getresponse"):
                stack.enter_context(
                    patch.object(
                        connection,
                        name,
                        _blocked(f"http.client.{connection.__name__}.{name}"),
                    )
                )
        # Patch both the public helper and opener entry point.  The latter is
        # important for ``from urllib.request import urlopen`` aliases that
        # were imported before this guard was entered.
        stack.enter_context(
            patch.object(urllib.request, "urlopen", _blocked("urllib.request.urlopen"))
        )
        stack.enter_context(
            patch.object(
                urllib.request.OpenerDirector,
                "open",
                _blocked("urllib.request.OpenerDirector.open"),
            )
        )
        stack.enter_context(
            patch.object(urllib.request, "build_opener", _blocked("urllib.request.build_opener"))
        )
        self._stack = stack
        self.state = NetworkGuardState(
            active=True,
            allowed_transport_count=len(self.allowed_transports),
        )
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        if self._stack is not None:
            self._stack.close()
            self._stack = None
        self.state = NetworkGuardState(
            active=False,
            allowed_transport_count=len(self.allowed_transports),
        )
        return False


@contextmanager
def no_network(
    *,
    allowed_transports: Any = (),
) -> Iterator[NoNetworkGuard]:
    """Block all real network access for the duration of a fixture test."""

    with NoNetworkGuard(allowed_transports=allowed_transports) as guard:
        yield guard


# Naming variants keep the helper readable at call sites and support the
# terminology used by the requirements/design documents.
no_network_guard = no_network
network_guard = no_network


__all__ = [
    "NetworkAccessError",
    "NetworkGuardState",
    "NoNetworkError",
    "NoNetworkGuard",
    "OutboundNetworkError",
    "network_guard",
    "no_network",
    "no_network_guard",
]
