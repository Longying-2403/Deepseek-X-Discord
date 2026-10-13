"""连外网：调用 API（Tavily、FxTwitter），以及下载网上的图。

下载网上的图时只连公网地址。bot 跑在家里的 NAS 上，要是有人能让它去下载 http://192.168.1.1/… 或者群晖后台，
就等于借它的手翻家里的内网。所以下载时：
- 只认 http / https 和默认端口，网址里不能带用户名密码；
- 网址里直接写 IP 的，必须是公网 IP；奇怪的 IP 写法（127.1、0x7f000001 之类）一律不连；
- 写域名的，只连它解析出来的公网地址（检查过的地址就是实际连的地址，不会检查完再被换成内网地址）；
- 不自动跟着跳转，每跳一次都重新检查；
- 不走代理（走代理的话，解析和连接都在代理那边，上面的检查就管不到了）；
- 限制大小和时间，不存 cookie。

调用 API 的网址是固定的（api.tavily.com、api.fxtwitter.com），照常走 HTTPS_PROXY 之类的代理设置。
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
API_MAX_BYTES = 10_000_000  # API 返回的 JSON 最多这么大

_BLOCKED_HOSTS = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback", "broadcasthost"}
# 只在内网里有意义的域名后缀（.local 是局域网里的 mDNS，群晖自己就叫 xxx.local）
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
    """没下载成 / 没调用成。transient：暂时的（超时、对方服务器出错），过一会儿可以再试。"""

    def __init__(self, message: str, *, transient: bool = False, status: int | None = None):
        super().__init__(message)
        self.transient = transient
        self.status = status


def address_is_public(addr: Any) -> bool:
    """是不是公网地址。内网、本机、链路本地、运营商级 NAT、组播、保留地址都不算；拿不准的也不算。"""
    try:
        ip = addr if isinstance(addr, (ipaddress.IPv4Address, ipaddress.IPv6Address)) else ipaddress.ip_address(str(addr))
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        # IPv6 地址里嵌着 IPv4 的几种写法：按里面那个 IPv4 判断（或者直接不连）
        if ip.ipv4_mapped is not None:
            return address_is_public(ip.ipv4_mapped)
        if int(ip) >> 32 == 0:  # ::a.b.c.d（早就废弃的写法）、::1、::
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
    """像 IP 但不是标准写法：127.1、2130706433、0x7f.0.0.1、017.0.0.1……（不同的库解析结果不一样，一律不连）。

    正常的域名最后一段（顶级域名）不会是纯数字。"""
    labels = host.split(".")
    return bool(_NUMERIC_LABEL.match(labels[-1])) or all(_NUMERIC_LABEL.match(label) for label in labels if label)


def normalize_host(host: str) -> str | None:
    """主机名换成实际连接时的写法。aiohttp（yarl）会按 IDNA 规则把全角数字、全角句号之类换成普通写法，
    “１２７.０.０.１”“192.168.1．1”到它手里就成了内网地址，所以要先换好再检查。换不了的返回 None。"""
    host = (host or "").lower()
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii").lower()
        except (UnicodeError, ValueError):
            return None
    return host.rstrip(".")


def check_host(host: str, *, allowed_hosts: frozenset[str] | set[str] | None = None) -> str | None:
    """主机名（已经是 ASCII 写法）能不能连：能就返回 None，不能就返回原因。"""
    if not host:
        return "没有主机名"
    if allowed_hosts is not None and not any(host == h or host.endswith("." + h) for h in allowed_hosts):
        return "不在允许的网站里"
    literal = _ip_literal(host.strip("[]"))
    if literal is not None:
        return None if address_is_public(literal) else "内网地址"
    if _looks_numeric(host):
        return "不标准的 IP 写法"
    if host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_SUFFIXES) or "." not in host:
        return "内网主机名"
    return None


def check_url(url: str, *, allowed_hosts: frozenset[str] | set[str] | None = None) -> str | None:
    """这个网址能不能去下载：能就返回 None，不能就返回原因（写日志用）。

    allowed_hosts：只许连这些网站（和它们的子域名），比如推文的图只从 pbs.twimg.com 下载。
    域名解析出来是不是公网地址，要等连接时才知道，由 PublicResolver 管。"""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "网址格式不对"
    if parts.scheme not in ("http", "https"):
        return "只支持 http / https"
    if port not in (None, 80, 443):
        return "只连默认端口"
    if parts.username is not None or parts.password is not None:
        return "网址里带了用户名密码"
    host = normalize_host(parts.hostname or "")
    if host is None:
        return "主机名格式不对"
    return check_host(host, allowed_hosts=allowed_hosts)


def _check_as_aiohttp_sees_it(url: str, allowed_hosts: frozenset[str] | set[str] | None) -> str | None:
    """用 aiohttp 自己的网址解析（yarl）再查一遍：实际连的就是这里解析出来的主机和端口，两边的解析有出入也不怕。"""
    try:
        from yarl import URL
    except ImportError:
        return None  # 没装 aiohttp 时也就不会去下载
    try:
        parsed = URL(url)
        host = parsed.raw_host or ""
        scheme, port, user = parsed.scheme, parsed.port, parsed.raw_user  # port：没写时是 80 / 443
    except (ValueError, TypeError, UnicodeError):
        return "网址格式不对"
    if scheme not in ("http", "https"):
        return "只支持 http / https"
    if port not in (None, 80, 443):
        return "只连默认端口"
    if user is not None or parsed.raw_password is not None:
        return "网址里带了用户名密码"
    normalized = normalize_host(host)
    if normalized is None:
        return "主机名格式不对"
    return check_host(normalized, allowed_hosts=allowed_hosts)


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _public_resolver() -> Any:
    """只返回公网地址的 DNS 解析器（aiohttp 连接时用它解析出来的地址，所以检查过的就是实际连的）。"""
    import aiohttp
    from aiohttp.abc import AbstractResolver

    class PublicResolver(AbstractResolver):
        def __init__(self) -> None:
            self._inner = aiohttp.ThreadedResolver()

        async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET) -> list[Any]:
            infos = await self._inner.resolve(host, port, family=family)
            public = [info for info in infos if address_is_public(info["host"])]
            if not public:
                raise OSError(f"{host} 只解析到内网地址，不连")
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
    """两个连接池：api() 调 Tavily、FxTwitter；fetch() 下载网上的图（只连公网）。"""

    def __init__(self, *, api_timeout: float = 25.0, fetch_timeout: float = 12.0):
        self.api_timeout = api_timeout
        self.fetch_timeout = fetch_timeout
        self._api: Any = None
        self._public: Any = None

    @staticmethod
    def _aiohttp() -> Any:
        try:
            import aiohttp  # 延迟导入：核心逻辑测试时不需要（discord.py 自带它）
        except ImportError:
            raise FetchError("没装 aiohttp（pip install -r requirements.txt）") from None
        return aiohttp

    def _api_session(self) -> Any:
        aiohttp = self._aiohttp()
        if self._api is None or self._api.closed:
            self._api = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.api_timeout),
                headers={"User-Agent": API_USER_AGENT},
                trust_env=True,  # 支持 HTTPS_PROXY 环境变量
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
                    "Accept-Encoding": "identity",  # 图片本来就压缩过；不解压，也就不怕“解压炸弹”
                },
                cookie_jar=aiohttp.DummyCookieJar(),
                auto_decompress=False,
                trust_env=False,  # 不走代理：解析和连接都要在这里做，内网检查才管用
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
        """调用一个 JSON API，返回 (HTTP 状态码, 解析好的 JSON；不是 JSON 时是开头一段文字)。"""
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
                        raise FetchError("返回的内容太大", status=resp.status)
                    chunks.append(chunk)
                return resp.status, _decode(b"".join(chunks))
        except asyncio.TimeoutError:
            raise FetchError("超时", transient=True) from None
        except aiohttp.ClientError as exc:
            raise FetchError(f"网络错误：{exc!r}", transient=True) from None

    async def fetch(
        self,
        url: str,
        *,
        max_bytes: int,
        allowed_hosts: frozenset[str] | set[str] | None = None,
    ) -> tuple[bytes, str]:
        """下载网上的一个文件（目前只用来下载图片），返回 (内容, Content-Type)。超过 max_bytes 就放弃。"""
        aiohttp = self._aiohttp()
        session = self._public_session()
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            reason = check_url(current, allowed_hosts=allowed_hosts) or _check_as_aiohttp_sees_it(current, allowed_hosts)
            if reason:
                raise FetchError(f"不连 {host_of(current) or current[:80]}：{reason}")
            try:
                async with session.get(current, allow_redirects=False) as resp:
                    if resp.status in REDIRECTS:
                        location = resp.headers.get("Location")
                        if not location:
                            raise FetchError(f"HTTP {resp.status} 但没给跳转地址")
                        current = urljoin(current, location)
                        continue
                    if resp.status != 200:
                        raise FetchError(f"HTTP {resp.status}", transient=resp.status == 429 or resp.status >= 500, status=resp.status)
                    if resp.content_length is not None and resp.content_length > max_bytes:
                        raise FetchError(f"太大（{resp.content_length} 字节）")
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.content.iter_chunked(65536):
                        total += len(chunk)
                        if total > max_bytes:
                            raise FetchError(f"太大（超过 {max_bytes} 字节）")
                        chunks.append(chunk)
                    return b"".join(chunks), resp.headers.get("Content-Type", "")
            except asyncio.TimeoutError:
                raise FetchError("超时", transient=True) from None
            except FetchError:
                raise
            except (aiohttp.ClientError, OSError, ValueError) as exc:
                raise FetchError(f"连不上：{exc!r}"[:200], transient=True) from None
        raise FetchError("跳转太多次")
