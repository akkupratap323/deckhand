"""Tests for the IPv4 pinning in net.py.

What is worth testing here is the switch and the wiring, not httpx's socket
behaviour: that outbound clients get an IPv4-bound transport by default, that
``FORCE_IPV4=0`` genuinely turns it off, and that a caller's own transport is
never silently replaced.
"""

from __future__ import annotations

import httpx
import pytest

from net import async_client, force_ipv4_enabled, ipv4_transport


@pytest.fixture(autouse=True)
def clear_force_ipv4(monkeypatch):
    """Start every test from an unset FORCE_IPV4 rather than the ambient env."""
    monkeypatch.delenv("FORCE_IPV4", raising=False)


def test_enabled_by_default():
    assert force_ipv4_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "False", "no", "off", "OFF", ""])
def test_disabled_by_falsey_values(monkeypatch, value):
    monkeypatch.setenv("FORCE_IPV4", value)
    assert force_ipv4_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "anything-else"])
def test_enabled_by_other_values(monkeypatch, value):
    monkeypatch.setenv("FORCE_IPV4", value)
    assert force_ipv4_enabled() is True


def test_transport_is_bound_to_an_ipv4_local_address():
    """The whole point: a local IPv4 bind is what keeps NAT64 off the table."""
    transport = ipv4_transport()
    assert isinstance(transport, httpx.AsyncHTTPTransport)
    # The bind address lives on the connection pool underneath the transport.
    assert transport._pool._local_address == "0.0.0.0"


def test_no_transport_when_disabled(monkeypatch):
    monkeypatch.setenv("FORCE_IPV4", "0")
    assert ipv4_transport() is None


@pytest.mark.asyncio
async def test_async_client_gets_the_ipv4_transport():
    async with async_client(timeout=1.0) as client:
        assert client._transport._pool._local_address == "0.0.0.0"


@pytest.mark.asyncio
async def test_async_client_is_plain_when_disabled(monkeypatch):
    monkeypatch.setenv("FORCE_IPV4", "0")
    async with async_client(timeout=1.0) as client:
        assert client._transport._pool._local_address is None


@pytest.mark.asyncio
async def test_caller_supplied_transport_wins():
    """A caller that brings its own transport keeps it, pinning or not."""
    mine = httpx.AsyncHTTPTransport()
    async with async_client(transport=mine, timeout=1.0) as client:
        assert client._transport is mine


@pytest.mark.asyncio
async def test_other_kwargs_pass_through():
    async with async_client(timeout=7.0, base_url="https://example.invalid") as client:
        assert client.timeout.connect == 7.0
        assert str(client.base_url) == "https://example.invalid"
