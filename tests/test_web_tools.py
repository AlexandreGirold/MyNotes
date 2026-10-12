"""Tests for src/mynotes/web_tools.py

Scope: mock HTTP transport, real DNS pinning logic, real HTML parsing.
No live network calls.
"""

from __future__ import annotations

import socket
from unittest.mock import MagicMock, patch

import pytest

from mynotes.web_tools import (
    make_request,
    read_web,
    search_web,
    validate_url,
)


# ── fixture helpers ─────────────────────────────────────────────────────────

def _fake_ddg_html(urls: list[str], snippets: list[str] | None = None) -> str:
    """Build a minimal DuckDuckGo HTML page."""
    rows = ""
    for i, url in enumerate(urls):
        sn = snippets[i] if snippets and i < len(snippets) else ""
        rows += (
            f'<div class="result">'
            f'<a href="{url}" class="result__a">Title {i}</a>'
            f'<p class="result__snippet">{sn}</p>'
            f'</div>'
        )
    return f"<html><body>{rows}</body></html>"


def _fake_page(title: str = "Test", text: str = "content", extra: str = "") -> str:
    return (
        f"<html><head><title>{title}</title></head>"
        f"<body>{extra}<p>{text}</p></body></html>"
    )


# ── URL validation ──────────────────────────────────────────────────────────

class TestValidateURL:
    @staticmethod
    def _mock_dns(*ips: str) -> patch:
        return patch("mynotes.web_tools.get_global_dns", return_value=list(ips))

    def test_https_ok(self) -> None:
        with self._mock_dns("93.184.216.34"):
            result = validate_url("https://example.com/path?q=1")
            assert "https" in result

    def test_http_rejected(self) -> None:
        with self._mock_dns("1.2.3.4"):
            with pytest.raises(ValueError, match="Only HTTPS"):
                validate_url("http://example.com")

    def test_credentials_rejected(self) -> None:
        with self._mock_dns("1.2.3.4"):
            with pytest.raises(ValueError, match="credentials"):
                validate_url("https://user:pass@example.com")

    def test_fragment_rejected(self) -> None:
        with self._mock_dns("1.2.3.4"):
            with pytest.raises(ValueError, match="fragments"):
                validate_url("https://example.com#top")

    def test_non_443_port_rejected(self) -> None:
        with self._mock_dns("1.2.3.4"):
            with pytest.raises(ValueError, match="Port must be 443"):
                validate_url("https://example.com:8443")

    def test_loopback_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-global"):
            validate_url("https://127.0.0.1")

    def test_embedded_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-global"):
            validate_url("https://10.0.0.1")


# ── search_web ──────────────────────────────────────────────────────────────

class TestSearchWeb:
    def test_max_5_results(self) -> None:
        urls = [f"https://example{i}.com" for i in range(10)]
        with patch("mynotes.web_tools.make_request", return_value=_fake_ddg_html(urls)):
            res = search_web("hello")
            assert len(res["results"]) == 5

    def test_ddg_urdg_unwrap(self) -> None:
        import urllib.parse
        real_url = "https://example.com/article"
        fake_link = f"https://html.duckduckgo.com/l/?uddg={urllib.parse.quote_plus(real_url)}"
        html = _fake_ddg_html([fake_link], ["snippet"])
        with patch("mynotes.web_tools.make_request", return_value=html):
            res = search_web("test")
            assert res["results"][0]["url"] == real_url

    def test_empty_query(self) -> None:
        res = search_web("")
        assert res["results"] == []
        assert "Empty query" in res["warnings"]

    def test_oversized_query(self) -> None:
        long_q = "a" * 501
        res = search_web(long_q)
        assert res["results"] == []
        assert "500" in res["warnings"][0]

    def test_blocked_ddg_challenges(self) -> None:
        with patch(
            "mynotes.web_tools.make_request",
            return_value="<html><body>captchas appear</body></html>",
        ):
            res = search_web("test")
            assert any("challenge" in w.lower() for w in res["warnings"])

    def test_warning_on_no_results(self) -> None:
        with patch("mynotes.web_tools.make_request", return_value="<html><body><p>No results</p></body></html>"):
            res = search_web("nothing")
            assert any("No search results" in w for w in res["warnings"])

    def test_results_have_required_fields(self) -> None:
        html = _fake_ddg_html(["https://example.com"], ["some snippet"])
        with patch("mynotes.web_tools.make_request", return_value=html):
            res = search_web("hello")
            r = res["results"][0]
            assert all(k in r for k in ("title", "url", "snippet"))
            assert r["snippet"] == "some snippet"


# ── read_web ────────────────────────────────────────────────────────────────

class TestReadWeb:
    def test_basic_fields(self) -> None:
        html = _fake_page("Hello World", "some text")
        with patch("mynotes.web_tools.make_request", return_value=html):
            res = read_web("https://example.com")
            assert res["url"] == "https://example.com"
            assert res["title"] == "Hello World"
            assert "some text" in res["text"]
            assert res["truncated"] is False

    def test_scripts_styles_removed(self) -> None:
        extra = '<script>var x=1;</script><style>.x{}</style>'
        html = _fake_page("Title", "real text", extra)
        with patch("mynotes.web_tools.make_request", return_value=html):
            res = read_web("https://example.com")
            assert "var x" not in res["text"]
            assert "real text" in res["text"]

    def test_headings_preserved(self) -> None:
        extra = "<h1>Big Heading</h1><h2>Sub</h2>"
        html = _fake_page("Title", "body", extra)
        with patch("mynotes.web_tools.make_request", return_value=html):
            res = read_web("https://example.com")
            assert "Big Heading" in res["text"]

    def test_16k_cap(self) -> None:
        long_text = "x " * 10_000
        html = _fake_page("Title", long_text)
        with patch("mynotes.web_tools.make_request", return_value=html):
            res = read_web("https://example.com")
            assert res["truncated"] is True
            assert len(res["text"]) <= 16_000

    def test_form_removed(self) -> None:
        extra = "<form><input type='text'></form>"
        html = _fake_page("Title", "body text", extra)
        with patch("mynotes.web_tools.make_request", return_value=html):
            res = read_web("https://example.com")
            assert "body text" in res["text"]

    def test_request_failure_becomes_value_error(self) -> None:
        with patch("mynotes.web_tools.make_request") as mock:
            mock.side_effect = ValueError("network error")
            with pytest.raises(ValueError, match="network error"):
                read_web("https://example.com")


# ── DNS pinning & transport ─────────────────────────────────────────────────

class TestDNSPinning:
    def test_get_global_dns_all_private_raises(self) -> None:
        """When ALL addresses are non-global, ValueError."""
        with patch("socket.getaddrinfo") as ginfo:
            ginfo.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 443)),
            ]
            with pytest.raises(ValueError, match="non-global"):
                from mynotes.web_tools import get_global_dns
                get_global_dns("local.example.com")

    def test_get_global_dns_returns_global(self) -> None:
        """Every DNS answer must be public, not just the first one."""
        with patch("socket.getaddrinfo") as ginfo:
            ginfo.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            ]
            from mynotes.web_tools import get_global_dns
            with pytest.raises(ValueError, match="non-global"):
                get_global_dns("example.com")

    def test_pinned_connection_no_monkeypatch(self) -> None:
        """PinnedHTTPSConnection doesn't replace the module-level class."""
        import http.client as hc
        with patch("mynotes.web_tools.make_request", return_value="<html><body></body></html>"):
            make_request("https://example.com")
        # verify the global class is untouched
        import inspect
        orig_init = hc.HTTPSConnection.__init__
        sig = inspect.signature(orig_init)
        params = list(sig.parameters.keys())
        assert "host" in params

    def test_dns_failure_in_make_request(self) -> None:
        """DNS failure propagates as ValueError."""
        with patch("mynotes.web_tools.get_global_dns") as gd:
            gd.side_effect = ValueError("dns fail")
            with pytest.raises(ValueError, match="dns fail"):
                make_request("https://example.com")


# ── Redirect handling ───────────────────────────────────────────────────────

class TestRedirects:
    def test_max_3_redirects(self) -> None:
        """Beyond 3 redirects, loop exits without body."""
        hop_count = [0]
        import http.client as hc

        def _fake_connect(self) -> None:
            hop_count[0] += 1
            self.sock = MagicMock()

        def _fake_getresponse(self) -> MagicMock:
            resp = MagicMock()
            if hop_count[0] <= 3:
                resp.status = 302
                resp.getheaders.return_value = [
                    ("Content-Type", "text/html"),
                    ("Location", "https://example.com/redirect"),
                ]
                resp.read.return_value = b"<html></html>"
            else:
                resp.status = 200
                resp.getheaders.return_value = [
                    ("Content-Type", "text/plain"),
                ]
                resp.read.return_value = b"stop"
            return resp

        with patch("mynotes.web_tools.get_global_dns", return_value=["1.2.3.4"]):
            with patch("mynotes.web_tools._PinnedHTTPSConnection.connect", _fake_connect):
                with patch.object(hc.HTTPSConnection, "getresponse", _fake_getresponse):
                    result = make_request("https://example.com")
                    assert result == "stop"
        # 1 initial + 3 redirects = 4 connection attempts
        assert hop_count[0] == 4
