"""Is anything between the VPS and Polymarket/OddsPapi blocking us? (user decision 1, M1)

For each host: system DNS vs DNS-over-HTTPS, TCP connect, TLS handshake with certificate
validation, an HTTP request (or a WebSocket handshake), and heuristics for block pages.
Facts are printed as a Markdown table for docs/latency.md; verdicts are hints, not proof.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import ssl
import time
from dataclasses import asdict, dataclass, field
from urllib.parse import urlsplit

import httpx
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import WebSocketException

from polybot.analytics.stats import md_table
from polybot.venues.polymarket.clob_rest import cf_colo

TIMEOUT_S = 10.0

# (label, url). WebSocket URLs are checked with a handshake instead of GET.
DEFAULT_TARGETS: tuple[tuple[str, str], ...] = (
    ("geoblock", "https://polymarket.com/api/geoblock"),
    ("clob", "https://clob.polymarket.com/time"),
    ("gamma", "https://gamma-api.polymarket.com/sports"),
    ("data-api", "https://data-api.polymarket.com/"),
    ("docs", "https://docs.polymarket.com/"),
    ("help", "https://help.polymarket.com/"),
    ("market-ws", "wss://ws-subscriptions-clob.polymarket.com/ws/market"),
    ("sports-ws", "wss://sports-api.polymarket.com/ws"),
    ("oddspapi", "https://api.oddspapi.io/v4/sports"),
)

DOH_RESOLVERS = (
    "https://cloudflare-dns.com/dns-query",
    "https://dns.google/resolve",
)


@dataclass
class HostCheck:
    label: str
    url: str
    host: str
    system_ips: list[str] = field(default_factory=list)
    doh_ips: list[str] = field(default_factory=list)
    dns_error: str | None = None
    tcp_ms: float | None = None
    tls_ms: float | None = None
    tls_error: str | None = None
    status: int | None = None
    server: str | None = None
    colo: str | None = None
    http_ms: float | None = None
    http_error: str | None = None
    notes: list[str] = field(default_factory=list)
    verdict: str = "unknown"


def _is_bogon(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return addr.is_private or addr.is_loopback or addr.is_reserved or addr.is_unspecified


async def _system_dns(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


async def _doh(client: httpx.AsyncClient, host: str) -> list[str]:
    ips: set[str] = set()
    for resolver in DOH_RESOLVERS:
        for rtype in ("A", "AAAA"):
            try:
                response = await client.get(
                    resolver,
                    params={"name": host, "type": rtype},
                    headers={"accept": "application/dns-json"},
                )
                answers = response.json().get("Answer") or []
            except (httpx.HTTPError, json.JSONDecodeError, AttributeError):
                continue
            ips.update(a["data"] for a in answers if a.get("type") in (1, 28) and "data" in a)
    return sorted(ips)


async def _tcp_tls(check: HostCheck, ip: str) -> None:
    started = time.perf_counter()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(ip, 443), TIMEOUT_S)
    except (OSError, TimeoutError) as exc:
        check.notes.append(f"tcp {type(exc).__name__}")
        return
    check.tcp_ms = (time.perf_counter() - started) * 1000
    writer.close()
    ctx = ssl.create_default_context()
    started = time.perf_counter()
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, 443, ssl=ctx, server_hostname=check.host), TIMEOUT_S
        )
    except (OSError, TimeoutError, ssl.SSLError) as exc:
        check.tls_error = f"{type(exc).__name__}: {exc}"[:160]
        return
    check.tls_ms = (time.perf_counter() - started) * 1000 - (check.tcp_ms or 0.0)
    writer.close()


async def _http(check: HostCheck, client: httpx.AsyncClient) -> None:
    started = time.perf_counter()
    try:
        response = await client.get(check.url)
    except httpx.HTTPError as exc:
        check.http_error = f"{type(exc).__name__}: {exc}"[:160]
        return
    check.http_ms = (time.perf_counter() - started) * 1000
    check.status = response.status_code
    check.server = response.headers.get("server")
    check.colo = cf_colo(response.headers)
    content_type = response.headers.get("content-type", "")
    if response.status_code == 451:
        check.notes.append("HTTP 451: blocked for legal reasons")
    if response.is_redirect:
        location = response.headers.get("location", "")
        if urlsplit(location).hostname not in (None, check.host):
            check.notes.append(f"redirect to {urlsplit(location).hostname}")
    if "text/html" in content_type and check.label in ("clob", "gamma", "oddspapi", "geoblock"):
        if "cloudflare" in (check.server or "").lower():
            check.notes.append("HTML from Cloudflare (WAF/challenge?)")
        else:
            check.notes.append("HTML from non-Cloudflare server: possible ISP block page")


async def _ws(check: HostCheck) -> None:
    started = time.perf_counter()
    try:
        async with ws_connect(check.url, open_timeout=TIMEOUT_S, close_timeout=2):
            check.http_ms = (time.perf_counter() - started) * 1000
            check.status = 101
    except (OSError, TimeoutError, WebSocketException) as exc:
        check.http_error = f"{type(exc).__name__}: {exc}"[:160]


def _verdict(check: HostCheck) -> str:
    if check.dns_error and check.doh_ips:
        return "DNS blocked (system resolver fails, DoH resolves)"
    if check.dns_error:
        return "DNS failure"
    if any(_is_bogon(ip) for ip in check.system_ips):
        return "DNS tampering (private/bogon address)"
    if check.tcp_ms is None:
        return "TCP blocked or unreachable"
    if check.tls_error:
        return "TLS failure (DPI/interception?)"
    if check.http_error:
        return "HTTP/WS failure"
    if any("block" in note.lower() for note in check.notes):
        return "possible block page"
    return "ok"


async def check_host(label: str, url: str, client: httpx.AsyncClient) -> HostCheck:
    host = urlsplit(url).hostname or ""
    check = HostCheck(label=label, url=url, host=host)
    try:
        check.system_ips = await asyncio.wait_for(_system_dns(host), TIMEOUT_S)
    except (OSError, TimeoutError) as exc:
        check.dns_error = type(exc).__name__
    check.doh_ips = await _doh(client, host)
    if check.system_ips:
        await _tcp_tls(check, check.system_ips[0])
        if url.startswith("wss://"):
            await _ws(check)
        else:
            await _http(check, client)
    if check.system_ips and check.doh_ips and not set(check.system_ips) & set(check.doh_ips):
        check.notes.append("system DNS and DoH disagree (CDN geo-DNS or tampering)")
    check.verdict = _verdict(check)
    return check


async def run_netcheck(targets: tuple[tuple[str, str], ...] = DEFAULT_TARGETS) -> list[HostCheck]:
    async with httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=False) as client:
        return [await check_host(label, url, client) for label, url in targets]


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f}"


def render(checks: list[HostCheck]) -> str:
    rows = [
        [
            c.label,
            c.host,
            ", ".join(c.system_ips[:2]) or "—",
            _fmt(c.tcp_ms),
            _fmt(c.tls_ms),
            c.status if c.status is not None else "—",
            c.colo or "—",
            c.verdict,
            "; ".join(c.notes) or "",
        ]
        for c in checks
    ]
    header = [
        "цель",
        "хост",
        "IP (системный DNS)",
        "TCP, мс",
        "TLS, мс",
        "HTTP",
        "CF colo",
        "вердикт",
        "заметки",
    ]
    return md_table(header, rows)


def to_json(checks: list[HostCheck]) -> str:
    return json.dumps([asdict(c) for c in checks], ensure_ascii=False, indent=1)
