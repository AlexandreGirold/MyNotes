"""Safe public web search and read helpers.

search_web(query) → dict{query, results:[{title,url,snippet}], warnings:list}
read_web(url)      → dict{url, title, text, truncated:bool}

DuckDuckgo HTML GET for search.  Stdlib http.client + SSL for read.
All transport errors → ValueError; blocked/challenge → warnings, not results.
"""

from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any

from bs4 import BeautifulSoup

# ── DNS pinning (no global monkey-patch) ────────────────────────────────────

_INVALID_FAMILIES = {socket.AF_UNIX}


def get_global_dns(hostname: str) -> list[str]:
    """Resolve *hostname* and return only global (public) IPs.

    Raises ``ValueError`` if any answer is not a global unicast address.
    """
    try:
        infos = socket.getaddrinfo(hostname, 443, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except Exception as exc:
        raise ValueError(f"DNS lookup failed for {hostname}: {exc}") from exc

    addrs: list[str] = []
    for _family, _socktype, _proto, _canonname, sockaddr in infos:
        if _family in _INVALID_FAMILIES:
            continue
        ip = str(sockaddr[0])
        # reject private, loopback, link-local, reserved
        if not _is_global_ip(ip):
            raise ValueError(f"DNS for {hostname} includes a non-global address")
        addrs.append(ip)

    if not addrs:
        raise ValueError(
            f"All addresses for {hostname} are non-global "
            f"(private/loopback/link-local)"
        )
    return addrs


def _is_global_ip(ip: str) -> bool:
    """Return ``True`` when *ip* is **not** private / loopback / link-local."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_global and not addr.is_multicast


# ── Transport ───────────────────────────────────────────────────────────────

class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection pre-resolved to a *single* global IP (DNS-rebind safe)."""

    def __init__(self, host: str, ip: str, port: int = 443,
                 timeout: float = 10, **kwargs: Any) -> None:
        super().__init__(host, port=port, timeout=timeout, **kwargs)
        self._pinned_ip = ip

    def connect(self) -> None:
        sock = socket.create_connection(
            (self._pinned_ip, self.port),
            timeout=self.timeout,
        )
        context = ssl.create_default_context()
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        try:
            self.sock = context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


def make_request(url: str, *, content_types: tuple[str, ...] = ("text/html", "text/plain")) -> str:
    """GET *url* via pinned transport.  Max 3 redirects, 2 MB body cap."""

    for _hop in range(4):  # 1 initial + 3 redirects
        url = validate_url(url, resolve=False)
        parsed = urllib.parse.urlparse(url)
        hostname = parsed.hostname or ""
        dns_ips = get_global_dns(hostname)  # resolves fresh every hop
        ip = dns_ips[0]  # pick first global address

        conn = _PinnedHTTPSConnection(hostname, ip, timeout=10)
        try:
            target = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
            conn.request("GET", target, headers={"User-Agent": "MyNotes/0.1 (public web reader)", "Accept-Encoding": "identity"})
            resp = conn.getresponse()
            status = resp.status
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            if status in (301, 302, 303, 307, 308):
                location = resp_headers.get("location")
                if not location or _hop == 3:
                    raise ValueError("Missing redirect location or too many redirects")
                url = urllib.parse.urljoin(url, location)
                continue
            if not 200 <= status < 300:
                raise ValueError(f"Web server returned HTTP {status}")
            ct = resp_headers.get("content-type", "").lower()
            if not any(ct.startswith(t) for t in content_types):
                raise ValueError(f"Unsupported content-type: {ct}")
            body_bytes = resp.read(2_000_001)
            if len(body_bytes) > 2_000_000:
                raise ValueError("Web page exceeds the 2 MB limit")
            return body_bytes.decode("utf-8", errors="replace")
        except Exception as exc:
            raise ValueError(f"Request failed for {url}: {exc}") from exc
        finally:
            conn.close()
    raise ValueError("Too many redirects")


# ── URL validation ──────────────────────────────────────────────────────────

def validate_url(url: str, *, resolve: bool = True) -> str:
    """Validate & canonicalise a URL for safe fetching.

    *HTTPS-only on port 443 (explicit)*
    *No credentials, no fragments*
    *Resolved IPs must be global*
    """
    if not isinstance(url, str) or len(url) > 4096 or any(ord(c) < 33 or ord(c) == 127 for c in url):
        raise ValueError("Invalid URL")
    parsed = urllib.parse.urlparse(url)

    # scheme
    if parsed.scheme.lower() != "https":
        raise ValueError(f"Only HTTPS allowed, got {parsed.scheme}")

    # fragment
    if parsed.fragment:
        raise ValueError("URL fragments not allowed")

    # credentials
    if parsed.username or parsed.password:
        raise ValueError("URL credentials not allowed")

    # explicit port
    if parsed.port is not None and parsed.port != 443:
        raise ValueError(f"Port must be 443, got {parsed.port}")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL must have a hostname")

    # DNS → all global
    if resolve:
        get_global_dns(hostname)

    return url


# ── Public helpers ──────────────────────────────────────────────────────────

def search_web(query: str) -> dict:
    """Search via DuckDuckgo HTML.

    Returns ``{query, results:[{title, url, snippet}], warnings:[]}``.
    Maximum 5 results.  Blocked / challenge pages produce warnings, not results.
    """
    if not query or not query.strip():
        return {"query": query, "results": [], "warnings": ["Empty query"]}

    if len(query) > 500:
        return {"query": query, "results": [], "warnings": ["Query exceeds 500 characters"]}

    query = query.strip()
    params = urllib.parse.urlencode({"q": query})
    target = f"https://html.duckduckgo.com/html/?{params}"

    try:
        html = make_request(target)
    except ValueError as exc:
        return _search_bing(query, [str(exc)])

    soup = BeautifulSoup(html, "html.parser")
    links = soup.select(".result__a")
    results: list[dict] = []
    for link in links[:10]:  # grab extras, then trim to 5
        href = link.get("href", "")
        if not isinstance(href, str):
            continue

        # unwrap DuckDuckGo urdg redirect
        if "/l/" in href:
            parsed_uddg = urllib.parse.urlparse(href)
            params_uddg = urllib.parse.parse_qs(parsed_uddg.query)
            uddg = params_uddg.get("uddg", [])[0] if params_uddg.get("uddg") else href
            uddg = uddg.strip()
        else:
            uddg = href

        if not urllib.parse.urlparse(uddg).scheme:
            continue
        if uddg.endswith("/"):
            uddg = uddg.rstrip("/")

        # validate
        try:
            validate_url(uddg, resolve=False)
        except ValueError:
            continue
        parent = link.find_parent(class_="result")
        snippet = parent.select_one(".result__snippet") if parent else None
        results.append({
            "title": link.get_text().strip(),
            "url": uddg,
            "snippet": snippet.get_text(" ", strip=True)[:1000] if snippet else "",
        })
        if len(results) >= 5:
            break

    warnings: list[str] = []
    if not results and any(marker in html.lower() for marker in ("captcha", "challenge", "anomaly")):
        warnings.append("DuckDuckGo returned a challenge page")
    if not results and not soup.select(".result__a"):
        warnings.append("No search results found")

    if not results:
        return _search_bing(query, warnings)
    return {"query": query, "results": results, "warnings": warnings}


def _search_bing(query: str, warnings: list[str]) -> dict:
    """Use public RSS search when the HTML search endpoint is blocked."""
    results = []
    try:
        url = "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query, "format": "rss"})
        feed = ET.fromstring(make_request(url, content_types=("application/rss+xml", "application/xml", "text/xml")))
        for item in feed.findall("./channel/item"):
            link = item.findtext("link", "")
            try:
                validate_url(link, resolve=False)
            except ValueError:
                continue
            results.append({"title": item.findtext("title", "")[:300], "url": link, "snippet": item.findtext("description", "")[:1000]})
            if len(results) == 5:
                break
    except (ValueError, ET.ParseError) as exc:
        warnings = [*warnings, f"Fallback search unavailable: {exc}"]
    return {"query": query, "results": results, "warnings": warnings, "provider": "Bing RSS"}


def read_web(url: str) -> dict:
    """Read & summarise a public web page.

    Returns ``{url, title, text, truncated}``.
    Max 16 000 chars of clean text, including headings.
    """
    html = make_request(url)

    soup = BeautifulSoup(html, "html.parser")

    title = ""
    t = soup.find("title")
    if t and isinstance(t.string, str):
        title = t.string.strip()

    # strip unwanted elements
    for tag_name in ("script", "style", "form", "head"):
        for tag in soup.find_all(tag_name):
            tag.decompose()

    text = " ".join(soup.stripped_strings)
    truncated = len(text) > 16_000
    if truncated:
        text = text[:16_000]

    return {"url": url, "title": title, "text": text, "truncated": truncated}


def research_web(question: str) -> dict:
    """Fetch public evidence before direct chat, independently of model tool support."""
    urls = list(dict.fromkeys(url.rstrip(".,;!?)\"'") for url in re.findall(r"https://[^\s<>]+", question)))
    warnings = []
    if not urls:
        search = search_web(question[:500])
        urls = [r["url"] for r in search["results"]]
        warnings = search["warnings"]
        if not urls:
            raise ValueError("Public web search returned no sources. " + " ".join(warnings) + " Try a public HTTPS URL instead.")
    blocks = []
    # ponytail: read at most three pages per request; use agent tools for broader research.
    for url in urls[:3]:
        try:
            page = read_web(url)
        except ValueError as exc:
            warnings.append(str(exc))
            continue
        if page["text"].strip():
            blocks.append({"source_id": page["url"], "title": page["title"] or page["url"], "text": page["text"], "origin": "public web page", "locator": {"url": page["url"]}})
            if page["truncated"]:
                warnings.append(f"Page text truncated: {page['url']}")
    if not blocks:
        raise ValueError("Could not read any public Internet sources. " + " ".join(warnings))
    return {"mode": "web", "metadata": {"title": "Public Internet research"}, "blocks": blocks, "warnings": warnings}
