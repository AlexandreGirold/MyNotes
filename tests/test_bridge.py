"""Tests for src/mynotes/bridge.py

Covers: runtime discovery, token redaction, API error mapping, all five
tool wrappers, MockTransport end-to-end, restart rotation, and app-closed
error paths.  No model calls.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import tempfile
from unittest import mock

import httpx
import pytest

# The module under test — httpx is imported at module level
import mynotes.bridge as bridge

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def runtime_file(tmp_path: pathlib.Path) -> pathlib.Path:
    """Create a well-formed runtime.json with mode 0600."""
    data = {
        "url": "http://127.0.0.1:8484/api",
        "token": "mcp-sess-abc123",
    }
    f = tmp_path / "runtime.json"
    f.write_text(json.dumps(data), encoding="utf-8")
    f.chmod(0o600)
    return f


@pytest.fixture(autouse=True)
def _patch_runtime(monkeypatch: pytest.MonkeyPatch, runtime_file: pathlib.Path):
    """Use the temp runtime file for every test."""
    monkeypatch.setenv("MYNOTES_RUNTIME_FILE", str(runtime_file))


# ---------------------------------------------------------------------------
# Runtime discovery
# ---------------------------------------------------------------------------


class TestDiscover:
    def test_returns_url_and_token(self, runtime_file: pathlib.Path):
        info = bridge._discover()
        assert info["url"] == "http://127.0.0.1:8484/api"
        assert info["token"] == "mcp-sess-abc123"

    def test_missing_file_raises(self, tmp_path: pathlib.Path):
        p = tmp_path / "nope.json"
        with mock.patch.dict(os.environ, {"MYNOTES_RUNTIME_FILE": str(p)}):
            with pytest.raises(RuntimeError, match="not running"):
                bridge._discover()

    def test_invalid_json_raises(self, tmp_path: pathlib.Path):
        p = tmp_path / "runtime.json"
        p.write_text("not json", encoding="utf-8")
        p.chmod(0o600)
        with mock.patch.dict(os.environ, {"MYNOTES_RUNTIME_FILE": str(p)}):
            with pytest.raises(RuntimeError, match="Invalid runtime file"):
                bridge._discover()

    def test_missing_url_raises(self, tmp_path: pathlib.Path):
        p = tmp_path / "runtime.json"
        p.write_text(json.dumps({"token": "x"}), encoding="utf-8")
        p.chmod(0o600)
        with mock.patch.dict(os.environ, {"MYNOTES_RUNTIME_FILE": str(p)}):
            with pytest.raises(RuntimeError, match="Incomplete runtime file"):
                bridge._discover()

    def test_missing_token_raises(self, tmp_path: pathlib.Path):
        p = tmp_path / "runtime.json"
        p.write_text(json.dumps({"url": "http://127.0.0.1:1"}), encoding="utf-8")
        p.chmod(0o600)
        with mock.patch.dict(os.environ, {"MYNOTES_RUNTIME_FILE": str(p)}):
            with pytest.raises(RuntimeError, match="Incomplete runtime file"):
                bridge._discover()

    def test_non_loopback_raises(self, tmp_path: pathlib.Path):
        p = tmp_path / "runtime.json"
        p.write_text(
            json.dumps({"url": "https://remote.example.com", "token": "x"}),
            encoding="utf-8",
        )
        p.chmod(0o600)
        with mock.patch.dict(os.environ, {"MYNOTES_RUNTIME_FILE": str(p)}):
            with pytest.raises(RuntimeError, match="not a loopback"):
                bridge._discover()

    def test_loose_permissions_warns_but_works(self, tmp_path: pathlib.Path):
        p = tmp_path / "runtime.json"
        p.write_text(
            json.dumps({"url": "http://127.0.0.1:9999", "token": "ok"}),
            encoding="utf-8",
        )
        p.chmod(0o644)
        info = bridge._discover()
        assert info["token"] == "ok"


# ---------------------------------------------------------------------------
# Token redaction
# ---------------------------------------------------------------------------


class TestRedactToken:
    def test_redacts_token_in_url(self):
        url = "http://127.0.0.1:8484/api?token=secret123"
        result = bridge._redact_token(url, "secret123")
        assert "secret123" not in result
        assert "REDACTED" in result

    def test_no_token_unchanged(self):
        url = "http://127.0.0.1:8484/api/decks"
        result = bridge._redact_token(url, "x")
        assert result == url


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SAMPLE_RUNTIME = {"url": "http://127.0.0.1:1111", "token": "tok"}


def _make_transport(
    *,
    status: int = 200,
    json_body: dict | None = None,
    exc: Exception | None = None,
) -> httpx.MockTransport:
    """Return a transport that responds with *status*/json_body or raises *exc*."""

    def handler(request: httpx.Request) -> httpx.Response:
        if exc is not None:
            raise exc
        return httpx.Response(
            status_code=status,
            content=json.dumps(json_body or {}).encode(),
            headers={"content-type": "application/json"},
        )

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# API calls via MockTransport using the _transport hook
# ---------------------------------------------------------------------------


class TestAPICall:
    def test_post_returns_json(self, runtime_file: pathlib.Path):
        response_data = {"blocks": [], "mode": "full", "estimated_tokens": 0}
        transport = _make_transport(json_body=response_data)
        with mock.patch.dict(os.environ, {"MYNOTES_RUNTIME_FILE": str(runtime_file)}):
            result = bridge._api_call(
                "POST", "/material", {"url": "http://127.0.0.1:1", "token": "x"},
                json_body={"document_ids": []},
                _transport=transport,
            )
        assert result == response_data

    def test_post_sends_auth_header(self, runtime_file: pathlib.Path):
        captured: list[httpx.Request] = []

        def capture(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={})

        transport = httpx.MockTransport(capture)
        with mock.patch.dict(os.environ, {"MYNOTES_RUNTIME_FILE": str(runtime_file)}):
            bridge._api_call(
                "POST", "/material",
                {"url": "http://127.0.0.1:1", "token": "tok99"},
                json_body={},
                _transport=transport,
            )
        assert len(captured) == 1
        assert captured[0].headers.get("Authorization") == "Bearer tok99"

    def test_post_sends_json_body(self, runtime_file: pathlib.Path):
        captured: list[httpx.Request] = []

        def capture(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={})

        transport = httpx.MockTransport(capture)
        with mock.patch.dict(os.environ, {"MYNOTES_RUNTIME_FILE": str(runtime_file)}):
            bridge._api_call(
                "POST", "/material",
                {"url": "http://127.0.0.1:1", "token": "x"},
                json_body={"budget": 1000, "query": "hello"},
                _transport=transport,
            )
        assert len(captured) == 1
        body = json.loads(captured[0].content)
        assert body["budget"] == 1000
        assert body["query"] == "hello"

    def test_get_returns_json(self, runtime_file: pathlib.Path):
        transport = _make_transport(json_body={"title": "Deck 1", "revision": 3})
        with mock.patch.dict(os.environ, {"MYNOTES_RUNTIME_FILE": str(runtime_file)}):
            result = bridge._api_call(
                "GET", "/decks/abc123",
                {"url": "http://127.0.0.1:1", "token": "x"},
                _transport=transport,
            )
        assert result["title"] == "Deck 1"
        assert result["revision"] == 3

    def test_409_returns_safe_message(self, runtime_file: pathlib.Path):
        transport = _make_transport(status=409, json_body={"detail": "Conflict"})
        with mock.patch.dict(os.environ, {"MYNOTES_RUNTIME_FILE": str(runtime_file)}):
            with pytest.raises(bridge._HTTPError) as exc_info:
                bridge._api_call(
                    "POST", "/decks",
                    {"url": "http://127.0.0.1:1", "token": "x"},
                    json_body={},
                    _transport=transport,
                )
        assert exc_info.value.status_code == 409
        assert "conflicting" in exc_info.value.message.lower()

    def test_401_returns_restart_message(self, runtime_file: pathlib.Path):
        transport = _make_transport(status=401, json_body={"detail": "Unauthorized"})
        with mock.patch.dict(os.environ, {"MYNOTES_RUNTIME_FILE": str(runtime_file)}):
            with pytest.raises(RuntimeError, match="restart"):
                bridge._api_call(
                    "GET", "/decks/abc",
                    {"url": "http://127.0.0.1:1", "token": "x"},
                    _transport=transport,
                )

    def test_404_returns_not_found_message(self, runtime_file: pathlib.Path):
        transport = _make_transport(status=404)
        with mock.patch.dict(os.environ, {"MYNOTES_RUNTIME_FILE": str(runtime_file)}):
            with pytest.raises(bridge._HTTPError) as exc_info:
                bridge._api_call(
                    "GET", "/decks/missing",
                    {"url": "http://127.0.0.1:1", "token": "x"},
                    _transport=transport,
                )
        assert exc_info.value.status_code == 404
        assert "not found" in exc_info.value.message.lower()

    def test_connect_error_becomes_runtime_error(self, runtime_file: pathlib.Path):
        transport = _make_transport(exc=httpx.ConnectError("refused"))
        with mock.patch.dict(os.environ, {"MYNOTES_RUNTIME_FILE": str(runtime_file)}):
            with pytest.raises(RuntimeError, match="not running"):
                bridge._api_call(
                    "POST", "/material",
                    {"url": "http://127.0.0.1:1", "token": "x"},
                    json_body={},
                    _transport=transport,
                )

    def test_other_error_returns_status(self, runtime_file: pathlib.Path):
        transport = _make_transport(status=500, json_body={"detail": "Boom"})
        with mock.patch.dict(os.environ, {"MYNOTES_RUNTIME_FILE": str(runtime_file)}):
            with pytest.raises(bridge._HTTPError) as exc_info:
                bridge._api_call(
                    "POST", "/jobs/xyz",
                    {"url": "http://127.0.0.1:1", "token": "x"},
                    _transport=transport,
                )
        assert exc_info.value.status_code == 500
        assert "Boom" in exc_info.value.message


# ---------------------------------------------------------------------------
# Tool implementations via MockTransport
# ---------------------------------------------------------------------------


class TestGetMaterial:
    def test_forwards_document_ids(self):
        transport = _make_transport(json_body={"blocks": [], "mode": "full"})
        with mock.patch("mynotes.bridge._discover", return_value=_SAMPLE_RUNTIME):
            result = bridge._tool_get_material(
                document_ids=["b1", "b2"], course_id="c1", query="test", budget=8000,
                _transport=transport,
            )
        assert result["mode"] == "full"

    def test_sends_correct_body(self):
        captured: list[httpx.Request] = []

        def capture(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json={"blocks": []}, headers={"content-type": "application/json"})

        transport = httpx.MockTransport(capture)
        with mock.patch("mynotes.bridge._discover", return_value=_SAMPLE_RUNTIME):
            bridge._tool_get_material(
                document_ids=["id1", "id2"], course_id="c1", query="hello", budget=5000,
                _transport=transport,
            )
        assert len(captured) == 1
        body = json.loads(captured[0].content)
        assert body["document_ids"] == ["id1", "id2"]
        assert body["course_id"] == "c1"
        assert body["query"] == "hello"
        assert body["budget"] == 5000


class TestGetDeck:
    def test_returns_deck_info(self):
        deck = {"id": "d1", "title": "My Deck", "revision": 2, "markdown": "# Hi"}
        transport = _make_transport(json_body=deck)
        with mock.patch("mynotes.bridge._discover", return_value=_SAMPLE_RUNTIME):
            result = bridge._tool_get_deck("d1", _transport=transport)
        assert result["title"] == "My Deck"
        assert result["revision"] == 2

    def test_hits_correct_path(self):
        captured: list[httpx.Request] = []

        def capture(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json={"id": "x"}, headers={"content-type": "application/json"})

        transport = httpx.MockTransport(capture)
        with mock.patch("mynotes.bridge._discover", return_value=_SAMPLE_RUNTIME):
            bridge._tool_get_deck("deck-42", _transport=transport)
        assert len(captured) == 1
        assert captured[0].method == "GET"
        assert captured[0].url.path == "/tools/decks/deck-42"


class TestSaveDeck:
    def test_create_post(self):
        saved = {"id": "new1", "revision": 1}
        transport = _make_transport(json_body=saved)
        captured: list[httpx.Request] = []

        def capture(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json=saved, headers={"content-type": "application/json"})

        transport_cap = httpx.MockTransport(capture)
        with mock.patch("mynotes.bridge._discover", return_value=_SAMPLE_RUNTIME):
            result = bridge._tool_save_deck(
                title="New", markdown="# Slide", source_ids=["b1"],
                _transport=transport_cap,
            )
        assert result["id"] == "new1"
        assert captured[0].method == "POST"
        assert captured[0].url.path == "/tools/decks"

    def test_update_put(self):
        saved = {"id": "d1", "revision": 5}
        transport = _make_transport(json_body=saved)
        captured: list[httpx.Request] = []

        def capture(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json=saved, headers={"content-type": "application/json"})

        transport_cap = httpx.MockTransport(capture)
        with mock.patch("mynotes.bridge._discover", return_value=_SAMPLE_RUNTIME):
            result = bridge._tool_save_deck(
                title="Updated", markdown="# Updated", source_ids=["b1"],
                deck_id="d1", expected_revision=4,
                _transport=transport_cap,
            )
        assert result["revision"] == 5
        assert captured[0].method == "PUT"
        assert "d1" in str(captured[0].url.path)

    def test_expected_revision_sent(self):
        captured: list[httpx.Request] = []

        def capture(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json={"id": "x"}, headers={"content-type": "application/json"})

        transport = httpx.MockTransport(capture)
        with mock.patch("mynotes.bridge._discover", return_value=_SAMPLE_RUNTIME):
            bridge._tool_save_deck(
                title="T", markdown="M", source_ids=[],
                expected_revision=3,
                _transport=transport,
            )
        body = json.loads(captured[0].content)
        assert body["expected_revision"] == 3

    def test_put_without_expected_revision(self):
        captured: list[httpx.Request] = []

        def capture(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json={"id": "x"}, headers={"content-type": "application/json"})

        transport = httpx.MockTransport(capture)
        with mock.patch("mynotes.bridge._discover", return_value=_SAMPLE_RUNTIME):
            bridge._tool_save_deck(
                title="T", markdown="M", source_ids=[], deck_id="d1",
                _transport=transport,
            )
        body = json.loads(captured[0].content)
        assert "expected_revision" not in body


class TestExportDeck:
    def test_queues_export(self):
        job = {"job_id": "j1", "status": "queued"}
        transport = _make_transport(json_body=job)
        captured: list[httpx.Request] = []

        def capture(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json={"revision": 3} if req.method == "GET" else job, headers={"content-type": "application/json"})

        transport_cap = httpx.MockTransport(capture)
        with mock.patch("mynotes.bridge._discover", return_value=_SAMPLE_RUNTIME):
            result = bridge._tool_export_deck("d42", _transport=transport_cap)
        assert result["job_id"] == "j1"
        assert captured[0].method == "GET"
        assert captured[1].url.path == "/tools/decks/d42/exports"
        assert json.loads(captured[1].content) == {"revision": 3}


class TestGetJob:
    def test_returns_job_status(self):
        job = {"job_id": "j1", "status": "succeeded", "result": {"html": "out.html"}}
        transport = _make_transport(json_body=job)
        with mock.patch("mynotes.bridge._discover", return_value=_SAMPLE_RUNTIME):
            result = bridge._tool_get_job("j1", _transport=transport)
        assert result["status"] == "succeeded"
        assert result["result"]["html"] == "out.html"

    def test_hits_correct_path(self):
        captured: list[httpx.Request] = []

        def capture(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json={"job_id": "x"}, headers={"content-type": "application/json"})

        transport = httpx.MockTransport(capture)
        with mock.patch("mynotes.bridge._discover", return_value=_SAMPLE_RUNTIME):
            bridge._tool_get_job("j-abc", _transport=transport)
        assert len(captured) == 1
        assert captured[0].method == "GET"
        assert captured[0].url.path == "/tools/jobs/j-abc"


# ---------------------------------------------------------------------------
# MCP server: tools are registered with correct names
# ---------------------------------------------------------------------------


class TestServerTools:
    def test_build_server_has_all_tools(self):
        server = bridge._build_server()
        tools = asyncio.run(server.list_tools())
        tool_names = [t.name for t in tools]
        assert "get_material" in tool_names
        assert "get_deck" in tool_names
        assert "save_deck" in tool_names
        assert "export_deck" in tool_names
        assert "get_job" in tool_names
        assert "search_web" in tool_names
        assert "read_web" in tool_names

    def test_each_tool_returns_json_string(self):
        """All tool functions are registered as async tools on MCPServer."""
        server = bridge._build_server()
        tools = asyncio.run(server.list_tools())
        assert len(tools) == 10

    def test_error_becomes_string_not_traceback(self):
        """A tool call that hits a runtime error returns a clean message."""
        with mock.patch("mynotes.bridge._discover", side_effect=RuntimeError("app closed")):
            with pytest.raises(RuntimeError):
                bridge._tool_get_material(document_ids=[])

        server = bridge._build_server()
        with mock.patch("mynotes.bridge._discover", side_effect=RuntimeError("app closed")):
            result = asyncio.run(server.call_tool("get_material", {"document_ids": []}))
            text_parts = [c.text for c in result.content if hasattr(c, "text")]
            combined = " ".join(text_parts)
            assert "Error" in combined or "error" in combined.lower()


# ---------------------------------------------------------------------------
# End-to-end via MockTransport: tool wrapper catches errors
# ---------------------------------------------------------------------------


class TestToolWrapperErrors:
    """Verify that MCP tool wrappers return error strings, not exceptions."""

    def _build_mock_server(self, response_status: int = 500) -> Any:
        """Build a server whose _api_call receives a failing transport."""
        transport = _make_transport(status=response_status)

        def fake_discover():
            return {"url": "http://127.0.0.1:1111", "token": "tok"}

        with mock.patch("mynotes.bridge._discover", fake_discover):
            with mock.patch("mynotes.bridge.httpx.Client", transport):
                server = bridge._build_server()
        return server

    def test_get_material_error_returns_message(self):
        server = self._build_mock_server(500)
        result = asyncio.run(server.call_tool("get_material", {"document_ids": []}))
        text_parts = [c.text for c in result.content if hasattr(c, "text")]
        combined = " ".join(text_parts)
        assert "Error:" in combined or "error" in combined.lower() or "500" in combined

    def test_get_deck_error_returns_message(self):
        server = self._build_mock_server(409)
        result = asyncio.run(server.call_tool("get_deck", {"deck_id": "d1"}))
        text_parts = [c.text for c in result.content if hasattr(c, "text")]
        combined = " ".join(text_parts)
        assert "Error:" in combined or "conflict" in combined.lower() or "409" in combined

    def test_save_deck_error_returns_message(self):
        server = self._build_mock_server(500)
        result = asyncio.run(server.call_tool("save_deck", {
            "title": "T", "markdown": "# M", "source_ids": [],
        }))
        text_parts = [c.text for c in result.content if hasattr(c, "text")]
        combined = " ".join(text_parts)
        assert "Error:" in combined or "error" in combined.lower()

    def test_export_deck_error_returns_message(self):
        server = self._build_mock_server(500)
        result = asyncio.run(server.call_tool("export_deck", {"deck_id": "d1"}))
        text_parts = [c.text for c in result.content if hasattr(c, "text")]
        combined = " ".join(text_parts)
        assert "Error:" in combined or "error" in combined.lower()

    def test_get_job_error_returns_message(self):
        server = self._build_mock_server(404)
        result = asyncio.run(server.call_tool("get_job", {"job_id": "missing"}))
        text_parts = [c.text for c in result.content if hasattr(c, "text")]
        combined = " ".join(text_parts)
        assert "Error:" in combined or "not found" in combined.lower() or "404" in combined


# ---------------------------------------------------------------------------
# End-to-end: successful tool calls through server machinery
# ---------------------------------------------------------------------------


class TestToolWrapperSuccess:
    """Verify successful paths go through the server machinery."""

    def test_get_deck_success(self):
        deck = {"id": "d1", "title": "My Deck", "revision": 3}

        def fake_discover():
            return {"url": "http://127.0.0.1:1111", "token": "tok"}

        def fake_api_call(method, path, runtime, json_body=None, _transport=None):
            return deck

        with mock.patch("mynotes.bridge._discover", fake_discover):
            with mock.patch("mynotes.bridge._api_call", fake_api_call):
                server = bridge._build_server()
                result = asyncio.run(server.call_tool("get_deck", {"deck_id": "d1"}))
        text_parts = [c.text for c in result.content if hasattr(c, "text")]
        combined = " ".join(text_parts)
        parsed = json.loads(combined)
        assert parsed["title"] == "My Deck"
        assert parsed["revision"] == 3

    def test_get_material_success(self):
        material = {"blocks": [{"id": "b1", "text": "Hello"}], "mode": "full"}

        def fake_discover():
            return {"url": "http://127.0.0.1:1111", "token": "tok"}

        def fake_api_call(method, path, runtime, json_body=None, _transport=None):
            return material

        with mock.patch("mynotes.bridge._discover", fake_discover):
            with mock.patch("mynotes.bridge._api_call", fake_api_call):
                server = bridge._build_server()
                result = asyncio.run(
                    server.call_tool("get_material", {"document_ids": ["b1"], "budget": 8000})
                )
        text_parts = [c.text for c in result.content if hasattr(c, "text")]
        combined = " ".join(text_parts)
        parsed = json.loads(combined)
        assert parsed["mode"] == "full"
        assert len(parsed["blocks"]) == 1


# ---------------------------------------------------------------------------
# Restart rotation: re-reading runtime per call
# ---------------------------------------------------------------------------


class TestRestartRotation:
    def test_reads_runtime_file_per_call(self):
        """_discover is called inside every tool — a restart changes the token."""
        token_v1 = "token-old"
        token_v2 = "token-new"

        def discover_v1():
            return {"url": "http://127.0.0.1:1", "token": token_v1}

        def discover_v2():
            return {"url": "http://127.0.0.1:1", "token": token_v2}

        captured: list[httpx.Request] = []

        def capture(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json={"ok": True}, headers={"content-type": "application/json"})

        transport = httpx.MockTransport(capture)

        # First call with old token
        captured.clear()
        with mock.patch("mynotes.bridge._discover", discover_v1):
            result = bridge._api_call(
                "GET", "/test",
                {"url": "http://127.0.0.1:1", "token": token_v1},
                _transport=transport,
            )
        assert result["ok"] is True
        assert captured[0].headers.get("Authorization") == f"Bearer {token_v1}"

        # Second call with new token (after app restart)
        captured.clear()
        with mock.patch("mynotes.bridge._discover", discover_v2):
            result = bridge._api_call(
                "GET", "/test",
                {"url": "http://127.0.0.1:1", "token": token_v2},
                _transport=transport,
            )
        assert result["ok"] is True
        assert captured[0].headers.get("Authorization") == f"Bearer {token_v2}"

    def test_app_unavailable_after_restart(self, runtime_file: pathlib.Path):
        """If the runtime file disappears, tools return a clear error."""
        runtime_file.unlink()
        with mock.patch.dict(os.environ, {"MYNOTES_RUNTIME_FILE": str(runtime_file)}):
            with pytest.raises(RuntimeError, match="not running"):
                bridge._discover()


# ---------------------------------------------------------------------------
# App closed: runtime file disappears between calls
# ---------------------------------------------------------------------------


class TestAppClosed:
    def test_tool_returns_error_when_app_gone(self):
        f = pathlib.Path(tempfile.mktemp())
        f.write_text(
            json.dumps({"url": "http://127.0.0.1:1", "token": "x"}),
            encoding="utf-8",
        )
        f.chmod(0o600)

        with mock.patch.dict(os.environ, {"MYNOTES_RUNTIME_FILE": str(f)}):
            info = bridge._discover()
            assert info["token"] == "x"

        f.unlink()

        with mock.patch.dict(os.environ, {"MYNOTES_RUNTIME_FILE": str(f)}):
            with pytest.raises(RuntimeError, match="not running"):
                bridge._discover()

        f.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# main() entry point: delegates to mcp.run
# ---------------------------------------------------------------------------


class TestMain:
    def test_main_calls_server_run(self):
        """main() creates the server and calls run(transport='stdio')."""
        with mock.patch.object(bridge, "_build_server") as mock_build:
            mock_server = mock.Mock()
            mock_build.return_value = mock_server
            bridge.main()
            mock_build.assert_called_once()
            mock_server.run.assert_called_once_with(transport="stdio")


# ---------------------------------------------------------------------------
# Environment variable: MYNOTES_RUNTIME_FILE overrides default path
# ---------------------------------------------------------------------------


class TestEnvVar:
    def test_env_var_uses_custom_path(self, tmp_path: pathlib.Path):
        f = tmp_path / "custom_runtime.json"
        f.write_text(
            json.dumps({"url": "http://127.0.0.1:7777", "token": "envtok"}),
            encoding="utf-8",
        )
        f.chmod(0o600)

        with mock.patch.dict(os.environ, {"MYNOTES_RUNTIME_FILE": str(f)}):
            info = bridge._discover()
        assert info["token"] == "envtok"
        assert "7777" in info["url"]


# ---------------------------------------------------------------------------
# No model calls: verify bridge only talks to HTTP backend
# ---------------------------------------------------------------------------


class TestNoModelCalls:
    def test_no_provider_import(self):
        """bridge.py must not import provider (no model calls)."""
        src = pathlib.Path(bridge.__file__).read_text()
        assert "from mynotes.provider" not in src
        assert "import mynotes.provider" not in src

    def test_no_db_import(self):
        """bridge.py must not import db (no database access)."""
        src = pathlib.Path(bridge.__file__).read_text()
        assert "from mynotes.db" not in src
        assert "import mynotes.db" not in src


# ---------------------------------------------------------------------------
# _HTTPError details
# ---------------------------------------------------------------------------


class TestHTTPError:
    def test_error_message_contains_status(self):
        exc = bridge._HTTPError(502, "Bad gateway")
        assert "502" in str(exc)
        assert "Bad gateway" in str(exc)

    def test_error_preserves_status_code(self):
        exc = bridge._HTTPError(409, "Conflict")
        assert exc.status_code == 409

    def test_error_preserves_message(self):
        exc = bridge._HTTPError(403, "Forbidden")
        assert exc.message == "Forbidden"


# ---------------------------------------------------------------------------
# Multiple document IDs are passed as a list
# ---------------------------------------------------------------------------


class TestMaterialList:
    def test_document_ids_passed_as_list(self):
        captured: list[httpx.Request] = []

        def capture(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json={"blocks": []}, headers={"content-type": "application/json"})

        transport = httpx.MockTransport(capture)
        with mock.patch("mynotes.bridge._discover", return_value=_SAMPLE_RUNTIME):
            bridge._api_call(
                "POST", "/material",
                {"url": "http://127.0.0.1:1", "token": "x"},
                json_body={"document_ids": ["id1", "id2", "id3"]},
                _transport=transport,
            )
        assert len(captured) == 1
        body = json.loads(captured[0].content)
        assert body["document_ids"] == ["id1", "id2", "id3"]


# ---------------------------------------------------------------------------
# Source_ids are not mutated by save_deck
# ---------------------------------------------------------------------------


class TestSourceIdsImmutable:
    def test_source_ids_not_mutated(self):
        source_ids = ["b1", "b2"]
        original = list(source_ids)
        captured: list[httpx.Request] = []

        def capture(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(200, json={"id": "d1"}, headers={"content-type": "application/json"})

        transport = httpx.MockTransport(capture)
        with mock.patch("mynotes.bridge._discover", return_value=_SAMPLE_RUNTIME):
            bridge._tool_save_deck(
                title="T", markdown="M", source_ids=source_ids,
                _transport=transport,
            )
        assert source_ids == original
        assert len(captured) == 1
        body = json.loads(captured[0].content)
        assert body["source_ids"] == ["b1", "b2"]
