"""Outbound HTTP transport, and the IPv6 problem it works around.

Every upstream API this app talks to (DeepSeek, Gemini, Bedrock) is reached
over plain HTTPS via ``httpx``. On an ordinary dual-stack network none of this
module would need to exist. It exists because of a specific, measured failure.

The symptom: sessions connected fine - WebRTC negotiated, Deepgram Flux and
Cartesia both up, all six tools registered - and then the opening greeting's
LLM call died with "Request timed out" after 16.6s, leaving the agent
connected but silent. Mid-session questions hit the same wall, producing dead
air. Deck ingestion failed the same way with ``httpx.ConnectTimeout``.

The cause, traced to the socket: on this network (Reliance Jio, whose Wi-Fi
adapter advertises an IPv6 DNS server) ``api.deepseek.com`` resolves to *two*
addresses, IPv6 first::

    IPv6  64:ff9b::3ad:153f
    IPv4  3.173.21.63

``64:ff9b::/96`` is the well-known NAT64 prefix from RFC 6052, and
``3ad:153f`` is just ``3.173.21.63`` in hex - a synthetic address minted by
the ISP's DNS64 resolver for an IPv4-only host. Synthesising it works;
actually routing it does not. Connecting to the IPv6 address hangs until the
socket times out, while the IPv4 address connects in 0.02s. Both measured
directly, not inferred.

``getaddrinfo`` returns the IPv6 address first, so that is what gets tried
first. The OpenAI SDK's defaults are ``connect=5.0`` with ``max_retries=2`` -
three attempts, each burning 5s on an address that will never answer, which is
exactly the 16.6s in the logs. httpx does not implement Happy Eyeballs
(RFC 8305), so there is no fast fallback to IPv4 to rescue it.

Binding the local socket to ``0.0.0.0`` constrains connections to IPv4, so the
dead NAT64 address is never attempted at all. That is the whole fix.

Defaulting this on is deliberate. The hosts here are IPv4-reachable, so
forcing IPv4 is safe on any network with working IPv4 - which is the common
case, including the broken-NAT64 one. The exception is a genuinely IPv6-only
network, where NAT64 is the *only* route to an IPv4-only host like DeepSeek
and forcing IPv4 would break what currently works; set ``FORCE_IPV4=0`` there.
"""

from __future__ import annotations

import os

import httpx
from loguru import logger


def force_ipv4_enabled() -> bool:
    """Whether outbound HTTP should be pinned to IPv4.

    On unless ``FORCE_IPV4`` is set to a falsey value. See the module docstring
    for why the default is what it is.
    """
    raw = os.environ.get("FORCE_IPV4", "1").strip().lower()
    return raw not in ("0", "false", "no", "off", "")


def ipv4_transport() -> httpx.AsyncHTTPTransport | None:
    """An httpx transport pinned to IPv4, or ``None`` to use httpx's default.

    ``local_address="0.0.0.0"`` binds the local end of the socket to an IPv4
    address, which constrains the connection to IPv4 and skips the unroutable
    NAT64 address entirely.
    """
    if not force_ipv4_enabled():
        return None
    return httpx.AsyncHTTPTransport(local_address="0.0.0.0")


def async_client(**kwargs) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` that avoids the NAT64 trap.

    A drop-in replacement for ``httpx.AsyncClient(...)``; all keyword
    arguments are passed straight through. Callers that pass their own
    ``transport`` are left alone.
    """
    if "transport" not in kwargs:
        transport = ipv4_transport()
        if transport is not None:
            kwargs["transport"] = transport
    return httpx.AsyncClient(**kwargs)


def log_transport_choice() -> None:
    """Say once, at startup, which way outbound HTTP is configured."""
    if force_ipv4_enabled():
        logger.info("Outbound HTTP pinned to IPv4 (FORCE_IPV4); see net.py for why")
    else:
        logger.info("Outbound HTTP using default dual-stack resolution (FORCE_IPV4=0)")
