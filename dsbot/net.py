"""Reaching the outside internet: calling APIs (Tavily, FxTwitter) and downloading pictures from the web.

When downloading pictures from the web, only public addresses are connected to. The bot runs on a NAS at home, and if someone could get it to
download http://192.168.1.1/… or the Synology admin page, that would amount to using it to poke around the home network. So when downloading:
- only http / https on the default ports, and no username or password in the URL;
- a URL with an IP written directly in it must be a public IP; odd ways of writing IPs (127.1, 0x7f000001 and the like) are never connected to;
- for a domain name, only the public addresses it resolves to are connected to (the address checked is the address actually connected to,
  so it can't be swapped for a private address after the check);
- redirects aren't followed automatically; every hop is checked again;
- no proxy (with a proxy, resolving and connecting happen on the proxy's side, and the checks above would have no say);
- size and time are limited, and no cookies are kept.

The API URLs are fixed (api.tavily.com, api.fxtwitter.com) and go through proxy settings like HTTPS_PROXY as usual.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import socket
from typing import Any
from urllib.parse import urljoin, urlsplit

from . import __version__

log = logging.getLogger(__name__)

PROJECT_URL = "https://github.com/Longying-2403/Deepseek-X-Discord"
API_USER_AGENT = f"Deepseek-X-Discord/{__version__} (+{PROJECT_URL})"
FETCH_USER_AGENT = f"Mozilla/5.0 (compatible; Deepseek-X-Discord/{__version__}; +{PROJECT_URL})"
REDIRECTS = (301, 302, 303, 307, 308)
MAX_REDIRECTS = 3
API_MAX_BYTES = 10_000_000  # the largest JSON an API response may return

_BLOCKED_HOSTS = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback", "broadcasthost"}
# domain suffixes that only mean something inside a private network (.local is mDNS on the LAN; a Synology NAS itself is called xxx.local)
_BLOCKED_SUFFIXES = (
    ".local",
    ".localhost",
    ".localdomain",
    ".internal",
    ".intranet",
    ".lan",
    ".home",
    ".home.arpa",
    ".corp",
    ".private",
    ".test",
    ".invalid",
    ".example",
    ".arpa",
)
_NUMERIC_LABEL = re.compile(r"^(0x[0-9a-f]*|[0-9]+)$")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")


class FetchError(Exception):
    """A download or API call that didn't succeed. transient: temporary (a timeout, or an error on the other server), so it can be retried in a while."""

    def __init__(self, message: str, *, transient: bool = False, status: int | None = None):
        super().__init__(message)
        self.transient = transient
        self.status = status


def address_is_public(addr: Any) -> bool:
    """Whether it's a public address. Private, loopback, link-local, carrier-grade NAT, multicast and reserved addresses don't count; neither does anything uncertain."""
    try:
        ip = addr if isinstance(addr, (ipaddress.IPv4Address, ipaddress.IPv6Address)) else ipaddress.ip_address(str(addr))
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        # the ways an IPv4 address can be embedded in an IPv6 one: judge by the IPv4 inside (or just don't connect)
        if ip.ipv4_mapped is not None:
            return address_is_public(ip.ipv4_mapped)
        if int(ip) >> 32 == 0:  # ::a.b.c.d (long-deprecated notation), ::1, ::
            return False
        if ip in _NAT64:
            return address_is_public(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        if ip in _NAT64_LOCAL or ip.teredo is not None:
            return False
        if ip.sixtofour is not None:
            return address_is_public(ip.sixtofour)
    return bool(
        ip.is_global
        and not ip.is_private
        and not ip.is_loopback
        and not ip.is_link_local
        and not ip.is_multicast
        and not ip.is_reserved
        and not ip.is_unspecified
    )


def _ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _looks_numeric(host: str) -> bool:
    """Looks like an IP but isn't written the standard way: 127.1, 2130706433, 0x7f.0.0.1, 017.0.0.1… (different libraries parse these differently, so none of them are connected to).

    The last label of a normal domain name (the top-level domain) is never all digits."""
    labels = host.split(".")
    return bool(_NUMERIC_LABEL.match(labels[-1])) or all(_NUMERIC_LABEL.match(label) for label in labels if label)


def normalize_host(host: str) -> str | None:
    """Turn the host name into the form actually used when connecting. aiohttp (yarl) applies IDNA rules that turn full-width digits,
    full-width dots and the like into ordinary ones, so "１２７.０.０.１" or "192.168.1．1" become private addresses in its hands; that's why
    the conversion happens before the check. None if it can't be converted."""
    host = (host or "").lower()
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii").lower()
        except (UnicodeError, ValueError):
            return None
    return host.rstrip(".")


def check_host(host: str, *, allowed_hosts: frozenset[str] | set[str] | None = None) -> str | None:
    """Whether a host name (already in ASCII form) may be connected to: None if it may, otherwise the reason."""
    if not host:
        return "no host name"
    if allowed_hosts is not None and not any(host == h or host.endswith("." + h) for h in allowed_hosts):
        return "not one of the allowed sites"
    literal = _ip_literal(host.strip("[]"))
    if literal is not None:
        return None if address_is_public(literal) else "private address"
    if _looks_numeric(host):
        return "non-standard IP notation"
    if host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_SUFFIXES) or "." not in host:
        return "private host name"
    return None


def check_url(url: str, *, allowed_hosts: frozenset[str] | set[str] | None = None) -> str | None:
    """Whether this URL may be downloaded: None if so, otherwise the reason (for the logs).

    allowed_hosts: only these sites (and their subdomains) may be connected to; e.g. images in posts on X are only downloaded from pbs.twimg.com.
    Whether a domain resolves to public addresses is only known at connection time; PublicResolver takes care of that."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "malformed URL"
    if parts.scheme not in ("http", "https"):
        return "only http / https are supported"
    if port not in (None, 80, 443):
        return "only the default ports are allowed"
    if parts.username is not None or parts.password is not None:
        return "the URL contains a username or password"
    host = normalize_host(parts.hostname or "")
    if host is None:
        return "malformed host name"
    return check_host(host, allowed_hosts=allowed_hosts)


def _check_as_aiohttp_sees_it(url: str, allowed_hosts: frozenset[str] | set[str] | None) -> str | None:
    """Check again with aiohttp's own URL parsing (yarl): the host and port parsed here are the ones actually connected to, so a mismatch between the two parsers doesn't matter."""
    try:
        from yarl import URL
    except ImportError:
        return None  # without aiohttp there's no downloading anyway
    try:
        parsed = URL(url)
        host = parsed.raw_host or ""
        scheme, port, user = parsed.scheme, parsed.port, parsed.raw_user  # port: 80 / 443 when not given
    except (ValueError, TypeError, UnicodeError):
        return "malformed URL"
    if scheme not in ("http", "https"):
        return "only http / https are supported"
    if port not in (None, 80, 443):
        return "only the default ports are allowed"
    if user is not None or parsed.raw_password is not None:
        return "the URL contains a username or password"
    normalized = normalize_host(host)
    if normalized is None:
        return "malformed host name"
    return check_host(normalized, allowed_hosts=allowed_hosts)


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _public_resolver() -> Any:
    """A DNS resolver that only returns public addresses (aiohttp connects to the addresses it resolves, so what was checked is what gets connected to)."""
    import aiohttp
    from aiohttp.abc import AbstractResolver

    class PublicResolver(AbstractResolver):
        def __init__(self) -> None:
            self._inner = aiohttp.ThreadedResolver()

        async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET) -> list[Any]:
            infos = await self._inner.resolve(host, port, family=family)
            public = [info for info in infos if address_is_public(info["host"])]
            if not public:
                raise OSError(f"{host} only resolves to private addresses; not connecting")
            return public

        async def close(self) -> None:
            await self._inner.close()

    return PublicResolver()


def _decode(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return raw.decode("utf-8", errors="replace")[:500]


class HttpClient:
    """Two connection pools: api() calls Tavily and FxTwitter; fetch() downloads pictures from the web (public addresses only)."""

    def __init__(self, *, api_timeout: float = 25.0, fetch_timeout: float = 12.0):
        self.api_timeout = api_timeout
        self.fetch_timeout = fetch_timeout
        self._api: Any = None
        self._public: Any = None

    @staticmethod
    def _aiohttp() -> Any:
        try:
            import aiohttp  # imported lazily: the core-logic tests don't need it (discord.py brings it along)
        except ImportError:
            raise FetchError("aiohttp isn't installed (pip install -r requirements.txt)") from None
        return aiohttp

    def _api_session(self) -> Any:
        aiohttp = self._aiohttp()
        if self._api is None or self._api.closed:
            self._api = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.api_timeout),
                headers={"User-Agent": API_USER_AGENT},
                trust_env=True,  # honors the HTTPS_PROXY environment variable
            )
        return self._api

    def _public_session(self) -> Any:
        aiohttp = self._aiohttp()
        if self._public is None or self._public.closed:
            self._public = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(resolver=_public_resolver(), limit=8, ttl_dns_cache=60),
                timeout=aiohttp.ClientTimeout(total=self.fetch_timeout),
                headers={
                    "User-Agent": FETCH_USER_AGENT,
                    "Accept": "image/webp,image/png,image/jpeg,image/gif;q=0.9,image/*;q=0.8",
                    "Accept-Encoding": "identity",  # images are compressed already; not decompressing also rules out "decompression bombs"
                },
                cookie_jar=aiohttp.DummyCookieJar(),
                auto_decompress=False,
                trust_env=False,  # no proxy: resolving and connecting have to happen here for the private-network checks to work
            )
        return self._public

    async def close(self) -> None:
        for session in (self._api, self._public):
            if session is not None:
                await session.close()
        self._api = self._public = None

    async def api(
        self,
        method: str,
        url: str,
        *,
        payload: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> tuple[int, Any]:
        """Call a JSON API and return (HTTP status code, the parsed JSON; or the beginning of the text when it isn't JSON)."""
        aiohttp = self._aiohttp()
        session = self._api_session()
        kwargs: dict[str, Any] = {"headers": headers or {}}
        if payload is not None:
            kwargs["json"] = payload
        if timeout is not None:
            kwargs["timeout"] = aiohttp.ClientTimeout(total=timeout)
        try:
            async with session.request(method, url, **kwargs) as resp:
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.content.iter_chunked(65536):
                    total += len(chunk)
                    if total > API_MAX_BYTES:
                        raise FetchError("the response is too large", status=resp.status)
                    chunks.append(chunk)
                return resp.status, _decode(b"".join(chunks))
        except asyncio.TimeoutError:
            raise FetchError("timed out", transient=True) from None
        except aiohttp.ClientError as exc:
            raise FetchError(f"network error: {exc!r}", transient=True) from None

    async def fetch(
        self,
        url: str,
        *,
        max_bytes: int,
        allowed_hosts: frozenset[str] | set[str] | None = None,
    ) -> tuple[bytes, str]:
        """Download one file from the web (currently only used for pictures) and return (content, Content-Type). Gives up past max_bytes."""
        aiohttp = self._aiohttp()
        session = self._public_session()
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            reason = check_url(current, allowed_hosts=allowed_hosts) or _check_as_aiohttp_sees_it(current, allowed_hosts)
            if reason:
                raise FetchError(f"not connecting to {host_of(current) or current[:80]}: {reason}")
            try:
                async with session.get(current, allow_redirects=False) as resp:
                    if resp.status in REDIRECTS:
                        location = resp.headers.get("Location")
                        if not location:
                            raise FetchError(f"HTTP {resp.status} but no redirect location given")
                        current = urljoin(current, location)
                        continue
                    if resp.status != 200:
                        raise FetchError(f"HTTP {resp.status}", transient=resp.status == 429 or resp.status >= 500, status=resp.status)
                    if resp.content_length is not None and resp.content_length > max_bytes:
                        raise FetchError(f"too large ({resp.content_length} bytes)")
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.content.iter_chunked(65536):
                        total += len(chunk)
                        if total > max_bytes:
                            raise FetchError(f"too large (over {max_bytes} bytes)")
                        chunks.append(chunk)
                    return b"".join(chunks), resp.headers.get("Content-Type", "")
            except asyncio.TimeoutError:
                raise FetchError("timed out", transient=True) from None
            except FetchError:
                raise
            except (aiohttp.ClientError, OSError, ValueError) as exc:
                raise FetchError(f"can't connect: {exc!r}"[:200], transient=True) from None
        raise FetchError("too many redirects")
